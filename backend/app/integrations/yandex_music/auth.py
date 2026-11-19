"""Yandex Music authentication: Device Flow, manual token and OAuth refresh.

The manager owns every secret-bearing operation: the token file, the device
flow state (including ``device_code``), the OAuth refresh and the async client.
Neither tokens nor ``device_code`` ever leave this module through API models,
WebSocket payloads, logs or exception messages.

Only methods confirmed in ``docs/YANDEX_API.md`` are used from ``yandex_music``;
OAuth refresh is a documented HTTP contract (docs/YANDEX_API.md §1), not a
library method.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal

import aiohttp
from yandex_music import ClientAsync

# yandex-music ships the public Android application credentials; the frozen
# contract (docs/YANDEX_API.md §1) allows reusing them when config overrides are
# absent. Overrides come from yandex.client_id / yandex.client_secret.
from yandex_music._client_async.device_auth import _DEFAULT_CLIENT_ID, _DEFAULT_CLIENT_SECRET
from yandex_music.exceptions import DeviceAuthError, NetworkError, UnauthorizedError

from app.config import YandexSettings
from app.core.event_bus import EventBus
from app.core.models import YandexAuthStatus, YandexDeviceStart, YandexDeviceStatus
from app.core.timeutils import to_iso
from app.errors import AuthenticationError, ConfigurationError, NotFoundError, UnavailableError
from app.integrations.yandex_music.token_store import TokenData, TokenSource, TokenStore
from app.logging_setup import get_logger
from app.services.qr import qr_png_data_uri

logger = get_logger("yandex.auth")

OAUTH_TOKEN_URL = "https://oauth.yandex.ru/token"
DEFAULT_DEVICE_NAME = "MusicNX"

ClientFactory = Callable[[str | None], ClientAsync]
FlowState = Literal["pending", "authorized", "expired", "denied", "error"]


class _RefreshFailedError(Exception):
    """Internal marker for a failed OAuth refresh (never carries token data)."""


def _default_client_factory(token: str | None = None) -> ClientAsync:
    return ClientAsync(token)


def _login_from_client(client: Any) -> str | None:
    """Read ``me.account.login`` defensively: schema drift must not crash login."""

    try:
        login = client.me.account.login
    except (AttributeError, TypeError):
        return None
    return login if isinstance(login, str) and login else None


def _token_data_from_oauth(token: Any) -> TokenData | None:
    """Convert the library ``OAuthToken`` into ``TokenData``; ``None`` on drift."""

    access_token = getattr(token, "access_token", None)
    if not isinstance(access_token, str) or not access_token:
        return None
    expires_in = getattr(token, "expires_in", None)
    if isinstance(expires_in, bool) or not isinstance(expires_in, int):
        expires_in = None
    try:
        return TokenData(
            access_token=access_token,
            refresh_token=_string_or(getattr(token, "refresh_token", None), None),
            expires_in=expires_in,
            obtained_at=time.time(),
            token_type=_string_or(getattr(token, "token_type", None), None),
            login=None,
            source="device",
        )
    except ValueError:
        return None


def _epoch_iso(value: float) -> str:
    return to_iso(datetime.fromtimestamp(value, UTC))


def _auth_status(token: TokenData | None) -> YandexAuthStatus:
    """Public auth status without any secret field."""

    if token is None:
        return YandexAuthStatus(authenticated=False)
    expires_at = token.expires_at
    # An expired grant is still usable when a refresh token can renew it.
    authenticated = expires_at is None or expires_at > time.time() or bool(token.refresh_token)
    if not authenticated:
        return YandexAuthStatus(authenticated=False)
    return YandexAuthStatus(
        authenticated=True,
        login=token.login,
        expires_at=_epoch_iso(expires_at) if expires_at is not None else None,
        source=token.source,
    )


@dataclass
class _DeviceFlow:
    """In-memory Device Flow state; ``device_code`` never leaves the manager."""

    flow_id: str
    device_code: str = field(repr=False)
    user_code: str
    verification_url: str
    interval: int
    expires_at: float
    state: FlowState = "pending"
    login: str | None = None
    error: str | None = None
    client: Any = field(default=None, repr=False)
    task: asyncio.Task[None] | None = None


class YandexAuthManager:
    """Real Yandex authentication backed by ``yandex_music.ClientAsync``."""

    mode = "real"

    def __init__(
        self,
        settings: YandexSettings,
        bus: EventBus,
        *,
        client_factory: ClientFactory | None = None,
        mode: str = "real",
    ) -> None:
        if settings.token_file is None:
            raise ConfigurationError("yandex.token_file must be configured in real mode")
        self._settings = settings
        self._bus = bus
        self.mode = mode
        self._client_factory = client_factory or _default_client_factory
        self._store = TokenStore(settings.token_file)
        self._client: Any = None
        self._client_token: str | None = None
        self._refresh_lock = asyncio.Lock()
        self._flows: dict[str, _DeviceFlow] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    # -- public API ---------------------------------------------------------

    async def status(self) -> YandexAuthStatus:
        token = await asyncio.to_thread(self._store.load)
        return _auth_status(token)

    async def start_device_flow(self) -> YandexDeviceStart:
        """Request a device code and start polling it in the background."""

        self._prune_flows()
        client = self._client_factory(None)
        try:
            device = await client.request_device_code(
                device_name=self._settings.device_name or DEFAULT_DEVICE_NAME,
                client_id=self._client_id,
            )
        except Exception as exc:
            logger.warning("Yandex device-code request failed (%s)", type(exc).__name__)
            raise UnavailableError("Yandex Music authorization is unavailable") from None

        interval = max(int(device.interval or 0), 1)
        expires_in = max(int(device.expires_in or 0), 0)
        flow = _DeviceFlow(
            flow_id=uuid.uuid4().hex,
            device_code=device.device_code,
            user_code=device.user_code,
            verification_url=device.verification_url,
            interval=interval,
            expires_at=time.time() + expires_in,
            client=client,
        )
        self._flows[flow.flow_id] = flow
        task = asyncio.create_task(self._poll_flow(flow), name=f"yandex-device-{flow.flow_id}")
        flow.task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return YandexDeviceStart(
            flow_id=flow.flow_id,
            user_code=flow.user_code,
            verification_url=flow.verification_url,
            expires_in=expires_in,
            interval=interval,
            qr_data_uri=qr_png_data_uri(flow.verification_url),
        )

    async def device_status(self, flow_id: str) -> YandexDeviceStatus:
        flow = self._flows.get(flow_id)
        if flow is None:
            raise NotFoundError("device authorization flow not found", details={"flow_id": flow_id})
        expires_at = flow.expires_at
        if flow.state == "authorized":
            token = await asyncio.to_thread(self._store.load)
            if token is not None and token.expires_at is not None:
                expires_at = token.expires_at
        return YandexDeviceStatus(
            state=flow.state,
            flow_id=flow.flow_id,
            login=flow.login,
            expires_at=_epoch_iso(expires_at),
            error=flow.error,
        )

    async def set_manual_token(self, token: str) -> YandexAuthStatus:
        """Validate a pasted token via ``ClientAsync.init()`` and persist it."""

        candidate = token.strip()
        if not candidate:
            raise AuthenticationError("Yandex token is empty", code="invalid_token")
        client = self._client_factory(candidate)
        try:
            await client.init()
            login = _login_from_client(client)
        except Exception as exc:
            logger.warning("manual Yandex token validation failed (%s)", type(exc).__name__)
            raise AuthenticationError("Yandex token is invalid", code="invalid_token") from None

        data = TokenData(
            access_token=candidate,
            refresh_token=None,
            expires_in=None,
            obtained_at=time.time(),
            token_type=None,
            login=login,
            source="manual",
        )
        async with self._refresh_lock:
            # A pending device flow must not overwrite the manual token later.
            cancelled: list[asyncio.Task[None]] = []
            for flow in list(self._flows.values()):
                if flow.state == "pending":
                    flow.state = "error"
                    flow.error = "cancelled"
                    if flow.task is not None:
                        flow.task.cancel()
                        cancelled.append(flow.task)
            self._flows.clear()
            if cancelled:
                await asyncio.gather(*cancelled, return_exceptions=True)
            try:
                await asyncio.to_thread(self._store.save, data)
            except OSError:
                logger.error("cannot persist Yandex token file %s", self._store.path)
                raise UnavailableError("cannot persist Yandex token") from None
            await self._replace_client(client, data.access_token)
            await self._publish_auth_status()
        return _auth_status(data)

    async def logout(self) -> None:
        """Cancel pending flows, drop the token file and the in-memory client.

        Serialized with refresh: otherwise an in-flight refresh could recreate
        the token file right after logout (and the account would stay logged in).
        """

        async with self._refresh_lock:
            cancelled: list[asyncio.Task[None]] = []
            for flow in list(self._flows.values()):
                if flow.state == "pending":
                    flow.state = "error"
                    flow.error = "cancelled"
                    if flow.task is not None:
                        flow.task.cancel()
                        cancelled.append(flow.task)
            self._flows.clear()
            if cancelled:
                await asyncio.gather(*cancelled, return_exceptions=True)
            await self._replace_client(None, None)
            await asyncio.to_thread(self._store.clear)
            await self._publish_auth_status()

    async def get_client(self) -> ClientAsync:
        """Return an initialized client, refreshing the token when needed.

        A rejected token (``UnauthorizedError``/``NetworkError`` during
        ``init()``) triggers exactly one refresh-and-retry attempt. Refresh
        failures publish ``auth.required`` and raise ``AuthenticationError``.
        """

        if self._closed:
            await self._publish_auth_required("manager_closed")
            raise AuthenticationError("Yandex Music is not available")
        token = await asyncio.to_thread(self._store.load)
        if token is None:
            await self._publish_auth_required("no_token")
            raise AuthenticationError("Yandex Music is not authenticated")

        if self._needs_refresh(token):
            token = await self._refresh_token()

        if self._client is not None and self._client_token == token.access_token:
            return self._client
        return await self._open_client(token)

    async def refresh_access_token(self) -> None:
        """Force an OAuth refresh when a refresh token exists.

        Compatibility hook for ``YandexClient.client(refresh=True)`` retries
        (catalog/stream). Manual tokens cannot be refreshed and are reused.
        """

        token = await asyncio.to_thread(self._store.load)
        if token is None or not token.refresh_token:
            return
        await self._refresh_token(force=True)

    async def close(self) -> None:
        """Cancel background polls and forget the client (idempotent)."""

        if self._closed:
            return
        self._closed = True
        tasks = [flow.task for flow in self._flows.values() if flow.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._flows.clear()
        await self._replace_client(None, None)

    # -- device flow --------------------------------------------------------

    async def _poll_flow(self, flow: _DeviceFlow) -> None:
        try:
            await self._poll_device_code(flow)
        except asyncio.CancelledError:
            raise
        finally:
            flow.client = None

    async def _poll_device_code(self, flow: _DeviceFlow) -> None:
        client = flow.client
        while flow.state == "pending":
            await asyncio.sleep(flow.interval)
            if flow.state != "pending":
                return
            if time.time() >= flow.expires_at:
                await self._finish_flow(flow, "expired", error="expired_token")
                return
            try:
                token = await client.poll_device_token(
                    flow.device_code,
                    client_id=self._client_id,
                    client_secret=self._client_secret,
                )
            except DeviceAuthError as exc:
                reason = str(exc).lower()
                if "access_denied" in reason or "denied" in reason:
                    await self._finish_flow(flow, "denied", error="access_denied")
                elif "expired" in reason:
                    await self._finish_flow(flow, "expired", error="expired_token")
                else:
                    await self._finish_flow(flow, "error", error="device_auth_failed")
                return
            except (UnauthorizedError, NetworkError):
                await self._finish_flow(flow, "error", error="device_auth_failed")
                return
            except Exception as exc:
                logger.warning("device flow poll failed (%s)", type(exc).__name__)
                await self._finish_flow(flow, "error", error="device_auth_failed")
                return
            if token is None:
                continue
            await self._complete_device_flow(flow, token)
            return

    async def _complete_device_flow(self, flow: _DeviceFlow, token: Any) -> None:
        # Serialize with logout/manual-token: a flow that finishes after logout
        # must not resurrect the token file or the in-memory client.
        async with self._refresh_lock:
            if flow.state != "pending":
                return
            data = _token_data_from_oauth(token)
            if data is None:
                logger.warning("device flow returned an unusable OAuth token payload")
                await self._finish_flow(flow, "error", error="token_validation_failed")
                return
            try:
                client = self._client_factory(data.access_token)
                await client.init()
                login = _login_from_client(client)
            except Exception as exc:
                logger.warning(
                    "device flow token validation failed (%s)", type(exc).__name__
                )
                await self._finish_flow(flow, "error", error="token_validation_failed")
                return

            data = replace(data, login=login)
            try:
                await asyncio.to_thread(self._store.save, data)
            except OSError:
                logger.error("cannot persist Yandex token file %s", self._store.path)
                await self._finish_flow(flow, "error", error="token_store_failed")
                return

            await self._replace_client(client, data.access_token)
            flow.login = login
            flow.state = "authorized"
            flow.error = None
            flow.task = None
            await self._publish_auth_status()

    async def _finish_flow(self, flow: _DeviceFlow, state: FlowState, *, error: str) -> None:
        flow.state = state
        flow.error = error
        flow.task = None
        await self._publish_auth_required(f"device_flow_{state}")

    def _prune_flows(self) -> None:
        """Drop finished flows once their device code is long dead."""

        cutoff = time.time() - 3600
        for flow_id, flow in list(self._flows.items()):
            if flow.state != "pending" and flow.expires_at < cutoff:
                del self._flows[flow_id]

    # -- token refresh ------------------------------------------------------

    def _needs_refresh(self, token: TokenData) -> bool:
        expires_at = token.expires_at
        if expires_at is None:
            return False
        return expires_at - time.time() < self._settings.refresh_before_s

    async def _refresh_token(
        self, *, force: bool = False, rejected_access_token: str | None = None
    ) -> TokenData:
        """Single-flight refresh: concurrent callers await one HTTP exchange.

        ``force`` ignores the proactive-refresh window and is used after a
        client rejected the current token.
        """

        async with self._refresh_lock:
            current = await asyncio.to_thread(self._store.load)
            if current is None:
                await self._publish_auth_required("no_token")
                raise AuthenticationError("Yandex Music is not authenticated")
            if not force and not self._needs_refresh(current):
                # Another caller refreshed while we waited for the lock.
                return current
            if (
                force
                and rejected_access_token is not None
                and current.access_token != rejected_access_token
            ):
                # A concurrent 401 retry already refreshed this same token.
                return current
            if not current.refresh_token:
                await self._publish_auth_required("refresh_unavailable")
                raise AuthenticationError("Yandex Music token cannot be refreshed")

            try:
                payload = await self._post_refresh(current.refresh_token)
            except Exception as exc:
                logger.warning("Yandex OAuth refresh failed (%s)", type(exc).__name__)
                await self._publish_auth_required("refresh_failed")
                raise AuthenticationError("Yandex Music token refresh failed") from None

            access_token = payload.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                logger.warning("Yandex OAuth refresh response has no access token")
                await self._publish_auth_required("refresh_failed")
                raise AuthenticationError("Yandex Music token refresh failed") from None

            refreshed = TokenData(
                access_token=access_token,
                refresh_token=_string_or(payload.get("refresh_token"), current.refresh_token),
                expires_in=_int_or(payload.get("expires_in"), current.expires_in),
                obtained_at=time.time(),
                token_type=_string_or(payload.get("token_type"), current.token_type),
                login=current.login,
                source=current.source,
            )
            try:
                await asyncio.to_thread(self._store.save, refreshed)
            except OSError:
                logger.error("cannot persist refreshed Yandex token to %s", self._store.path)
                raise UnavailableError("cannot persist Yandex token") from None
            await self._publish_auth_status()
            return refreshed

    async def _post_refresh(self, refresh_token: str) -> dict[str, Any]:
        """Exchange a refresh token at the OAuth endpoint (docs/YANDEX_API.md §1)."""

        timeout = aiohttp.ClientTimeout(total=self._settings.request_timeout_s)
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.post(OAUTH_TOKEN_URL, data=data) as response,
        ):
            if response.status != 200:
                raise _RefreshFailedError(f"oauth refresh returned {response.status}")
            payload = await response.json(content_type=None)
        if not isinstance(payload, dict):
            raise _RefreshFailedError("oauth refresh returned a non-object payload")
        return payload

    # -- client lifecycle ---------------------------------------------------

    async def _open_client(self, token: TokenData) -> ClientAsync:
        try:
            client = self._client_factory(token.access_token)
            await client.init()
        except (UnauthorizedError, NetworkError):
            # One retry after a forced refresh; manual tokens cannot refresh.
            token = await self._refresh_token(
                force=True, rejected_access_token=token.access_token
            )
            try:
                client = self._client_factory(token.access_token)
                await client.init()
            except (UnauthorizedError, NetworkError):
                await self._publish_auth_required("yandex_auth_failed")
                raise AuthenticationError("Yandex Music token was rejected") from None
            except Exception as exc:
                logger.warning("Yandex client start failed (%s)", type(exc).__name__)
                raise UnavailableError("Yandex Music is unavailable") from None
        except Exception as exc:
            logger.warning("Yandex client start failed (%s)", type(exc).__name__)
            raise UnavailableError("Yandex Music is unavailable") from None
        await self._replace_client(client, token.access_token)
        return client

    async def _replace_client(self, client: Any, access_token: str | None) -> None:
        previous = self._client
        self._client = client
        self._client_token = access_token
        if previous is not None and previous is not client:
            close = getattr(previous, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    logger.debug("Yandex client shutdown failed", exc_info=True)

    # -- configuration and events --------------------------------------------

    @property
    def _client_id(self) -> str:
        return self._settings.client_id or _DEFAULT_CLIENT_ID

    @property
    def _client_secret(self) -> str:
        return self._settings.client_secret or _DEFAULT_CLIENT_SECRET

    async def _publish_auth_status(self) -> None:
        status = await self.status()
        await self._publish("auth.status", status.model_dump())

    async def _publish_auth_required(self, reason: str) -> None:
        await self._publish("auth.required", {"reason": reason})

    async def _publish(self, event_type: str, payload: dict[str, Any]) -> None:
        try:
            await self._bus.publish(event_type, payload)
        except Exception:
            # A WebSocket fan-out problem must never break the auth operation.
            logger.warning("cannot publish %s event", event_type)


@dataclass
class _MockFlow:
    flow_id: str
    user_code: str
    verification_url: str
    interval: int
    expires_in: int
    polls: int = 0
    state: FlowState = "pending"
    login: str | None = None
    error: str | None = None


class MockYandexAuthManager:
    """Network-free authentication double with the real manager's interface.

    A device flow starts ``pending`` and becomes ``authorized`` on the
    ``authorized_after_polls``-th status poll. No real client exists, so
    ``get_client()`` fails with a clear ``UnavailableError``.
    """

    mode = "mock"

    def __init__(
        self,
        settings: YandexSettings,
        bus: EventBus,
        *,
        client_factory: ClientFactory | None = None,
        mode: str = "mock",
        authorized_after_polls: int = 2,
        expires_in: int = 300,
        interval: int = 5,
    ) -> None:
        self._settings = settings
        self._bus = bus
        self.mode = mode
        self._authorized_after_polls = max(int(authorized_after_polls), 1)
        self._expires_in = expires_in
        self._interval = interval
        self._flows: dict[str, _MockFlow] = {}
        self._authenticated = False
        self._login: str | None = None
        self._source: TokenSource | None = None
        self._expires_at: float | None = None

    async def status(self) -> YandexAuthStatus:
        return YandexAuthStatus(
            authenticated=self._authenticated,
            login=self._login,
            expires_at=(_epoch_iso(self._expires_at) if self._expires_at is not None else None),
            source=self._source,
        )

    async def start_device_flow(self) -> YandexDeviceStart:
        flow_id = uuid.uuid4().hex
        user_code = f"{uuid.uuid4().hex[:4].upper()}-{uuid.uuid4().hex[:4].upper()}"
        verification_url = "https://ya.ru/device"
        self._flows[flow_id] = _MockFlow(
            flow_id=flow_id,
            user_code=user_code,
            verification_url=verification_url,
            interval=self._interval,
            expires_in=self._expires_in,
        )
        return YandexDeviceStart(
            flow_id=flow_id,
            user_code=user_code,
            verification_url=verification_url,
            expires_in=self._expires_in,
            interval=self._interval,
            qr_data_uri=qr_png_data_uri(verification_url),
        )

    async def device_status(self, flow_id: str) -> YandexDeviceStatus:
        flow = self._flows.get(flow_id)
        if flow is None:
            raise NotFoundError("device authorization flow not found", details={"flow_id": flow_id})
        if flow.state == "pending":
            flow.polls += 1
            if flow.polls >= self._authorized_after_polls:
                flow.state = "authorized"
                flow.login = "mock-user"
                self._authenticated = True
                self._login = flow.login
                self._source = "device"
                self._expires_at = time.time() + flow.expires_in
                await self._publish_auth_status()
        return YandexDeviceStatus(
            state=flow.state,
            flow_id=flow.flow_id,
            login=flow.login,
            expires_at=_epoch_iso(time.time() + flow.expires_in),
            error=flow.error,
        )

    async def set_manual_token(self, token: str) -> YandexAuthStatus:
        if not token.strip():
            raise AuthenticationError("Yandex token is empty", code="invalid_token")
        self._authenticated = True
        self._login = "mock-user"
        self._source = "manual"
        self._expires_at = None
        await self._publish_auth_status()
        return await self.status()

    async def logout(self) -> None:
        self._flows.clear()
        self._authenticated = False
        self._login = None
        self._source = None
        self._expires_at = None
        await self._publish_auth_status()

    async def get_client(self) -> ClientAsync:
        raise UnavailableError(
            "mock Yandex adapter does not provide an HTTP client; use the adapter API"
        )

    async def refresh_access_token(self) -> None:
        """No-op: the mock manager has no OAuth grant to refresh."""

        return None

    async def close(self) -> None:
        self._flows.clear()

    async def _publish_auth_status(self) -> None:
        status = await self.status()
        await self._bus.publish("auth.status", status.model_dump())


def _string_or(value: Any, fallback: str | None) -> str | None:
    return value if isinstance(value, str) and value else fallback


def _int_or(value: Any, fallback: int | None) -> int | None:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return fallback
    return fallback
