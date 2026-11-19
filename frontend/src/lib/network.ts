/**
 * P8 Network / Wi-Fi REST client, defensive normalisers and pure helpers —
 * mirrors docs/API.md §13.
 *
 * `GET /network/status` is open; `POST /network/scan|connect|disconnect` are
 * PIN/localhost protected. Mutations ride the HttpOnly `musicnx_session` cookie
 * opened by `POST /auth/pin`; the browser holds neither a PIN nor a Wi-Fi
 * password. The password exists only inside one `connect` request body: it is
 * never stored in a store, never logged and never returned by the server.
 *
 * A backend without P8 answers `404`; callers surface that honestly instead of
 * drawing a fake network state (AGENTS.md §12). `backend` is `mock` or `nmcli`;
 * a `mock` adapter is labelled as virtual, never as real Wi-Fi.
 *
 * Normalisation is intentionally conservative: a field the server did not send
 * stays `null` (or `false` for `connected`) instead of being invented. `null`
 * signal/interface/SSID render as «—» / «не сообщено».
 */
import { ApiError } from './api';

export { ApiError };

export const API_BASE = '/api/v1';

/** Maximum SSID length in bytes; the UI counts characters (ASCII-safe values). */
export const SSID_MAX_LENGTH = 32;
/** Maximum Wi-Fi password length accepted by the current P8 route. */
export const PASSWORD_MAX_LENGTH = 128;

/** `NetworkStatus` from docs/API.md §13 after normalisation. */
export interface NetworkStatus {
  /** Adapter that answered: `mock` | `nmcli` | another build-specific name. */
  backend: string | null;
  /** Only an explicit `true` counts as connected; anything else reads as no. */
  connected: boolean;
  interface: string | null;
  ssid: string | null;
  ip: string | null;
  /** Signal strength in percent; null when the server did not report it. */
  signal: number | null;
  /** Internet reachability; null when the backend did not report the flag. */
  internet: boolean | null;
  /** Server explanation, e.g. why NetworkManager is unavailable. */
  message: string | null;
}

/** One scanned network (`WifiNetwork`); never contains a password. */
export interface WifiNetwork {
  ssid: string;
  signal: number | null;
  security: string | null;
  connected: boolean;
}

/** Body of `POST /network/scan` after normalisation. */
export interface WifiScanPage {
  /** Connected network first, then strongest signal, then SSID. */
  items: WifiNetwork[];
  total: number;
  /** Backend that produced the scan; null when not reported. */
  backend: string | null;
}

/** Resolves a PIN, or null when the user cancelled the prompt. */
export type NetworkPinPrompt = () => Promise<string | null>;

function asRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function trimmedString(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const text = value.trim();
  return text === '' ? null : text;
}

function toFiniteNumber(value: unknown): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function toNonNegativeInt(value: unknown): number | null {
  const parsed = toFiniteNumber(value);
  if (parsed === null) return null;
  return Math.max(0, Math.round(parsed));
}

/** Signal in 0..100; a malformed/absent value stays null rather than 0. */
export function normalizeSignal(value: unknown): number | null {
  const parsed = toFiniteNumber(value);
  if (parsed === null) return null;
  return Math.min(100, Math.max(0, Math.round(parsed)));
}

/**
 * Normalises `GET /network/status`. Null means the body was not an object at
 * all and cannot be interpreted — the caller reports an invalid response
 * instead of showing an empty «connected: false» card.
 */
export function normalizeNetworkStatus(raw: unknown): NetworkStatus | null {
  const record = asRecord(raw);
  if (record === null) return null;
  return {
    backend: trimmedString(record.backend),
    connected: record.connected === true,
    interface: trimmedString(record.interface),
    ssid: trimmedString(record.ssid),
    ip: trimmedString(record.ip),
    signal: normalizeSignal(record.signal),
    internet: typeof record.internet === 'boolean' ? record.internet : null,
    message: trimmedString(record.message),
  };
}

/** One scan row; a blank/absent SSID is not a network and is skipped. */
export function normalizeWifiNetwork(raw: unknown): WifiNetwork | null {
  const record = asRecord(raw);
  if (record === null) return null;
  // Preserve the SSID exactly: leading/trailing spaces can be part of a name.
  if (typeof record.ssid !== 'string' || record.ssid.trim() === '') return null;
  return {
    ssid: record.ssid,
    signal: normalizeSignal(record.signal),
    security: trimmedString(record.security),
    connected: record.connected === true,
  };
}

function strongerSignal(left: number | null, right: number | null): number | null {
  if (left === null) return right;
  if (right === null) return left;
  return Math.max(left, right);
}

function compareNetworks(left: WifiNetwork, right: WifiNetwork): number {
  if (left.connected !== right.connected) return left.connected ? -1 : 1;
  const leftSignal = left.signal ?? -1;
  const rightSignal = right.signal ?? -1;
  if (leftSignal !== rightSignal) return rightSignal - leftSignal;
  return left.ssid.localeCompare(right.ssid);
}

/**
 * Normalises `POST /network/scan`, also tolerating a bare array body. Duplicate
 * SSIDs are merged (strongest signal, protected/connected flags kept) so the
 * list is safe to key by SSID and never renders the same network twice.
 */
export function normalizeWifiScanPage(raw: unknown): WifiScanPage {
  const arrayBody = Array.isArray(raw) ? raw : null;
  const record = asRecord(raw);
  const rawItems = arrayBody ?? (Array.isArray(record?.items) ? record.items : []);

  const bySsid = new Map<string, WifiNetwork>();
  for (const item of rawItems) {
    const network = normalizeWifiNetwork(item);
    if (network === null) continue;
    const existing = bySsid.get(network.ssid);
    if (existing === undefined) {
      bySsid.set(network.ssid, network);
      continue;
    }
    bySsid.set(network.ssid, {
      ssid: network.ssid,
      signal: strongerSignal(existing.signal, network.signal),
      security: network.security ?? existing.security,
      connected: existing.connected || network.connected,
    });
  }

  const items = [...bySsid.values()].sort(compareNetworks);
  const reportedTotal = toNonNegativeInt(record?.total);
  return {
    items,
    total: Math.max(reportedTotal ?? items.length, items.length),
    backend: trimmedString(record?.backend),
  };
}

/** True when a scan body is an object/array and can be normalised at all. */
export function isWifiScanPayload(raw: unknown): boolean {
  return Array.isArray(raw) || asRecord(raw) !== null;
}

async function parseError(response: Response): Promise<ApiError> {
  let message = `Ошибка запроса (HTTP ${response.status})`;
  let code = 'http_error';
  try {
    const body = (await response.json()) as { error?: { code?: string; message?: string } };
    if (body.error?.message) message = body.error.message;
    if (body.error?.code) code = body.error.code;
  } catch {
    // Non-JSON error body: keep the HTTP status message.
  }
  return new ApiError(message, code, response.status);
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set('Accept', 'application/json');
  if (init.body !== undefined) headers.set('Content-Type', 'application/json');

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers,
      // Mutations ride the HttpOnly PIN session cookie; never a token.
      credentials: 'same-origin',
    });
  } catch {
    throw new ApiError('Сервер MusicNX недоступен: проверьте соединение.', 'network_error', 0);
  }

  if (!response.ok) throw await parseError(response);
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  if (text.trim() === '') {
    throw new ApiError('Сервер вернул пустой ответ раздела сети.', 'invalid_response', response.status);
  }
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new ApiError(
      'Сервер вернул некорректный JSON в ответе раздела сети.',
      'invalid_response',
      response.status,
    );
  }
}

export const networkApi = {
  /** `GET /network/status` — open; the only source of the displayed state. */
  async status(): Promise<NetworkStatus> {
    const raw = await request<unknown>('/network/status');
    const status = normalizeNetworkStatus(raw);
    if (status === null) {
      throw new ApiError(
        'Сервер вернул некорректный статус сети.',
        'invalid_response',
        200,
      );
    }
    return status;
  },

  /** `POST /network/scan` — PIN/localhost protected; no passwords in the answer. */
  async scan(): Promise<WifiScanPage> {
    const raw = await request<unknown>('/network/scan', { method: 'POST', body: '{}' });
    if (!isWifiScanPayload(raw)) {
      throw new ApiError(
        'Сервер вернул некорректный список сетей.',
        'invalid_response',
        200,
      );
    }
    return normalizeWifiScanPage(raw);
  },

  /**
   * `POST /network/connect` — PIN/localhost protected. The password travels
   * only in this request body: never log it, never keep it in a store, never
   * echo it into an error message. The server answers with `NetworkStatus`.
   */
  async connect(ssid: string, password: string): Promise<NetworkStatus> {
    const raw = await request<unknown>('/network/connect', {
      method: 'POST',
      body: JSON.stringify({ ssid, password }),
    });
    const status = normalizeNetworkStatus(raw);
    if (status === null) {
      throw new ApiError(
        'Сервер не вернул состояние сети после подключения.',
        'invalid_response',
        200,
      );
    }
    return status;
  },

  /** `POST /network/disconnect` — PIN/localhost protected → `NetworkStatus`. */
  async disconnect(): Promise<NetworkStatus> {
    const raw = await request<unknown>('/network/disconnect', { method: 'POST', body: '{}' });
    const status = normalizeNetworkStatus(raw);
    if (status === null) {
      throw new ApiError(
        'Сервер не вернул состояние сети после отключения.',
        'invalid_response',
        200,
      );
    }
    return status;
  },

  /** `POST /auth/pin` — opens the HttpOnly session cookie used by mutations. */
  async loginPin(pin: string): Promise<void> {
    await request<{ authenticated?: boolean }>('/auth/pin', {
      method: 'POST',
      body: JSON.stringify({ pin }),
    });
  },
};

/** Human-readable network backend label; mock is always marked as virtual. */
export function networkBackendLabel(backend: string | null): string {
  if (backend === null) return 'backend не сообщён';
  const normalized = backend.trim().toLowerCase();
  if (normalized === 'mock') return 'mock (без железа)';
  if (normalized === 'nmcli') return 'NetworkManager (nmcli)';
  return backend.trim();
}

/** True only for an explicitly reported `mock` adapter. */
export function isMockNetworkBackend(backend: string | null): boolean {
  return backend !== null && backend.trim().toLowerCase() === 'mock';
}

/** `78 %`; `—` when the server did not report a signal. */
export function wifiSignalText(signal: number | null): string {
  if (signal === null || !Number.isFinite(signal)) return '—';
  return `${Math.round(signal)} %`;
}

/** Number of filled signal bars (0..4); 0 also means "not reported". */
export function signalLevel(signal: number | null): number {
  if (signal === null || !Number.isFinite(signal) || signal <= 0) return 0;
  if (signal >= 75) return 4;
  if (signal >= 50) return 3;
  if (signal >= 25) return 2;
  return 1;
}

/** Textual strength so signal is never communicated by colour alone. */
export function signalStrengthLabel(signal: number | null): string {
  switch (signalLevel(signal)) {
    case 4:
      return 'отличный';
    case 3:
      return 'хороший';
    case 2:
      return 'средний';
    case 1:
      return 'слабый';
    default:
      return 'не сообщён';
  }
}

/** Russian security label; an empty/`open` value reads as an open network. */
export function wifiSecurityLabel(security: string | null): string {
  if (security === null) return 'открытая';
  const text = security.trim();
  if (text === '') return 'открытая';
  const normalized = text.toLowerCase();
  if (normalized === 'open' || normalized === 'none' || normalized === '--') return 'открытая';
  return text;
}

/**
 * True only when the scan said the network is protected. Unknown security is
 * *not* treated as protected, so a manually typed hidden network can be tried
 * without a client-side password requirement.
 */
export function securityRequiresPassword(security: string | null): boolean {
  if (security === null) return false;
  const normalized = security.trim().toLowerCase();
  if (normalized === '' || normalized === 'open' || normalized === 'none' || normalized === '--') {
    return false;
  }
  return true;
}

/** Trimmed SSID for the request; an SSID itself may contain inner spaces. */
export function normalizeSsid(text: string): string {
  return text.trim();
}

/** Validation message for the SSID field, or null when it can be sent. */
export function ssidError(text: string): string | null {
  const ssid = normalizeSsid(text);
  if (ssid === '') return 'Укажите имя сети (SSID).';
  if (ssid.length > SSID_MAX_LENGTH) {
    return `SSID длиннее ${SSID_MAX_LENGTH} символов — Wi-Fi такое имя не поддерживает.`;
  }
  if (/[\r\n\t]/.test(ssid)) return 'SSID не должен содержать переводы строк и табуляцию.';
  return null;
}

/**
 * Validation message for the password field, or null when it can be sent.
 *
 * Empty is a valid value for an open network (the P8 route accepts it); a
 * password is required only when the scan explicitly marked the network as
 * protected. Unknown security (e.g. a manually typed hidden SSID) is not
 * treated as protected — the server gives the authoritative answer.
 */
export function passwordError(password: string, security: string | null): string | null {
  if (password.length > PASSWORD_MAX_LENGTH) {
    return `Пароль длиннее ${PASSWORD_MAX_LENGTH} символов — сервер такое значение отклонит.`;
  }
  if (password === '' && securityRequiresPassword(security)) {
    return 'Сеть защищена — введите пароль.';
  }
  return null;
}

/**
 * Codes that must not open a PIN dialog: an Origin/Host rejection cannot be
 * fixed by a PIN, `pin_not_configured` means the server has no PIN at all, and
 * the login-stage codes are reported by the dialog itself.
 */
const PIN_EXEMPT_CODES: ReadonlySet<string> = new Set([
  'origin_forbidden',
  'host_forbidden',
  'pin_not_configured',
  'invalid_pin',
  'too_many_attempts',
  'network_error',
  'validation_error',
]);

/** True when a mutation answered "PIN needed" and a PIN entry can help. */
export function isNetworkPinRequired(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  if (error.status !== 401 && error.status !== 403) return false;
  return !PIN_EXEMPT_CODES.has(error.code);
}

/** True for failures of `POST /auth/pin` that a new PIN could fix. */
export function isRetryableNetworkPinError(error: unknown): boolean {
  return error instanceof ApiError && error.code === 'invalid_pin';
}

/** A `404` on `/network/*` means this backend build has no P8 network routes. */
export function isNetworkRouteMissing(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

/** Thrown when the user closed the PIN prompt: the mutation was not replayed. */
export class NetworkPinCancelledError extends Error {
  constructor(message = 'PIN input was cancelled') {
    super(message);
    this.name = 'NetworkPinCancelledError';
  }
}

/**
 * Runs a PIN-protected mutation. On `401 auth_required` / `403 pin_required`
 * the prompt supplies a PIN, `POST /auth/pin` opens the session cookie and the
 * exact same request is replayed once. Login failures (`invalid_pin`, …) and a
 * cancelled prompt surface as errors; the caller decides whether to ask again.
 */
export async function withNetworkPinRetry<T>(
  run: () => Promise<T>,
  askPin?: NetworkPinPrompt | null,
): Promise<T> {
  try {
    return await run();
  } catch (error) {
    if (!isNetworkPinRequired(error) || !askPin) throw error;
    const pin = await askPin();
    if (pin === null || pin.trim() === '') throw new NetworkPinCancelledError();
    // Wrong PIN / rate limit / no PIN configured throws here, before any replay.
    await networkApi.loginPin(pin);
    return run();
  }
}

function pinErrorText(error: ApiError): string {
  switch (error.code) {
    case 'pin_required':
      return 'Управление сетью с другого устройства требует PIN. Задайте security.pin в config.yaml и перезапустите сервер.';
    case 'auth_required':
      return 'Действие требует PIN-код: введите PIN и повторите.';
    case 'invalid_pin':
      return 'Неверный PIN.';
    case 'pin_not_configured':
      return 'PIN на сервере не настроен: задайте security.pin в config.yaml.';
    case 'too_many_attempts':
      return 'Слишком много попыток ввода PIN. Подождите минуту и повторите.';
    case 'origin_forbidden':
    case 'host_forbidden':
      return 'Сервер отклонил запрос: недопустимый Origin/Host. Откройте MusicNX по адресу сервера.';
    default:
      return error.message;
  }
}

/** Human-readable message for any network API failure (never includes secrets). */
export function describeNetworkError(error: unknown): string {
  if (error instanceof NetworkPinCancelledError) {
    return 'Ввод PIN отменён — действие с сетью не выполнено.';
  }
  if (error instanceof ApiError) {
    if (error.status === 404) {
      return 'Сервер не поддерживает управление сетью: маршрут /api/v1/network/* вернул 404. Пока P8 backend не включён, Wi-Fi настраивается на самой плате.';
    }
    if (error.status === 401 || error.status === 403) return pinErrorText(error);
    switch (error.code) {
      case 'validation_error':
        return 'Сервер отклонил параметры подключения: проверьте SSID (1–32 символа) и пароль (непустой, до 128 символов).';
      case 'unavailable':
        return 'Управление сетью недоступно на сервере: NetworkManager не запущен или адаптер занят.';
      case 'network_error':
        return 'Сервер MusicNX не отвечает: проверьте соединение.';
      case 'invalid_response':
        return 'Сервер вернул некорректный ответ раздела сети.';
      case 'method_not_allowed':
        return 'Сервер не принимает этот запрос управления сетью (405): маршрут не реализован в этой сборке.';
      default:
        return error.message;
    }
  }
  return 'Не удалось выполнить действие с сетью.';
}
