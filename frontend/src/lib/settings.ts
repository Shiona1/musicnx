/**
 * P8 settings + service REST client, defensive normalisers and pure helpers —
 * mirrors docs/API.md §12 (settings, backups, maintenance).
 *
 * The server owns every value: `GET /settings` (open) returns
 * `{values, editable}` and the UI only edits the keys named in `editable`. The
 * documented whitelist is `player.default_volume` (integer 0..100, applied
 * immediately) and `sync.enabled` (scheduler start/stop). `PUT /settings` is
 * PIN/localhost protected and accepts a *partial* body: the browser rides the
 * HttpOnly session cookie opened by `POST /auth/pin` — it never holds a PIN or
 * a Yandex token, and nothing is persisted in localStorage.
 *
 * A backend without P8 answers `404`: the panel must say so honestly and must
 * not render switches that pretend to work (AGENTS.md §12). A newer backend may
 * return keys this UI has no control for: they are kept in `unknownKeys` and
 * shown read-only instead of being dropped. Values that are not primitives are
 * reported in `unreadableKeys`, never guessed at.
 *
 * The «Сервис» block is gated by `GET /system/backups` (open). Only once that
 * endpoint really answered do the backup list and the PIN-protected backup /
 * maintenance actions appear; the UI never calls a mutation blind.
 */
import { ApiError } from './api';

export { ApiError };

export const API_BASE = '/api/v1';

/** Documented settings whitelist (docs/API.md §12). */
export const VOLUME_KEY = 'player.default_volume';
export const SYNC_ENABLED_KEY = 'sync.enabled';
export const DOWNLOAD_QUALITY_KEY = 'downloads.quality';
/** `player.default_volume` bounds. */
export const VOLUME_MIN = 0;
export const VOLUME_MAX = 100;

export const SETTINGS_KEYS = [VOLUME_KEY, SYNC_ENABLED_KEY, DOWNLOAD_QUALITY_KEY] as const;

export type SettingsKey = (typeof SETTINGS_KEYS)[number];

/** A setting value the UI knows how to render; anything else is reported raw. */
export type SettingValue = number | boolean | string;

export type SettingsValues = Record<string, SettingValue>;

/** Body of `GET /settings` after normalisation. */
export interface SettingsSnapshot {
  values: SettingsValues;
  /** Keys the server allows this client to change. Empty means read-only. */
  editable: string[];
  /** Values returned by a newer backend that this UI has no control for. */
  unknownKeys: string[];
  /** Keys present in the payload whose value could not be read. */
  unreadableKeys: string[];
}

/** Form draft; volume stays text so invalid input is visible, not silently coerced. */
export interface SettingsDraft {
  volumeText: string;
  syncEnabled: boolean;
  /**
   * True once the user flipped the switch. Needed when the server reported no
   * current value: an untouched switch must not send `false` by accident while
   * saving an unrelated key.
   */
  syncTouched?: boolean;
}

/** Result of comparing a draft with the server snapshot. */
export interface SettingsPlan {
  /** Partial PUT body: changed *and* editable keys only. */
  changes: Record<string, number | boolean>;
  changedKeys: string[];
  errors: string[];
}

/** One row of `GET /system/backups` (docs/API.md §12). */
export interface BackupItem {
  name: string;
  size_bytes: number | null;
  created_at: string | null;
  verified: boolean | null;
}

/** Body of `GET /system/backups` after normalisation. */
export interface BackupsPage {
  items: BackupItem[];
  total: number;
  directory: string | null;
}

/** Resolves a PIN, or null when the user cancelled the prompt. */
export type SettingsPinPrompt = () => Promise<string | null>;

const SETTING_LABELS: Record<string, string> = {
  [VOLUME_KEY]: 'Громкость по умолчанию',
  [SYNC_ENABLED_KEY]: 'Фоновая синхронизация',
  [DOWNLOAD_QUALITY_KEY]: 'Качество загрузок',
};

const SETTING_DESCRIPTIONS: Record<string, string> = {
  [VOLUME_KEY]:
    'С какой громкостью плеер стартует и играет после перезапуска. Новое значение применяется сразу.',
  [SYNC_ENABLED_KEY]:
    'Включает или выключает фоновую синхронизацию с Яндекс Музыкой на сервере (расписание стартует или останавливается).',
};

export function isKnownSettingKey(key: string): key is SettingsKey {
  return (SETTINGS_KEYS as readonly string[]).includes(key);
}

/** Russian label for a settings key; unknown keys are shown verbatim. */
export function settingLabel(key: string): string {
  return SETTING_LABELS[key] ?? key;
}

/** One-line explanation for a settings key, or null when unknown. */
export function settingDescription(key: string): string | null {
  return SETTING_DESCRIPTIONS[key] ?? null;
}

/** Human-readable value for the read-only summary. */
export function formatSettingValue(value: SettingValue): string {
  if (typeof value === 'boolean') return value ? 'включено' : 'выключено';
  if (typeof value === 'number') return String(value);
  return value;
}

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

function toIsoString(value: unknown): string | null {
  if (typeof value === 'string') {
    const trimmed = value.trim();
    return trimmed === '' ? null : trimmed;
  }
  if (typeof value === 'number' && Number.isFinite(value)) {
    const date = new Date(value < 1e12 ? value * 1000 : value);
    return Number.isNaN(date.getTime()) ? null : date.toISOString();
  }
  return null;
}

/** Primitive scalar or null when the payload cannot be read honestly. */
export function normalizeSettingValue(value: unknown): SettingValue | null {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (typeof value === 'string') {
    const trimmed = value.trim();
    return trimmed === '' ? null : trimmed;
  }
  return null;
}

function normalizeEditable(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  const keys: string[] = [];
  for (const entry of value) {
    if (typeof entry !== 'string') continue;
    const key = entry.trim();
    if (key === '' || seen.has(key)) continue;
    seen.add(key);
    keys.push(key);
  }
  return keys;
}

function valueEntries(raw: unknown): [string, unknown][] {
  const record = asRecord(raw);
  if (record === null) return [];
  // Fallback shape tolerance: accept a flat `{key: value, …}` body too, but
  // never treat the `editable` / `values` containers as settings themselves.
  return Object.entries(record).filter(([key]) => key !== 'editable' && key !== 'values');
}

/**
 * Normalises `GET /settings`. Missing fields stay empty — nothing is invented,
 * and `editable` defaults to "nothing" so a malformed answer cannot unlock the
 * form.
 */
export function normalizeSettings(raw: unknown): SettingsSnapshot {
  const record = asRecord(raw) ?? {};
  const valuesRecord = asRecord(record.values) ?? asRecord(record.settings);
  const entries = valuesRecord !== null ? Object.entries(valuesRecord) : valueEntries(raw);

  const values: SettingsValues = {};
  const unreadableKeys: string[] = [];
  const unknownKeys: string[] = [];

  for (const [rawKey, rawValue] of entries) {
    const key = rawKey.trim();
    if (key === '') continue;
    const value = normalizeSettingValue(rawValue);
    if (value === null) {
      unreadableKeys.push(key);
      continue;
    }
    values[key] = value;
    if (!isKnownSettingKey(key)) unknownKeys.push(key);
  }

  unreadableKeys.sort();
  unknownKeys.sort();

  return { values, editable: normalizeEditable(record.editable), unknownKeys, unreadableKeys };
}

/**
 * True when a payload looks like a settings snapshot. `PUT /settings` may answer
 * with an empty body or with the updated snapshot; only the latter is adopted,
 * otherwise the caller re-reads `GET /settings` (the server stays the truth).
 */
export function hasSettingsPayload(raw: unknown): boolean {
  const record = asRecord(raw);
  if (record === null) return false;
  return asRecord(record.values) !== null || Array.isArray(record.editable);
}

export function isSettingsEditable(snapshot: SettingsSnapshot | null, key: string): boolean {
  return snapshot !== null && snapshot.editable.includes(key);
}

/** `"70"` → 70; `""`, `"70.5"`, `"-1"`, `"101"` → null (rejected, not clamped). */
export function parseVolume(text: string): number | null {
  const trimmed = text.trim();
  if (!/^\d{1,3}$/.test(trimmed)) return null;
  const parsed = Number(trimmed);
  if (!Number.isInteger(parsed) || parsed < VOLUME_MIN || parsed > VOLUME_MAX) return null;
  return parsed;
}

/** Validation message for a non-empty volume input, or null when it is valid. */
export function volumeError(text: string): string | null {
  const trimmed = text.trim();
  if (trimmed === '') return null;
  if (!/^\d{1,3}$/.test(trimmed)) return 'Громкость — целое число от 0 до 100.';
  const parsed = Number(trimmed);
  if (parsed < VOLUME_MIN || parsed > VOLUME_MAX) {
    return 'Громкость должна быть от 0 до 100.';
  }
  return null;
}

/** Draft initialised from the server snapshot (empty volume when not reported). */
export function draftFromSnapshot(snapshot: SettingsSnapshot | null): SettingsDraft {
  const volume = snapshot?.values[VOLUME_KEY];
  const sync = snapshot?.values[SYNC_ENABLED_KEY];
  return {
    volumeText: typeof volume === 'number' ? String(volume) : '',
    syncEnabled: typeof sync === 'boolean' ? sync : false,
  };
}

function currentNumber(snapshot: SettingsSnapshot, key: string): number | null {
  const value = snapshot.values[key];
  return typeof value === 'number' ? value : null;
}

function currentBoolean(snapshot: SettingsSnapshot, key: string): boolean | null {
  const value = snapshot.values[key];
  return typeof value === 'boolean' ? value : null;
}

/**
 * Builds the partial PUT body. Only keys that are (a) documented, (b) editable
 * on the server and (c) actually changed are sent; invalid input yields errors
 * instead of a silent clamp. A key the server reported with an unreadable value
 * can still be set explicitly — that is the honest repair path.
 */
export function planSettingsSave(
  snapshot: SettingsSnapshot | null,
  draft: SettingsDraft,
): SettingsPlan {
  const errors: string[] = [];
  const changes: Record<string, number | boolean> = {};
  const changedKeys: string[] = [];

  if (snapshot === null) {
    return { changes, changedKeys, errors: ['Настройки ещё не загружены с сервера.'] };
  }

  const editable = new Set(snapshot.editable);

  if (editable.has(VOLUME_KEY)) {
    const current = currentNumber(snapshot, VOLUME_KEY);
    const text = draft.volumeText.trim();
    if (text === '') {
      if (current !== null) errors.push('Укажите громкость от 0 до 100.');
    } else {
      const parsed = parseVolume(text);
      if (parsed === null) {
        errors.push(volumeError(text) ?? 'Громкость — целое число от 0 до 100.');
      } else if (parsed !== current) {
        changes[VOLUME_KEY] = parsed;
        changedKeys.push(VOLUME_KEY);
      }
    }
  }

  if (editable.has(SYNC_ENABLED_KEY)) {
    const current = currentBoolean(snapshot, SYNC_ENABLED_KEY);
    if (current === null) {
      // No server value: only an explicit user flip is sent, never a default.
      if (draft.syncTouched === true) {
        changes[SYNC_ENABLED_KEY] = draft.syncEnabled;
        changedKeys.push(SYNC_ENABLED_KEY);
      }
    } else if (current !== draft.syncEnabled) {
      changes[SYNC_ENABLED_KEY] = draft.syncEnabled;
      changedKeys.push(SYNC_ENABLED_KEY);
    }
  }

  return { changes, changedKeys, errors };
}

/** Newest first; a missing timestamp sorts last, the name is the tie-breaker. */
export function compareBackupsNewestFirst(left: BackupItem, right: BackupItem): number {
  const leftTime = left.created_at === null ? NaN : new Date(left.created_at).getTime();
  const rightTime = right.created_at === null ? NaN : new Date(right.created_at).getTime();
  const leftValid = Number.isFinite(leftTime);
  const rightValid = Number.isFinite(rightTime);
  if (leftValid && rightValid && leftTime !== rightTime) return rightTime - leftTime;
  if (leftValid !== rightValid) return leftValid ? -1 : 1;
  return right.name.localeCompare(left.name);
}

/** One backup row; null when there is no usable name to show. */
export function normalizeBackupItem(raw: unknown): BackupItem | null {
  const record = asRecord(raw);
  if (record === null) return null;
  const name = typeof record.name === 'string' ? record.name.trim() : '';
  if (name === '') return null;
  return {
    name,
    size_bytes: toNonNegativeInt(record.size_bytes),
    created_at: toIsoString(record.created_at),
    verified: typeof record.verified === 'boolean' ? record.verified : null,
  };
}

/** Normalises `GET /system/backups` (also tolerates a bare array body). */
export function normalizeBackupsPage(raw: unknown): BackupsPage {
  const arrayBody = Array.isArray(raw) ? raw : null;
  const record = asRecord(raw);
  const rawItems = arrayBody ?? (Array.isArray(record?.items) ? record.items : []);
  const items = rawItems
    .map((item) => normalizeBackupItem(item))
    .filter((item): item is BackupItem => item !== null)
    .sort(compareBackupsNewestFirst);
  const directory =
    typeof record?.directory === 'string' && record.directory.trim() !== ''
      ? record.directory.trim()
      : null;
  const total = toNonNegativeInt(record?.total) ?? items.length;
  return { items, total: Math.max(total, items.length), directory };
}

/** `29,5 МБ`; `null` means the server did not report a size. */
export function formatBackupSize(bytes: number | null): string {
  if (bytes === null) return 'размер не прислан';
  if (bytes < 1024) return `${bytes} Б`;
  const units = ['КБ', 'МБ', 'ГБ'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const digits = value >= 10 ? 0 : 1;
  return `${value.toFixed(digits).replace('.', ',')} ${units[unit]}`;
}

/** `14.11.2026, 12:00`; falls back to the raw server string when unparsable. */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
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
  // A 202/204 may legally carry an empty body; never turn that into a JSON crash.
  const text = await response.text();
  if (text.trim() === '') return undefined as T;
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new ApiError(
      'Сервер вернул некорректный JSON в ответе настроек.',
      'invalid_response',
      response.status,
    );
  }
}

export const settingsApi = {
  /** `GET /settings` — values plus the list of keys this client may change. */
  async get(): Promise<SettingsSnapshot> {
    return normalizeSettings(await request<unknown>('/settings'));
  },

  /**
   * `PUT /settings` — PIN/localhost protected partial update. Returns the new
   * snapshot when the server echoes one, otherwise null (the caller re-reads).
   */
  async update(
    changes: Record<string, number | boolean | string>,
  ): Promise<SettingsSnapshot | null> {
    const raw = await request<unknown>('/settings', {
      method: 'PUT',
      body: JSON.stringify(changes),
    });
    return hasSettingsPayload(raw) ? normalizeSettings(raw) : null;
  },

  /** `POST /auth/pin` — opens the HttpOnly session cookie used by mutations. */
  async loginPin(pin: string): Promise<void> {
    await request<{ authenticated?: boolean }>('/auth/pin', {
      method: 'POST',
      body: JSON.stringify({ pin }),
    });
  },
};

export const serviceApi = {
  /** `GET /system/backups` — probe for the P8 service API; 404 means "absent". */
  async backups(): Promise<BackupsPage> {
    return normalizeBackupsPage(await request<unknown>('/system/backups'));
  },

  /** `POST /system/backups` — SQLite Backup API, 201 `BackupInfo`; PIN/localhost. */
  async createBackup(): Promise<BackupItem | null> {
    const raw = await request<unknown>('/system/backups', { method: 'POST', body: '{}' });
    const record = asRecord(raw);
    return normalizeBackupItem(record?.backup) ?? normalizeBackupItem(raw);
  },

  /** `POST /system/maintenance` — cleanup + WAL checkpoint + quick_check; PIN/localhost. */
  async maintenance(): Promise<void> {
    await request<unknown>('/system/maintenance', { method: 'POST', body: '{}' });
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
  'invalid_setting',
  'validation_error',
]);

/** True when a mutation answered "PIN needed" and a PIN entry can help. */
export function isSettingsPinRequired(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  if (error.status !== 401 && error.status !== 403) return false;
  return !PIN_EXEMPT_CODES.has(error.code);
}

/** Only a wrong PIN is worth asking again. */
export function isRetryableSettingsPinError(error: unknown): boolean {
  return error instanceof ApiError && error.code === 'invalid_pin';
}

/** A `404` on `/settings` means this backend build has no P8 settings route. */
export function isSettingsRouteMissing(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

/** A `404` on `/system/backups` means this backend build has no service API. */
export function isServiceRouteMissing(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

/** Thrown when the user closed the PIN prompt: the mutation was not replayed. */
export class SettingsPinCancelledError extends Error {
  constructor(message = 'PIN input was cancelled') {
    super(message);
    this.name = 'SettingsPinCancelledError';
  }
}

/**
 * Runs a PIN-protected mutation. On `401 auth_required` / `403 pin_required`
 * the prompt supplies a PIN, `POST /auth/pin` opens the session cookie and the
 * exact same request is replayed once. Login failures (`invalid_pin`, …) and a
 * cancelled prompt surface as errors; the caller decides whether to ask again.
 */
export async function withSettingsPinRetry<T>(
  run: () => Promise<T>,
  askPin?: SettingsPinPrompt | null,
): Promise<T> {
  try {
    return await run();
  } catch (error) {
    if (!isSettingsPinRequired(error) || !askPin) throw error;
    const pin = await askPin();
    if (pin === null || pin.trim() === '') throw new SettingsPinCancelledError();
    // Wrong PIN / rate limit / no PIN configured throws here, before any replay.
    await settingsApi.loginPin(pin);
    return run();
  }
}

function pinErrorText(error: ApiError, scope: 'settings' | 'service'): string {
  const subject = scope === 'settings' ? 'Изменение настроек' : 'Сервисная операция';
  switch (error.code) {
    case 'pin_required':
      return `${subject} с другого устройства требует PIN. Задайте security.pin в config.yaml и перезапустите сервер.`;
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

/** Human-readable message for any settings API failure. */
export function describeSettingsError(error: unknown): string {
  if (error instanceof SettingsPinCancelledError) {
    return 'Действие отменено: PIN не введён, настройки не изменены.';
  }
  if (error instanceof ApiError) {
    if (error.status === 404) {
      return 'Сервер не поддерживает настройки: GET /api/v1/settings вернул 404. Значения не выдумываются — включите P8 backend и повторите.';
    }
    if (error.status === 401 || error.status === 403) return pinErrorText(error, 'settings');
    switch (error.code) {
      case 'invalid_setting':
      case 'validation_error':
        return `Сервер отклонил значение: ${error.message}`;
      case 'network_error':
        return 'Сервер MusicNX не отвечает: проверьте соединение.';
      case 'invalid_response':
        return 'Сервер вернул некорректный ответ настроек.';
      case 'method_not_allowed':
        return 'Сервер не принимает сохранение настроек (405): маршрут PUT /settings не реализован.';
      default:
        return error.message;
    }
  }
  return 'Не удалось выполнить запрос к настройкам.';
}

/** Human-readable message for a backup/maintenance failure. */
export function describeServiceError(error: unknown): string {
  if (error instanceof SettingsPinCancelledError) {
    return 'Действие отменено: PIN не введён, сервисная операция не выполнена.';
  }
  if (error instanceof ApiError) {
    if (error.status === 404) {
      return 'Сервер не поддерживает сервисные операции: GET /api/v1/system/backups вернул 404. Кнопки скрыты, чтобы не выглядеть работающими.';
    }
    if (error.status === 401 || error.status === 403) return pinErrorText(error, 'service');
    switch (error.code) {
      case 'network_error':
        return 'Сервер MusicNX не отвечает: проверьте соединение.';
      case 'invalid_response':
        return 'Сервер вернул некорректный ответ сервисного раздела.';
      case 'backup_failed':
        return `Не удалось создать резервную копию: ${error.message}`;
      case 'maintenance_failed':
        return `Обслуживание завершилось ошибкой: ${error.message}`;
      case 'database_locked':
        return 'База данных занята: повторите через несколько секунд.';
      default:
        return error.message;
    }
  }
  return 'Не удалось выполнить сервисную операцию.';
}
