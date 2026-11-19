"""Yandex auth manager and API routes. No network, no real credentials, no Pi."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from app.api.routes import auth as auth_routes
from app.api.routes import yandex_auth as yandex_auth_router
from app.auth import PinAttemptLimiter
from app.config import Settings, YandexSettings
from app.core.event_bus import EventBus
from app.errors import AppError, AuthenticationError, NotFoundError, UnavailableError
from app.integrations.yandex_music.auth import (
    MockYandexAuthManager,
    YandexAuthManager,
)
from app.integrations.yandex_music.token_store import TokenData, TokenStore
from app.main import create_app
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from tests.conftest import make_settings
from yandex_music.exceptions import DeviceAuthError, NetworkError, UnauthorizedError

DEVICE_CODE = "device-code-secret-value"
ACCESS_TOKEN = "manual-access-secret"
REFRESH_TOKEN = "refresh-secret-value"
QR_PREFIX = "data:image/png;base64,"


# --- fakes -----------------------------------------------------------------


@dataclass
class FakeDeviceCode:
    user_code: str = "ABCD-EFGH"
    verification_url: str = "https://ya.ru/device"
    expires_in: int = 30
    interval: int = 1
    device_code: str = DEVICE_CODE


@dataclass
class FakeOAuthToken:
    access_token: str = "device-access-secret"
    refresh_token: str | None = "device-refresh-secret"
    expires_in: int = 3600
    token_type: str = "bearer"


class FakeClient:
    def __init__(self, token: str | None, factory: FakeFactory) -> None:
        self.token = token
        self.factory = factory
        self.closed = False

    async def request_device_code(
        self, *, device_name: str | None = None, client_id: str | None = None
    ) -> FakeDeviceCode:
        self.factory.device_requests.append((device_name, client_id))
        if self.factory.device_error is not None:
            raise self.factory.device_error
        return self.factory.device_code

    async def poll_device_token(
        self,
        device_code: str,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> FakeOAuthToken | None:
        self.factory.poll_calls.append(device_code)
        if self.factory.poll_error is not None:
            raise self.factory.poll_error
        if self.factory.poll_results:
            return self.factory.poll_results.pop(0)
        return None

    async def init(self) -> FakeClient:
        self.factory.init_tokens.append(self.token)
        if self.factory.init_errors:
            raise self.factory.init_errors.pop(0)
        self.me = SimpleNamespace(
            account=SimpleNamespace(login=self.factory.login),
        )
        return self

    async def close(self) -> None:
        self.closed = True


class FakeFactory:
    """Test double for ``ClientAsync`` construction (records every call)."""

    def __init__(self) -> None:
        self.created: list[FakeClient] = []
        self.device_requests: list[tuple[str | None, str | None]] = []
        self.poll_calls: list[str] = []
        self.init_tokens: list[str | None] = []
        self.init_errors: list[BaseException] = []
        self.poll_results: list[FakeOAuthToken] = []
        self.poll_error: BaseException | None = None
        self.device_error: BaseException | None = None
        self.device_code = FakeDeviceCode()
        self.login = "mock-user"

    def __call__(self, token: str | None = None) -> FakeClient:
        client = FakeClient(token, self)
        self.created.append(client)
        return client


# --- helpers ---------------------------------------------------------------


def make_manager(
    tmp_path: Path,
    bus: EventBus,
    factory: FakeFactory,
    **settings_overrides: Any,
) -> YandexAuthManager:
    settings = YandexSettings(
        token_file=tmp_path / "secrets" / "yandex_token.json",
        device_name="MusicNX-test",
        refresh_before_s=120,
        request_timeout_s=1,
        **settings_overrides,
    )
    return YandexAuthManager(settings, bus, client_factory=factory)


def drain(subscription: Any) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    while True:
        try:
            messages.append(subscription.queue.get_nowait())
        except asyncio.QueueEmpty:
            return messages


def event_types(messages: list[dict[str, Any]]) -> list[str]:
    return [message["type"] for message in messages]


async def wait_for_flow_state(
    manager: YandexAuthManager, flow_id: str, expected: str, timeout: float = 6.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = await manager.device_status(flow_id)
        if status.state == expected:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"flow {flow_id} did not reach {expected}")


# --- real manager: device flow ---------------------------------------------


async def test_device_flow_pending_then_authorized(tmp_path: Path) -> None:
    factory = FakeFactory()
    factory.poll_results = [None, FakeOAuthToken()]
    bus = EventBus()
    subscription = bus.subscribe()
    manager = make_manager(tmp_path, bus, factory)

    started = await manager.start_device_flow()

    assert started.user_code == "ABCD-EFGH"
    assert started.verification_url == "https://ya.ru/device"
    assert started.expires_in == 30
    assert started.interval == 1
    assert started.qr_data_uri is not None
    assert started.qr_data_uri.startswith(QR_PREFIX)
    assert DEVICE_CODE not in started.model_dump_json()
    assert factory.device_requests == [("MusicNX-test", "23cabbbdc6cd418abb4b39c32c41195d")]

    pending = await manager.device_status(started.flow_id)
    assert pending.state == "pending"
    assert DEVICE_CODE not in pending.model_dump_json()

    await wait_for_flow_state(manager, started.flow_id, "authorized")
    authorized = await manager.device_status(started.flow_id)
    assert authorized.login == "mock-user"
    assert authorized.error is None

    token = TokenStore(tmp_path / "secrets" / "yandex_token.json").load()
    assert token is not None
    assert token.access_token == "device-access-secret"
    assert token.source == "device"
    assert token.login == "mock-user"

    messages = drain(subscription)
    status_events = [m for m in messages if m["type"] == "auth.status"]
    assert status_events
    payload = status_events[-1]["payload"]
    assert set(payload) == {"authenticated", "login", "expires_at", "source"}
    assert payload["authenticated"] is True
    assert payload["source"] == "device"
    assert "device-access-secret" not in json.dumps(messages)
    assert DEVICE_CODE not in json.dumps(messages)


async def test_device_flow_denied_publishes_auth_required(tmp_path: Path) -> None:
    factory = FakeFactory()
    factory.poll_error = DeviceAuthError("access_denied: user rejected")
    bus = EventBus()
    subscription = bus.subscribe()
    manager = make_manager(tmp_path, bus, factory)

    started = await manager.start_device_flow()
    await wait_for_flow_state(manager, started.flow_id, "denied")

    status = await manager.device_status(started.flow_id)
    assert status.error == "access_denied"
    assert status.login is None
    assert TokenStore(tmp_path / "secrets" / "yandex_token.json").load() is None

    messages = drain(subscription)
    required = [m for m in messages if m["type"] == "auth.required"]
    assert required
    assert required[-1]["payload"] == {"reason": "device_flow_denied"}
    assert "access_denied" not in json.dumps(required)


async def test_device_flow_expires_and_requires_auth(tmp_path: Path) -> None:
    factory = FakeFactory()
    factory.device_code = FakeDeviceCode(expires_in=1, interval=1)
    bus = EventBus()
    subscription = bus.subscribe()
    manager = make_manager(tmp_path, bus, factory)

    started = await manager.start_device_flow()
    await wait_for_flow_state(manager, started.flow_id, "expired")

    status = await manager.device_status(started.flow_id)
    assert status.error == "expired_token"
    assert TokenStore(tmp_path / "secrets" / "yandex_token.json").load() is None
    required = [m for m in drain(subscription) if m["type"] == "auth.required"]
    assert required[-1]["payload"] == {"reason": "device_flow_expired"}


async def test_device_start_failure_is_503_without_leaking(tmp_path: Path) -> None:
    factory = FakeFactory()
    factory.device_error = NetworkError("network down")
    manager = make_manager(tmp_path, EventBus(), factory)

    with pytest.raises(UnavailableError) as info:
        await manager.start_device_flow()

    assert info.value.status_code == 503
    assert "network down" not in str(info.value)


async def test_unknown_flow_is_not_found(tmp_path: Path) -> None:
    manager = make_manager(tmp_path, EventBus(), FakeFactory())

    with pytest.raises(NotFoundError):
        await manager.device_status("missing-flow")


# --- real manager: manual token --------------------------------------------


async def test_manual_token_persists_login_and_status(tmp_path: Path) -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    factory = FakeFactory()
    manager = make_manager(tmp_path, bus, factory)

    status = await manager.set_manual_token(ACCESS_TOKEN)

    assert status.authenticated is True
    assert status.login == "mock-user"
    assert status.source == "manual"
    assert status.expires_at is None
    assert ACCESS_TOKEN not in status.model_dump_json()

    stored = TokenStore(tmp_path / "secrets" / "yandex_token.json").load()
    assert stored is not None
    assert stored.access_token == ACCESS_TOKEN
    assert stored.refresh_token is None
    assert stored.source == "manual"
    assert stored.login == "mock-user"
    assert factory.init_tokens == [ACCESS_TOKEN]

    messages = drain(subscription)
    assert "auth.status" in event_types(messages)
    assert ACCESS_TOKEN not in json.dumps(messages)


async def test_invalid_manual_token_is_401_and_not_persisted(tmp_path: Path) -> None:
    factory = FakeFactory()
    factory.init_errors = [UnauthorizedError("401 Unauthorized")]
    manager = make_manager(tmp_path, EventBus(), factory)

    with pytest.raises(AuthenticationError) as info:
        await manager.set_manual_token("bad-token-value")

    assert info.value.status_code == 401
    assert info.value.code == "invalid_token"
    assert "bad-token-value" not in str(info.value)
    assert "bad-token-value" not in repr(info.value)
    assert TokenStore(tmp_path / "secrets" / "yandex_token.json").load() is None


async def test_empty_manual_token_is_rejected(tmp_path: Path) -> None:
    manager = make_manager(tmp_path, EventBus(), FakeFactory())

    with pytest.raises(AuthenticationError):
        await manager.set_manual_token("   ")


async def test_status_without_token_has_no_token_fields(tmp_path: Path) -> None:
    manager = make_manager(tmp_path, EventBus(), FakeFactory())

    status = await manager.status()

    assert status.authenticated is False
    assert set(status.model_dump()) == {"authenticated", "login", "expires_at", "source"}
    assert "token" not in status.model_dump_json().lower()


# --- real manager: refresh and client lifecycle ----------------------------


def store_expired_token(
    tmp_path: Path,
    *,
    access_token: str = "stale-access-secret",
    refresh_token: str | None = REFRESH_TOKEN,
    expires_in: int | None = 10,
) -> TokenStore:
    store = TokenStore(tmp_path / "secrets" / "yandex_token.json")
    store.save(
        TokenData(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
            obtained_at=time.time() - 600,
            login="user@example",
            source="device",
        )
    )
    return store


async def test_status_hides_details_for_unrefreshable_expired_token(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "secrets" / "yandex_token.json")
    store.save(
        TokenData(
            access_token="expired-access-secret",
            refresh_token=None,
            expires_in=10,
            obtained_at=time.time() - 600,
            login="user@example",
            source="manual",
        )
    )
    manager = make_manager(tmp_path, EventBus(), FakeFactory())

    status = await manager.status()

    assert status.authenticated is False
    assert status.login is None
    assert status.expires_at is None
    assert status.source is None


async def test_get_client_refreshes_proactively(tmp_path: Path, monkeypatch) -> None:
    store = store_expired_token(tmp_path)
    factory = FakeFactory()
    bus = EventBus()
    subscription = bus.subscribe()
    manager = make_manager(tmp_path, bus, factory)

    async def fake_refresh(refresh_token: str) -> dict[str, Any]:
        assert refresh_token == REFRESH_TOKEN
        return {
            "access_token": "refreshed-access-secret",
            "refresh_token": "rotated-refresh-secret",
            "expires_in": 3600,
            "token_type": "bearer",
        }

    monkeypatch.setattr(manager, "_post_refresh", fake_refresh)

    client = await manager.get_client()

    assert client is factory.created[-1]
    assert factory.init_tokens == ["refreshed-access-secret"]
    refreshed = store.load()
    assert refreshed is not None
    assert refreshed.access_token == "refreshed-access-secret"
    assert refreshed.refresh_token == "rotated-refresh-secret"
    assert "auth.status" in event_types(drain(subscription))


async def test_get_client_refresh_failure_publishes_auth_required(
    tmp_path: Path, monkeypatch
) -> None:
    store_expired_token(tmp_path)
    factory = FakeFactory()
    bus = EventBus()
    subscription = bus.subscribe()
    manager = make_manager(tmp_path, bus, factory)

    async def failing_refresh(refresh_token: str) -> dict[str, Any]:
        raise RuntimeError("oauth endpoint is down")

    monkeypatch.setattr(manager, "_post_refresh", failing_refresh)

    with pytest.raises(AuthenticationError) as info:
        await manager.get_client()

    assert info.value.status_code == 401
    assert factory.created == []
    messages = drain(subscription)
    required = [m for m in messages if m["type"] == "auth.required"]
    assert required[-1]["payload"] == {"reason": "refresh_failed"}
    assert ACCESS_TOKEN not in json.dumps(messages)
    assert REFRESH_TOKEN not in json.dumps(messages)


async def test_get_client_retries_once_after_401(tmp_path: Path, monkeypatch) -> None:
    store = TokenStore(tmp_path / "secrets" / "yandex_token.json")
    store.save(
        TokenData(
            access_token="stale-access-secret",
            refresh_token=REFRESH_TOKEN,
            expires_in=3600,
            obtained_at=time.time(),
            login="user@example",
            source="device",
        )
    )
    factory = FakeFactory()
    factory.init_errors = [UnauthorizedError("401 Unauthorized")]
    manager = make_manager(tmp_path, EventBus(), factory)
    refresh_calls: list[str] = []

    async def fake_refresh(refresh_token: str) -> dict[str, Any]:
        refresh_calls.append(refresh_token)
        return {
            "access_token": "retried-access-secret",
            "refresh_token": "rotated-refresh-secret",
            "expires_in": 3600,
        }

    monkeypatch.setattr(manager, "_post_refresh", fake_refresh)

    client = await manager.get_client()

    assert refresh_calls == [REFRESH_TOKEN]
    assert factory.init_tokens == ["stale-access-secret", "retried-access-secret"]
    assert client.token == "retried-access-secret"


async def test_get_client_without_token_requires_auth(tmp_path: Path) -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    manager = make_manager(tmp_path, bus, FakeFactory())

    with pytest.raises(AuthenticationError):
        await manager.get_client()

    required = [m for m in drain(subscription) if m["type"] == "auth.required"]
    assert required[-1]["payload"] == {"reason": "no_token"}


async def test_concurrent_get_client_refreshes_once(tmp_path: Path, monkeypatch) -> None:
    store_expired_token(tmp_path)
    factory = FakeFactory()
    manager = make_manager(tmp_path, EventBus(), factory)
    refresh_calls: list[str] = []

    async def slow_refresh(refresh_token: str) -> dict[str, Any]:
        refresh_calls.append(refresh_token)
        await asyncio.sleep(0.01)
        return {"access_token": "shared-access-secret", "expires_in": 3600}

    monkeypatch.setattr(manager, "_post_refresh", slow_refresh)

    await asyncio.gather(manager.get_client(), manager.get_client(), manager.get_client())

    assert refresh_calls == [REFRESH_TOKEN]


async def test_logout_clears_token_and_publishes_status(tmp_path: Path) -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    manager = make_manager(tmp_path, bus, FakeFactory())
    await manager.set_manual_token(ACCESS_TOKEN)

    await manager.logout()

    assert TokenStore(tmp_path / "secrets" / "yandex_token.json").load() is None
    status = await manager.status()
    assert status.authenticated is False
    assert status.login is None
    payloads = [m["payload"] for m in drain(subscription) if m["type"] == "auth.status"]
    assert payloads[-1] == {
        "authenticated": False,
        "login": None,
        "expires_at": None,
        "source": None,
    }


async def test_refresh_access_token_hook_rotates_the_grant(tmp_path: Path, monkeypatch) -> None:
    store = TokenStore(tmp_path / "secrets" / "yandex_token.json")
    store.save(
        TokenData(
            access_token="stale-access-secret",
            refresh_token=REFRESH_TOKEN,
            expires_in=3600,
            obtained_at=time.time(),
            login="user@example",
            source="device",
        )
    )
    manager = make_manager(tmp_path, EventBus(), FakeFactory())

    async def fake_refresh(refresh_token: str) -> dict[str, Any]:
        return {"access_token": "hooked-access-secret", "expires_in": 3600}

    monkeypatch.setattr(manager, "_post_refresh", fake_refresh)

    await manager.refresh_access_token()

    refreshed = store.load()
    assert refreshed is not None
    assert refreshed.access_token == "hooked-access-secret"


async def test_refresh_access_token_hook_is_noop_for_manual_token(
    tmp_path: Path, monkeypatch
) -> None:
    manager = make_manager(tmp_path, EventBus(), FakeFactory())
    await manager.set_manual_token(ACCESS_TOKEN)
    calls: list[str] = []

    async def fake_refresh(refresh_token: str) -> dict[str, Any]:
        calls.append(refresh_token)
        return {}

    monkeypatch.setattr(manager, "_post_refresh", fake_refresh)

    await manager.refresh_access_token()

    assert calls == []


async def test_close_blocks_get_client(tmp_path: Path) -> None:
    manager = make_manager(tmp_path, EventBus(), FakeFactory())

    await manager.close()

    with pytest.raises(AuthenticationError):
        await manager.get_client()
    with pytest.raises(NotFoundError):
        await manager.device_status("anything")


# --- mock manager -----------------------------------------------------------


def make_mock_manager(tmp_path: Path, bus: EventBus, **overrides: Any) -> MockYandexAuthManager:
    settings = YandexSettings(token_file=tmp_path / "mock_token.json")
    return MockYandexAuthManager(settings, bus, **overrides)


async def test_mock_device_flow_pending_then_authorized(tmp_path: Path) -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    manager = make_mock_manager(tmp_path, bus)

    started = await manager.start_device_flow()
    assert started.qr_data_uri is not None
    assert started.qr_data_uri.startswith(QR_PREFIX)
    assert "device_code" not in started.model_dump_json()

    pending = await manager.device_status(started.flow_id)
    assert pending.state == "pending"
    assert pending.login is None

    authorized = await manager.device_status(started.flow_id)
    assert authorized.state == "authorized"
    assert authorized.login == "mock-user"

    status = await manager.status()
    assert status.authenticated is True
    assert status.source == "device"
    assert status.expires_at is not None
    assert set(status.model_dump()) == {"authenticated", "login", "expires_at", "source"}

    messages = drain(subscription)
    assert "auth.status" in event_types(messages)
    assert "token" not in json.dumps(messages).lower()


async def test_mock_device_flow_authorizes_after_configured_polls(tmp_path: Path) -> None:
    manager = make_mock_manager(tmp_path, EventBus(), authorized_after_polls=3)
    started = await manager.start_device_flow()

    states = [(await manager.device_status(started.flow_id)).state for _ in range(3)]

    assert states == ["pending", "pending", "authorized"]


async def test_mock_manual_token_and_logout(tmp_path: Path) -> None:
    bus = EventBus()
    manager = make_mock_manager(tmp_path, bus)

    assert (await manager.status()).authenticated is False

    status = await manager.set_manual_token("mock")
    assert status.authenticated is True
    assert status.source == "manual"
    assert status.login == "mock-user"
    assert status.expires_at is None

    with pytest.raises(AuthenticationError):
        await manager.set_manual_token("")

    await manager.logout()
    assert (await manager.status()).authenticated is False


async def test_mock_get_client_is_explicitly_unavailable(tmp_path: Path) -> None:
    manager = make_mock_manager(tmp_path, EventBus())

    with pytest.raises(UnavailableError) as info:
        await manager.get_client()

    assert "mock" in str(info.value).lower()


# --- API routes ------------------------------------------------------------


def build_client(tmp_path: Path, manager: Any, *, pin: str | None = "1234") -> TestClient:
    """Minimal app: yandex auth + PIN login, context swapped for a stub.

    ``create_app`` is not used because the integrator wires the router into the
    real application factory; here only the contract under test is mounted.
    """

    settings: Settings = make_settings(tmp_path, pin=pin)
    app = FastAPI()
    app.state.settings = settings
    app.include_router(yandex_auth_router.router, prefix="/api/v1")
    app.include_router(auth_routes.router, prefix="/api/v1")

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    app.state.ctx = SimpleNamespace(
        settings=settings,
        pin_limiter=PinAttemptLimiter(),
        yandex_auth=manager,
    )
    return TestClient(app)


def login(client: TestClient) -> None:
    response = client.post("/api/v1/auth/pin", json={"pin": "1234"})
    assert response.status_code == 200


@pytest.mark.skip(reason="PIN feature removed")
def test_device_routes_require_pin_then_run_flow(tmp_path: Path) -> None:
    bus = EventBus()
    manager = make_mock_manager(tmp_path, bus)
    client = build_client(tmp_path, manager)
    try:
        anonymous_status = client.get("/api/v1/yandex/auth/status")
        assert anonymous_status.status_code == 200
        assert anonymous_status.json() == {
            "authenticated": False,
            "login": None,
            "expires_at": None,
            "source": None,
        }

        denied = client.post("/api/v1/yandex/auth/device/start")
        assert denied.status_code == 401
        assert denied.json()["error"]["code"] == "auth_required"

        login(client)
        started = client.post("/api/v1/yandex/auth/device/start")
        assert started.status_code == 200
        body = started.json()
        assert set(body) == {
            "flow_id",
            "user_code",
            "verification_url",
            "expires_in",
            "interval",
            "qr_data_uri",
        }
        assert body["qr_data_uri"].startswith(QR_PREFIX)
        assert "device_code" not in started.text

        first = client.get("/api/v1/yandex/auth/device/status", params={"flow_id": body["flow_id"]})
        assert first.status_code == 200
        assert first.json()["state"] == "pending"

        second = client.get(
            "/api/v1/yandex/auth/device/status", params={"flow_id": body["flow_id"]}
        )
        assert second.json()["state"] == "authorized"
        assert second.json()["login"] == "mock-user"

        missing_flow = client.get(
            "/api/v1/yandex/auth/device/status", params={"flow_id": "unknown"}
        )
        assert missing_flow.status_code == 404

        invalid_query = client.get("/api/v1/yandex/auth/device/status", params={"flow_id": ""})
        assert invalid_query.status_code == 422

        logged_out = client.post("/api/v1/yandex/auth/logout")
        assert logged_out.status_code == 200
        assert logged_out.json()["authenticated"] is False
        assert client.get("/api/v1/yandex/auth/status").json()["authenticated"] is False
    finally:
        client.close()


def test_manual_token_route_maps_invalid_token_to_401(tmp_path: Path) -> None:
    factory = FakeFactory()
    factory.init_errors = [UnauthorizedError("401 Unauthorized")]
    settings = YandexSettings(token_file=tmp_path / "secrets" / "yandex_token.json")
    manager = YandexAuthManager(settings, EventBus(), client_factory=factory)
    client = build_client(tmp_path, manager)
    try:
        login(client)
        response = client.post("/api/v1/yandex/auth/token", json={"token": "bad-token-value"})

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_token"
        assert "bad-token-value" not in response.text
        assert TokenStore(settings.token_file).load() is None  # type: ignore[arg-type]
    finally:
        client.close()

def test_real_mode_without_token_starts_and_status_has_no_secret(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, mode="real")
    settings.yandex.enabled = True
    settings.yandex.token_file = tmp_path / "secrets" / "yandex_token.json"
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/api/v1/yandex/auth/status")
        assert response.status_code == 200
        payload = response.json()
        assert payload["authenticated"] is False
        assert "access_token" not in response.text
        assert "refresh_token" not in response.text
        assert client.get("/api/v1/yandex/likes").status_code == 401
