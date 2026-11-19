/**
 * Zones & Bluetooth (P7) REST client, defensive normalisers and pure view
 * helpers — mirrors docs/API.md §11, docs/AUDIO.md §5–§7 and
 * docs/DATABASE.md §4.8.
 *
 * `GET /api/v1/zones` is open read-only and returns `{items, total}` of
 * `AudioZone` (`local_sink | browser | bt`). `POST /zones/{id}/activate`,
 * `/volume {volume}` and `/offset {offset_ms}` are PIN/localhost protected:
 * the browser never holds a token, it only rides the HttpOnly `musicnx_session`
 * cookie set by `POST /auth/pin`; `withZonesPinRetry` asks for a PIN once, logs
 * in and replays the exact request. Bluetooth is the same shape:
 * `GET /bluetooth/devices` plus PIN-protected `scan | pair | connect | forget`.
 * Scrubbing `scan` and `forget` are real mutations — they are fully implemented
 * here or not rendered at all (AGENTS.md §12).
 *
 * Everything is normalised because the P7 DTO is fresh: known field names are
 * accepted, unknown `kind` values degrade to `unknown`, malformed numbers are
 * dropped (rendered as "сервер не прислал"), and nothing is invented. A
 * `404` on the read routes means the backend has no P7 routes yet; the UI must
 * say so instead of drawing a fake zone list.
 *
 * Client-side persistence: none. No tokens, no localStorage — the server is the
 * single source of truth and every mount/refresh re-reads it.
 */
import { ApiError } from './api';

export { ApiError };

export const API_BASE = '/api/v1';

/** Per-zone volume range (integer 0..100, ADR-010, docs/AUDIO.md §5). */
export const ZONE_VOLUME_MIN = 0;
export const ZONE_VOLUME_MAX = 100;

/** Offset buttons move the zone delay by 50 ms (A2DP latency is 150–300 ms). */
export const ZONE_OFFSET_STEP_MS = 50;

/**
 * UI guard only: an absurd offset is rejected before the request. The server
 * stays the authority — if it validates tighter, its error wins.
 */
export const ZONE_OFFSET_LIMIT_MS = 5_000;

/** `audio_zones.kind` values from docs/DATABASE.md §4.8. */
export const ZONE_KINDS = ['local_sink', 'browser', 'bt'] as const;

export type ZoneKind = (typeof ZONE_KINDS)[number];

/** Unknown kinds from a newer backend are shown honestly, never guessed. */
export type ZoneKindOrUnknown = ZoneKind | 'unknown';

/** One `audio_zones` row as exposed by `GET /zones` (docs/API.md §11). */
export interface AudioZone {
  id: string;
  kind: ZoneKindOrUnknown;
  name: string;
  /** PipeWire sink name when the server knows it (display only). */
  sink_name: string | null;
  /** Null when the server did not send a usable 0..100 volume. */
  volume: number | null;
  /** Per-zone delay in ms; null when the server did not send it. */
  offset_ms: number | null;
  active: boolean;
  available: boolean;
}

/** Body of `GET /api/v1/zones`. */
export interface ZonePage {
  items: AudioZone[];
  total: number;
  backend?: string;
  degraded?: boolean;
}

/** One Bluetooth device as exposed by `GET /bluetooth/devices`. */
export interface BluetoothDevice {
  address: string;
  name: string | null;
  paired: boolean;
  connected: boolean;
}

/** Body of `GET /api/v1/bluetooth/devices`. */
export interface BluetoothDevicePage {
  items: BluetoothDevice[];
  total: number;
  /** `mock | real` — the panel says "без железа" for a mock adapter. */
  backend: string | null;
}

/** Resolves a PIN, or null when the user cancelled the prompt. */
export type ZonesPinPrompt = () => Promise<string | null>;

function asRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
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

function toNonEmptyString(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  return trimmed === '' ? null : trimmed;
}

/** Zone/device identifiers are TEXT in SQLite but may be numeric in JSON. */
function toIdentifier(value: unknown): string | null {
  const text = toNonEmptyString(value);
  if (text !== null) return text;
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  return null;
}

export function isZoneKind(value: unknown): value is ZoneKind {
  return typeof value === 'string' && (ZONE_KINDS as readonly string[]).includes(value);
}

/** Normalises one zone; null when there is no usable id to key the list by. */
export function normalizeAudioZone(raw: unknown): AudioZone | null {
  const record = asRecord(raw);
  if (record === null) return null;
  const id = toIdentifier(record.id);
  if (id === null) return null;

  const volume = toFiniteNumber(record.volume);
  const offset = toFiniteNumber(record.offset_ms);

  return {
    id,
    kind: isZoneKind(record.kind) ? record.kind : 'unknown',
    name: toNonEmptyString(record.name) ?? id,
    sink_name: toNonEmptyString(record.sink_name),
    volume:
      volume === null
        ? null
        : Math.min(ZONE_VOLUME_MAX, Math.max(ZONE_VOLUME_MIN, Math.round(volume))),
    offset_ms: offset === null ? null : Math.round(offset),
    active: record.active === true,
    // `available` defaults to true in the DTO; only an explicit false disables.
    available: record.available !== false,
  };
}

const ZONE_KIND_ORDER: Record<ZoneKindOrUnknown, number> = {
  local_sink: 0,
  bt: 1,
  browser: 2,
  unknown: 3,
};

/** Active first, then available, then jack/USB before BT before the browser. */
export function compareZones(left: AudioZone, right: AudioZone): number {
  if (left.active !== right.active) return left.active ? -1 : 1;
  if (left.available !== right.available) return left.available ? -1 : 1;
  const kindDelta = ZONE_KIND_ORDER[left.kind] - ZONE_KIND_ORDER[right.kind];
  if (kindDelta !== 0) return kindDelta;
  return left.name.localeCompare(right.name, 'ru');
}

/** Normalises `GET /zones` (also tolerates a bare array body). */
export function normalizeZonePage(raw: unknown): ZonePage {
  const body = asRecord(raw);
  const rawItems = Array.isArray(raw) ? raw : Array.isArray(body?.items) ? body.items : [];
  const items = rawItems
    .map((item) => normalizeAudioZone(item))
    .filter((item): item is AudioZone => item !== null)
    .sort(compareZones);
  const total = toNonNegativeInt(body?.total) ?? items.length;
  return {
    items,
    total: Math.max(total, items.length),
    backend: toNonEmptyString(body?.backend) ?? undefined,
    degraded: body?.degraded === true,
  };
}

/** Extracts a single zone from a mutation answer (`{zone}` or the zone itself). */
export function extractAudioZone(raw: unknown): AudioZone | null {
  const record = asRecord(raw);
  if (record === null) return null;
  return normalizeAudioZone(record.zone) ?? normalizeAudioZone(record);
}

/** Normalises one Bluetooth device; the address is the stable identity. */
export function normalizeBluetoothDevice(raw: unknown): BluetoothDevice | null {
  const record = asRecord(raw);
  if (record === null) return null;
  const address = toIdentifier(record.address);
  if (address === null) return null;
  return {
    address,
    name: toNonEmptyString(record.name),
    paired: record.paired === true,
    connected: record.connected === true,
  };
}

/** Connected first, then paired, then by label — the useful order in a list. */
export function compareBluetoothDevices(
  left: BluetoothDevice,
  right: BluetoothDevice,
): number {
  if (left.connected !== right.connected) return left.connected ? -1 : 1;
  if (left.paired !== right.paired) return left.paired ? -1 : 1;
  return bluetoothDeviceLabel(left).localeCompare(bluetoothDeviceLabel(right), 'ru');
}

/** Normalises `GET /bluetooth/devices` (also tolerates a bare array body). */
export function normalizeBluetoothDevicePage(raw: unknown): BluetoothDevicePage {
  const body = asRecord(raw);
  const rawItems = Array.isArray(raw) ? raw : Array.isArray(body?.items) ? body.items : [];
  const items = rawItems
    .map((item) => normalizeBluetoothDevice(item))
    .filter((item): item is BluetoothDevice => item !== null)
    .sort(compareBluetoothDevices);
  const total = toNonNegativeInt(body?.total) ?? items.length;
  const backend = toNonEmptyString(body?.backend) ?? toNonEmptyString(body?.adapter);
  return { items, total: Math.max(total, items.length), backend };
}

/** Extracts a single device from a mutation answer (`{device}` or the device). */
export function extractBluetoothDevice(raw: unknown): BluetoothDevice | null {
  const record = asRecord(raw);
  if (record === null) return null;
  return normalizeBluetoothDevice(record.device) ?? normalizeBluetoothDevice(record);
}

/** Russian labels for the zone kind chip; unknown kinds stay honest. */
export const ZONE_KIND_LABELS: Record<ZoneKindOrUnknown, string> = {
  local_sink: 'Локальный выход',
  bt: 'Bluetooth',
  browser: 'Браузер',
  unknown: 'Неизвестный тип',
};

export function zoneKindLabel(kind: ZoneKindOrUnknown | string): string {
  return isZoneKind(kind) ? ZONE_KIND_LABELS[kind] : (ZONE_KIND_LABELS.unknown as string);
}

/** One-line detail under the kind: what the physical output actually is. */
export function zoneKindHint(kind: ZoneKindOrUnknown | string): string | null {
  switch (kind) {
    case 'local_sink':
      return 'jack 3,5 мм / USB-DAC на Raspberry Pi';
    case 'bt':
      return 'A2DP-устройство';
    case 'browser':
      return 'звук в этой вкладке клиента';
    default:
      return null;
  }
}

/** CSS-safe tone for the zone state chip. */
export type ZoneTone = 'active' | 'available' | 'unavailable';

export function zoneTone(zone: AudioZone): ZoneTone {
  if (zone.active) return 'active';
  return zone.available ? 'available' : 'unavailable';
}

/**
 * Chip text for one zone. An active BT zone can lose its A2DP sink while the
 * server still flags it active (the row is kept), so that state is named
 * explicitly instead of pretending the output works.
 */
export function zoneStateLabel(zone: AudioZone): string {
  if (zone.active) return zone.available ? 'Активна' : 'Активна · нет sink';
  return zone.available ? 'Доступна' : 'Недоступна';
}

/** `mock — без железа` / `bluetoothctl (BlueZ)`; raw value otherwise. */
export function bluetoothBackendLabel(backend: string | null | undefined): string | null {
  if (!backend) return null;
  const key = backend.trim().toLowerCase();
  if (key === 'mock') return 'mock — без железа';
  if (key === 'bluetoothctl' || key === 'bluez') return 'bluetoothctl (BlueZ)';
  if (key === 'real' || key === 'system') return 'реальное железо';
  return backend;
}

/** True when the adapter is a mock: discovery is virtual, no radio is used. */
export function isMockBluetoothBackend(backend: string | null | undefined): boolean {
  return (backend ?? '').trim().toLowerCase() === 'mock';
}

export function bluetoothDeviceLabel(
  device: Pick<BluetoothDevice, 'address' | 'name'>,
): string {
  return device.name ?? device.address;
}

/** `Подключено` / `Сопряжено` / `Не сопряжено`. */
export function bluetoothDeviceState(device: BluetoothDevice): string {
  if (device.connected) return 'Подключено';
  if (device.paired) return 'Сопряжено';
  return 'Не сопряжено';
}

/** 0..100 integer; non-finite input falls back to the minimum (UI guard). */
export function clampZoneVolume(value: number): number {
  if (!Number.isFinite(value)) return ZONE_VOLUME_MIN;
  return Math.min(ZONE_VOLUME_MAX, Math.max(ZONE_VOLUME_MIN, Math.round(value)));
}

/** ±`ZONE_OFFSET_LIMIT_MS`, integer ms (UI guard only). */
export function clampZoneOffset(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.min(ZONE_OFFSET_LIMIT_MS, Math.max(-ZONE_OFFSET_LIMIT_MS, Math.round(value)));
}

export function stepZoneVolume(value: number, delta: number): number {
  return clampZoneVolume((Number.isFinite(value) ? value : ZONE_VOLUME_MIN) + delta);
}

export function stepZoneOffset(value: number, delta: number): number {
  return clampZoneOffset((Number.isFinite(value) ? value : 0) + delta);
}

/** `—` when the server sent no volume, otherwise `72%`. */
export function zoneVolumeText(volume: number | null | undefined): string {
  if (volume === null || volume === undefined) return '—';
  return `${Math.round(volume)}%`;
}

/** `—` when absent, `0 мс`, `+150 мс`, `-150 мс`. */
export function zoneOffsetText(offsetMs: number | null | undefined): string {
  if (offsetMs === null || offsetMs === undefined) return '—';
  const rounded = Math.round(offsetMs);
  return `${rounded > 0 ? '+' : ''}${rounded} мс`;
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
  // A 202 may legally carry an empty body; never turn that into a JSON crash.
  const text = await response.text();
  if (text.trim() === '') return undefined as T;
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new ApiError(
      'Сервер вернул некорректный JSON в ответе раздела зон.',
      'invalid_response',
      response.status,
    );
  }
}

function zonePath(id: string, action: string): string {
  return `/zones/${encodeURIComponent(id)}/${action}`;
}

export const zonesApi = {
  /** `GET /zones` — open, read-only list of audio zones. */
  async list(): Promise<ZonePage> {
    return normalizeZonePage(await request<unknown>('/zones'));
  },

  /** `POST /zones/{id}/activate` — PIN/localhost; moves the mpv sink-input. */
  async activate(id: string): Promise<AudioZone | null> {
    return extractAudioZone(
      await request<unknown>(zonePath(id, 'activate'), { method: 'POST', body: '{}' }),
    );
  },

  /** `POST /zones/{id}/volume {volume}` — PIN/localhost; 0..100 integer. */
  async setVolume(id: string, volume: number): Promise<AudioZone | null> {
    return extractAudioZone(
      await request<unknown>(zonePath(id, 'volume'), {
        method: 'POST',
        body: JSON.stringify({ volume: clampZoneVolume(volume) }),
      }),
    );
  },

  /** `POST /zones/{id}/offset {offset_ms}` — PIN/localhost; per-zone delay. */
  async setOffset(id: string, offsetMs: number): Promise<AudioZone | null> {
    return extractAudioZone(
      await request<unknown>(zonePath(id, 'offset'), {
        method: 'POST',
        body: JSON.stringify({ offset_ms: clampZoneOffset(offsetMs) }),
      }),
    );
  },

  /** `GET /bluetooth/devices` — paired/known devices plus the adapter kind. */
  async devices(): Promise<BluetoothDevicePage> {
    return normalizeBluetoothDevicePage(await request<unknown>('/bluetooth/devices'));
  },

  /** `POST /bluetooth/scan` — PIN/localhost; discovery is asynchronous. */
  async scan(): Promise<BluetoothDevicePage | null> {
    const raw = await request<unknown>('/bluetooth/scan', { method: 'POST', body: '{}' });
    const body = asRecord(raw);
    if (body === null) return null;
    if (!('items' in body) && !('backend' in body)) return null;
    return normalizeBluetoothDevicePage(raw);
  },

  /** `POST /bluetooth/pair {address}` — PIN/localhost. */
  async pair(address: string): Promise<BluetoothDevice | null> {
    return extractBluetoothDevice(
      await request<unknown>('/bluetooth/pair', {
        method: 'POST',
        body: JSON.stringify({ address }),
      }),
    );
  },

  /** `POST /bluetooth/connect {address}` — PIN/localhost; A2DP sink appears. */
  async connect(address: string): Promise<BluetoothDevice | null> {
    return extractBluetoothDevice(
      await request<unknown>('/bluetooth/connect', {
        method: 'POST',
        body: JSON.stringify({ address }),
      }),
    );
  },

  /** `POST /bluetooth/forget {address}` — PIN/localhost; 204 or the device. */
  async forget(address: string): Promise<BluetoothDevice | null> {
    return extractBluetoothDevice(
      await request<unknown>('/bluetooth/forget', {
        method: 'POST',
        body: JSON.stringify({ address }),
      }),
    );
  },

  /** `POST /auth/pin` — opens the HttpOnly session cookie used by mutations. */
  async loginPin(pin: string): Promise<void> {
    await request<{ authenticated?: boolean }>('/auth/pin', {
      method: 'POST',
      body: JSON.stringify({ pin }),
    });
  },
};

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
]);

/** True when a mutation answered "PIN needed" and a PIN entry can help. */
export function isZonesPinRequired(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  if (error.status !== 401 && error.status !== 403) return false;
  return !PIN_EXEMPT_CODES.has(error.code);
}

/** Only a wrong PIN is worth asking again. */
export function isRetryableZonesPinError(error: unknown): boolean {
  return error instanceof ApiError && error.code === 'invalid_pin';
}

/**
 * A `404` on `GET /zones` / `GET /bluetooth/devices` means the backend has no
 * P7 routes yet (the routes have no path parameters, so a domain 404 cannot
 * come from them). Domain misses on mutations (`zone_not_found`, …) stay real
 * errors, see `describeZonesError`.
 */
export function isZonesRouteMissing(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

/** Thrown when the user closed the PIN prompt: the mutation was not replayed. */
export class ZonesPinCancelledError extends Error {
  constructor(message = 'PIN input was cancelled') {
    super(message);
    this.name = 'ZonesPinCancelledError';
  }
}

/**
 * Runs a PIN-protected mutation. On `401 auth_required` / `403 pin_required`
 * the prompt supplies a PIN, `POST /auth/pin` opens the session cookie and the
 * exact same request is replayed once. Login failures (`invalid_pin`, …) and a
 * cancelled prompt surface as errors; the caller decides whether to ask again.
 */
export async function withZonesPinRetry<T>(
  run: () => Promise<T>,
  askPin?: ZonesPinPrompt | null,
): Promise<T> {
  try {
    return await run();
  } catch (error) {
    if (!isZonesPinRequired(error) || !askPin) throw error;
    const pin = await askPin();
    if (pin === null || pin.trim() === '') throw new ZonesPinCancelledError();
    // Wrong PIN / rate limit / no PIN configured throws here, before any replay.
    await zonesApi.loginPin(pin);
    return run();
  }
}

/** Human-readable message for any zones/Bluetooth API failure. */
export function describeZonesError(error: unknown): string {
  if (error instanceof ZonesPinCancelledError) {
    return 'Действие отменено: PIN не введён.';
  }
  if (error instanceof ApiError) {
    switch (error.code) {
      case 'pin_required':
        return 'Управление зонами с другого устройства требует PIN. Задайте security.pin в config.yaml и перезапустите сервер.';
      case 'auth_required':
        return 'Действие требует PIN-код: введите PIN и повторите.';
      case 'invalid_pin':
        return 'Неверный PIN.';
      case 'pin_not_configured':
        return 'PIN на сервере не настроен: задайте security.pin в config.yaml.';
      case 'too_many_attempts':
        return 'Слишком много попыток ввода PIN. Подождите минуту и повторите.';
      case 'validation_error':
        return 'Сервер отклонил значение: громкость — целое 0–100, смещение — в миллисекундах.';
      case 'zone_not_found':
      case 'device_not_found':
      case 'not_found':
        return 'Сервер не нашёл эту зону или устройство: список устарел — обновите его.';
      case 'bt_unavailable':
      case 'bluetooth_unavailable':
      case 'adapter_unavailable':
        return 'Bluetooth-адаптер недоступен на сервере: зоны работают, Bluetooth — нет.';
      case 'unavailable':
        return 'Сервис аудио/Bluetooth на сервере недоступен (503): зона или адаптер не готовы.';
      case 'zone_unavailable':
        return 'Эта зона сейчас недоступна: устройство отключено или sink исчез.';
      case 'network_error':
        return 'Сервер MusicNX не отвечает: проверьте соединение.';
      case 'invalid_response':
        return 'Сервер вернул некорректный ответ раздела зон.';
      default:
        return error.message;
    }
  }
  return 'Не удалось выполнить запрос к разделу зон.';
}
