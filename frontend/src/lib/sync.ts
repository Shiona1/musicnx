/**
 * Sync (P6) REST client, defensive normalisers and pure view helpers —
 * mirrors docs/API.md §10 and docs/SYNC.md.
 *
 * `GET /sync/status` is the single server-owned truth for the Sync Center:
 * `running`, the last `SyncRun`, `next_run_at`, `interval_s` and — while a run
 * is active — `progress`. `GET /sync/history?limit=20` returns the newest runs
 * first. `POST /sync/start` / `POST /sync/cancel` are PIN/localhost protected:
 * the browser never holds a Yandex token, it only rides the HttpOnly
 * `musicnx_session` cookie set by `POST /auth/pin`; `withSyncPinRetry` asks for
 * a PIN once, logs in and replays the exact request.
 *
 * The backend DTO is not fully frozen yet (docs/API.md gives one example and
 * docs/DATABASE.md names snake_case columns), so every payload goes through a
 * normaliser: known shapes (`last`, flat `last_attempt_at`/`counts`, nested
 * `counts.tracks`, flat `tracks_added`, …) are accepted, malformed values are
 * dropped, and nothing is invented. A backend without P6 answers `404`; the UI
 * must say that honestly instead of showing a fake schedule (AGENTS.md §12).
 *
 * Nothing is persisted client-side: no tokens, no localStorage. Every mount
 * re-reads the server state.
 */
import { ApiError } from './api';

export { ApiError };

export const API_BASE = '/api/v1';

/** The UI never asks for more than the documented page size. */
export const SYNC_HISTORY_LIMIT = 20;

/** Run statuses from docs/DATABASE.md §4.9 `sync_runs.status`. */
export const SYNC_RUN_STATUSES = ['running', 'success', 'partial', 'failed', 'cancelled'] as const;

export type SyncRunStatus = (typeof SYNC_RUN_STATUSES)[number];

/** Change counters; `added | updated | removed` as required by the Sync UI. */
export interface SyncCounts {
  added: number;
  updated: number;
  removed: number;
}

/** One `sync_runs` row as exposed by the API (docs/API.md §10). */
export interface SyncRun {
  id: number;
  /** Known status or a raw unknown string; render it honestly. */
  status: string;
  started_at: string | null;
  finished_at: string | null;
  /** Track changes of this run. */
  counts: SyncCounts;
  /** Playlist-level changes when the backend reports them separately. */
  playlist_counts: SyncCounts | null;
  error: string | null;
  /** `manual | hourly | startup` … when the server names the trigger. */
  trigger: string | null;
}

/** Body of `sync.progress` and `status.progress`: `{phase, current, total}`. */
export interface SyncProgress {
  phase: string | null;
  current: number | null;
  total: number | null;
}

/** Body of `GET /sync/status` after normalisation. */
export interface SyncStatus {
  running: boolean;
  progress: SyncProgress | null;
  last: SyncRun | null;
  next_run_at: string | null;
  interval_s: number | null;
  /** Flat counters some backends put on the status instead of `last.counts`. */
  counts: SyncCounts | null;
  last_attempt_at: string | null;
  last_attempt_status: string | null;
  last_success_at: string | null;
  last_error: string | null;
}

/** Body of `GET /sync/history?limit=20`: newest first. */
export interface SyncHistoryPage {
  items: SyncRun[];
  total: number;
}

/** Resolves a PIN, or null when the user cancelled the prompt. */
export type SyncPinPrompt = () => Promise<string | null>;

export const EMPTY_SYNC_COUNTS: SyncCounts = { added: 0, updated: 0, removed: 0 };

/** A fresh zeroed counter object (never share the frozen constant). */
export function emptyCounts(): SyncCounts {
  return { ...EMPTY_SYNC_COUNTS };
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
    // Accept epoch seconds or milliseconds; the backend normally sends ISO.
    const date = new Date(value < 1e12 ? value * 1000 : value);
    return Number.isNaN(date.getTime()) ? null : date.toISOString();
  }
  return null;
}

/**
 * `error` columns are TEXT in SQLite but docs/SYNC.md shows
 * `{"code": "...", "message": "..."}`; accept both without leaking secrets.
 */
export function syncErrorText(value: unknown): string | null {
  if (typeof value === 'string') {
    const trimmed = value.trim();
    return trimmed === '' ? null : trimmed;
  }
  const record = asRecord(value);
  if (record === null) return null;
  if (typeof record.message === 'string' && record.message.trim() !== '') {
    return record.message.trim();
  }
  if (typeof record.code === 'string' && record.code.trim() !== '') {
    return record.code.trim();
  }
  return null;
}

/** Reads `{added, updated, removed}`; returns null when the shape is absent. */
export function normalizeSyncCounts(value: unknown): SyncCounts | null {
  const record = asRecord(value);
  if (record === null) return null;
  const hasAny =
    'added' in record || 'updated' in record || 'removed' in record;
  if (!hasAny) return null;
  return {
    added: toNonNegativeInt(record.added) ?? 0,
    updated: toNonNegativeInt(record.updated) ?? 0,
    removed: toNonNegativeInt(record.removed) ?? 0,
  };
}

/** `{tracks_added, tracks_updated, tracks_removed}` (database-style columns). */
function flatCounts(record: Record<string, unknown>, prefix: string): SyncCounts | null {
  const addedKey = `${prefix}_added`;
  const updatedKey = `${prefix}_updated`;
  const removedKey = `${prefix}_removed`;
  if (!(addedKey in record) && !(updatedKey in record) && !(removedKey in record)) {
    return null;
  }
  return {
    added: toNonNegativeInt(record[addedKey]) ?? 0,
    updated: toNonNegativeInt(record[updatedKey]) ?? 0,
    removed: toNonNegativeInt(record[removedKey]) ?? 0,
  };
}

export function isSyncRunStatus(value: unknown): value is SyncRunStatus {
  return (
    typeof value === 'string' &&
    (SYNC_RUN_STATUSES as readonly string[]).includes(value)
  );
}

/**
 * Normalises one run. Returns null when there is no usable identity: the store
 * keys history by `id`, so a run without a numeric id cannot be upserted.
 */
export function normalizeSyncRun(raw: unknown): SyncRun | null {
  const record = asRecord(raw);
  if (record === null) return null;
  const id = toFiniteNumber(record.id);
  if (id === null || id < 0) return null;

  const nestedCounts = asRecord(record.counts);
  const counts =
    normalizeSyncCounts(nestedCounts?.tracks) ??
    normalizeSyncCounts(nestedCounts) ??
    normalizeSyncCounts(record.tracks) ??
    flatCounts(record, 'tracks') ??
    emptyCounts();
  const playlistCounts =
    normalizeSyncCounts(nestedCounts?.playlists) ??
    normalizeSyncCounts(record.playlists) ??
    flatCounts(record, 'playlists');

  const status = typeof record.status === 'string' && record.status.trim() !== ''
    ? record.status.trim()
    : 'unknown';
  const trigger =
    typeof record.trigger === 'string' && record.trigger.trim() !== ''
      ? record.trigger.trim()
      : typeof record.mode === 'string' && record.mode.trim() !== ''
        ? record.mode.trim()
        : typeof record.reason === 'string' && record.reason.trim() !== ''
          ? record.reason.trim()
          : null;

  return {
    id: Math.round(id),
    status,
    started_at: toIsoString(record.started_at),
    finished_at: toIsoString(record.finished_at),
    counts,
    playlist_counts: playlistCounts,
    error: syncErrorText(record.error),
    trigger,
  };
}

/**
 * `{phase, current, total}` — the shape of WS `sync.progress` and of the
 * status object. The P6 backend also stores a compact human string
 * (`"starting"`, `"playlists 2/5"`), so both forms are accepted: a parsable
 * string feeds the bar, anything else is shown verbatim as the phase.
 */
export function normalizeSyncProgress(raw: unknown): SyncProgress | null {
  if (typeof raw === 'string') {
    const trimmed = raw.trim();
    if (trimmed === '') return null;
    const parsed = /^([a-zA-Z_]+)\s+(\d+)\s*\/\s*(\d+)$/.exec(trimmed);
    if (parsed !== null) {
      return {
        phase: parsed[1],
        current: toNonNegativeInt(parsed[2]),
        total: toNonNegativeInt(parsed[3]),
      };
    }
    return { phase: trimmed, current: null, total: null };
  }
  const record = asRecord(raw);
  if (record === null) return null;
  const phase =
    typeof record.phase === 'string' && record.phase.trim() !== ''
      ? record.phase.trim()
      : null;
  const current = toNonNegativeInt(record.current);
  const total = toNonNegativeInt(record.total);
  if (phase === null && current === null && total === null) return null;
  return { phase, current, total };
}

/** Normalises `GET /sync/status`; missing fields stay null instead of zeroes. */
export function normalizeSyncStatus(raw: unknown): SyncStatus {
  const record = asRecord(raw) ?? {};
  const last =
    normalizeSyncRun(record.last) ??
    normalizeSyncRun(record.last_run) ??
    normalizeSyncRun(record.last_attempt);
  const lastSuccessRun = normalizeSyncRun(record.last_success);
  const flatCountsOnStatus =
    normalizeSyncCounts(asRecord(record.counts)?.tracks) ??
    normalizeSyncCounts(record.counts) ??
    normalizeSyncCounts(record.tracks) ??
    flatCounts(record, 'tracks');
  const lastError =
    syncErrorText(record.last_error) ??
    syncErrorText(record.error) ??
    last?.error ??
    null;

  return {
    running: record.running === true,
    progress: normalizeSyncProgress(record.progress),
    last,
    next_run_at: toIsoString(record.next_run_at),
    interval_s: toNonNegativeInt(record.interval_s),
    counts: flatCountsOnStatus,
    last_attempt_at:
      toIsoString(record.last_attempt_at) ??
      last?.finished_at ??
      last?.started_at ??
      null,
    last_attempt_status:
      typeof record.last_attempt_status === 'string' &&
      record.last_attempt_status.trim() !== ''
        ? record.last_attempt_status.trim()
        : (last?.status ?? null),
    last_success_at:
      toIsoString(record.last_success_at) ??
      lastSuccessRun?.finished_at ??
      lastSuccessRun?.started_at ??
      null,
    last_error: lastError,
  };
}

/** Newest first, id as a stable tie-breaker; invalid timestamps sort last. */
export function compareRunsNewestFirst(left: SyncRun, right: SyncRun): number {
  const leftTime = left.started_at === null ? NaN : new Date(left.started_at).getTime();
  const rightTime = right.started_at === null ? NaN : new Date(right.started_at).getTime();
  const leftValid = Number.isFinite(leftTime);
  const rightValid = Number.isFinite(rightTime);
  if (leftValid && rightValid && leftTime !== rightTime) return rightTime - leftTime;
  if (leftValid !== rightValid) return leftValid ? -1 : 1;
  return right.id - left.id;
}

/** Normalises `GET /sync/history` (also tolerates a bare array body). */
export function normalizeSyncHistory(
  raw: unknown,
  limit = SYNC_HISTORY_LIMIT,
): SyncHistoryPage {
  const arrayBody = Array.isArray(raw) ? raw : null;
  const record = asRecord(raw);
  const rawItems = arrayBody ?? (Array.isArray(record?.items) ? record.items : []);
  const items = rawItems
    .map((item) => normalizeSyncRun(item))
    .filter((item): item is SyncRun => item !== null)
    .sort(compareRunsNewestFirst);
  const capped = items.slice(0, Math.max(1, limit));
  const total = toNonNegativeInt(record?.total) ?? items.length;
  return { items: capped, total: Math.max(total, capped.length) };
}

/**
 * ISO timestamp of the newest *successful* run. `status.last_success_at` wins
 * (the server records the last success separately from the last attempt, see
 * docs/SYNC.md §3); the history is only a fallback for older payloads.
 */
export function latestSuccessAt(
  status: SyncStatus | null,
  history: readonly SyncRun[],
): string | null {
  if (status?.last_success_at) return status.last_success_at;
  for (const run of history) {
    if (run.status === 'success') return run.finished_at ?? run.started_at;
  }
  return null;
}

function parseDate(value: string | null | undefined): Date | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** `14.11.2026, 12:00`; falls back to the raw server string when unparsable. */
export function formatSyncDate(value: string | null | undefined): string {
  if (!value) return '—';
  const date = parseDate(value);
  if (date === null) return value;
  return date.toLocaleString('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

/** `12:00:05` — used for the "обновлено" freshness stamp. */
export function formatClockTime(ms: number): string {
  return new Date(ms).toLocaleTimeString('ru-RU', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

/** `4 с`, `1 мин 12 с`, `2 ч 5 мин` — a compact Russian duration. */
export function formatDurationSeconds(seconds: number): string {
  const total = Math.max(0, Math.round(Number.isFinite(seconds) ? seconds : 0));
  if (total < 60) return `${total} с`;
  const minutes = Math.floor(total / 60);
  const restSeconds = total % 60;
  if (minutes < 60) {
    return restSeconds === 0 ? `${minutes} мин` : `${minutes} мин ${restSeconds} с`;
  }
  const hours = Math.floor(minutes / 60);
  const restMinutes = minutes % 60;
  return restMinutes === 0 ? `${hours} ч` : `${hours} ч ${restMinutes} мин`;
}

/** Duration of a finished run, or null while it is still running. */
export function formatRunDuration(run: SyncRun): string | null {
  const start = parseDate(run.started_at);
  const end = parseDate(run.finished_at);
  if (start === null || end === null) return null;
  return formatDurationSeconds((end.getTime() - start.getTime()) / 1000);
}

/** How long the current run has been going, e.g. `идёт 3 мин`. */
export function formatSyncElapsed(
  startedAt: string | null | undefined,
  nowMs = Date.now(),
): string | null {
  const start = parseDate(startedAt);
  if (start === null) return null;
  return formatDurationSeconds((nowMs - start.getTime()) / 1000);
}

function pluralRu(count: number, one: string, few: string, many: string): string {
  const mod10 = count % 10;
  const mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return few;
  return many;
}

/**
 * Human interval from `interval_s` (3600 → «каждый час»). Null when the server
 * reports no schedule; the UI then says so instead of inventing one.
 */
export function formatIntervalSeconds(seconds: number | null | undefined): string | null {
  if (seconds === null || seconds === undefined) return null;
  if (!Number.isFinite(seconds) || seconds <= 0) return null;
  const rounded = Math.round(seconds);
  if (rounded === 3600) return 'каждый час';
  if (rounded % 3600 === 0) return `каждые ${rounded / 3600} ч`;
  if (rounded === 60) return 'каждую минуту';
  if (rounded % 60 === 0) return `каждые ${rounded / 60} мин`;
  return `каждые ${rounded} с`;
}

/**
 * «через 12 минут» / «5 минут назад» for `nowMs`. Relative labels are only a
 * hint: the absolute timestamp is always rendered next to them.
 */
export function formatRelativeTime(
  value: string | null | undefined,
  nowMs = Date.now(),
): string | null {
  const date = parseDate(value);
  if (date === null) return null;
  const delta = date.getTime() - nowMs;
  const future = delta >= 0;
  const abs = Math.abs(delta);
  if (abs < 45_000) return 'сейчас';
  const label = (amount: number, one: string, few: string, many: string): string =>
    amount === 1 ? one : `${amount} ${pluralRu(amount, one, few, many)}`;
  const suffix = (amount: number, one: string, few: string, many: string): string =>
    future ? `через ${label(amount, one, few, many)}` : `${label(amount, one, few, many)} назад`;
  const minutes = Math.round(abs / 60_000);
  if (minutes < 60) return suffix(minutes, 'минуту', 'минуты', 'минут');
  const hours = Math.round(abs / 3_600_000);
  if (hours < 24) return suffix(hours, 'час', 'часа', 'часов');
  const days = Math.round(abs / 86_400_000);
  return suffix(days, 'день', 'дня', 'дней');
}

/** Russian labels for the status chip; unknown statuses render raw. */
export const SYNC_STATUS_LABELS: Record<SyncRunStatus, string> = {
  running: 'Идёт',
  success: 'Успешно',
  partial: 'Частично',
  failed: 'Ошибка',
  cancelled: 'Отменено',
};

export function syncStatusLabel(status: string | null | undefined): string {
  if (!status) return '—';
  return isSyncRunStatus(status) ? SYNC_STATUS_LABELS[status] : status;
}

/** CSS-safe class for the status chip (known statuses only). */
export type SyncStatusTone =
  | 'running'
  | 'success'
  | 'partial'
  | 'failed'
  | 'cancelled'
  | 'unknown';

export function syncStatusTone(status: string | null | undefined): SyncStatusTone {
  return isSyncRunStatus(status) ? status : 'unknown';
}

/** Trigger labels; an unknown server value is shown verbatim. */
const SYNC_TRIGGER_LABELS: Record<string, string> = {
  manual: 'вручную',
  hourly: 'по расписанию',
  schedule: 'по расписанию',
  scheduled: 'по расписанию',
  startup: 'при запуске',
  initial: 'при запуске',
  auto: 'по расписанию',
};

export function syncTriggerLabel(trigger: string | null | undefined): string | null {
  if (!trigger) return null;
  return SYNC_TRIGGER_LABELS[trigger] ?? trigger;
}

/** Phase labels for `sync.progress`; unknown phases are shown verbatim. */
const SYNC_PHASE_LABELS: Record<string, string> = {
  starting: 'запуск',
  network: 'проверка сети',
  auth: 'авторизация',
  likes: '«Мне нравится»',
  playlists: 'плейлисты',
  playlist: 'плейлист',
  tracks: 'треки',
  diff: 'сравнение состава',
  write: 'запись в базу',
  commit: 'запись в базу',
  finalize: 'завершение',
  done: 'завершение',
};

export function syncPhaseLabel(phase: string | null | undefined): string | null {
  if (!phase) return null;
  return SYNC_PHASE_LABELS[phase.toLowerCase()] ?? phase;
}

/** 0…100, or null when the server sent no usable counter. */
export function progressPercent(progress: SyncProgress | null | undefined): number | null {
  if (!progress) return null;
  const { current, total } = progress;
  if (current === null || total === null || total <= 0) return null;
  return Math.min(100, Math.max(0, Math.round((current / total) * 100)));
}

/** `3 из 12` (or whatever half of the pair the server provided). */
export function progressText(progress: SyncProgress | null | undefined): string | null {
  if (!progress) return null;
  const { current, total } = progress;
  if (current !== null && total !== null) return `${current} из ${total}`;
  if (current !== null) return `${current}`;
  if (total !== null) return `из ${total}`;
  return null;
}

/** `добавлено 3 · обновлено 2 · удалено 1`. */
export function countsSummary(counts: SyncCounts | null | undefined): string | null {
  if (!counts) return null;
  return `добавлено ${counts.added} · обновлено ${counts.updated} · удалено ${counts.removed}`;
}

/** Total number of changed entities in a run. */
export function countsTotal(counts: SyncCounts | null | undefined): number {
  if (!counts) return 0;
  return counts.added + counts.updated + counts.removed;
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
      'Сервер вернул некорректный JSON в ответе синхронизации.',
      'invalid_response',
      response.status,
    );
  }
}

export const syncApi = {
  /** `GET /sync/status` — running state, last run, schedule, progress. */
  async status(): Promise<SyncStatus> {
    return normalizeSyncStatus(await request<unknown>('/sync/status'));
  },

  /** `GET /sync/history?limit=20` — newest first. */
  async history(limit = SYNC_HISTORY_LIMIT): Promise<SyncHistoryPage> {
    const safeLimit = Math.max(1, Math.min(SYNC_HISTORY_LIMIT, Math.round(limit)));
    return normalizeSyncHistory(
      await request<unknown>(`/sync/history?limit=${safeLimit}`),
      safeLimit,
    );
  },

  /** `POST /sync/start` — 202 `SyncStatus`; PIN/localhost protected. */
  async start(): Promise<SyncStatus | null> {
    const raw = await request<unknown>('/sync/start', {
      method: 'POST',
      body: '{}',
    });
    const record = asRecord(raw);
    if (record === null || Object.keys(record).length === 0) return null;
    return normalizeSyncStatus(raw);
  },

  /** `POST /sync/cancel` — 202 `SyncRun`; PIN/localhost protected. */
  async cancel(): Promise<SyncRun | null> {
    const raw = await request<unknown>('/sync/cancel', {
      method: 'POST',
      body: '{}',
    });
    const record = asRecord(raw);
    if (record === null) return null;
    return normalizeSyncRun(record.run) ?? normalizeSyncRun(raw);
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
export function isSyncPinRequired(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  if (error.status !== 401 && error.status !== 403) return false;
  return !PIN_EXEMPT_CODES.has(error.code);
}

/** Only a wrong PIN is worth asking again. */
export function isRetryableSyncPinError(error: unknown): boolean {
  return error instanceof ApiError && error.code === 'invalid_pin';
}

/**
 * A `404` on the sync routes means the backend has no P6 routes yet. FastAPI's
 * default 404 body has no `error.code`; there are no path parameters in §10, so
 * any 404 on these paths is read as "route missing" and surfaced honestly.
 */
export function isSyncRouteMissing(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

/** Thrown when the user closed the PIN prompt: the mutation was not replayed. */
export class SyncPinCancelledError extends Error {
  constructor(message = 'PIN input was cancelled') {
    super(message);
    this.name = 'SyncPinCancelledError';
  }
}

/**
 * Runs a PIN-protected mutation. On `401 auth_required` / `403 pin_required`
 * the prompt supplies a PIN, `POST /auth/pin` opens the session cookie and the
 * exact same request is replayed once. Login failures (`invalid_pin`, …) and a
 * cancelled prompt surface as errors; the caller decides whether to ask again.
 */
export async function withSyncPinRetry<T>(
  run: () => Promise<T>,
  askPin?: SyncPinPrompt | null,
): Promise<T> {
  try {
    return await run();
  } catch (error) {
    if (!isSyncPinRequired(error) || !askPin) throw error;
    const pin = await askPin();
    if (pin === null || pin.trim() === '') throw new SyncPinCancelledError();
    // Wrong PIN / rate limit / no PIN configured throws here, before any replay.
    await syncApi.loginPin(pin);
    return run();
  }
}

/** Human-readable message for any sync API failure. */
export function describeSyncError(error: unknown): string {
  if (error instanceof SyncPinCancelledError) {
    return 'Действие отменено: PIN не введён.';
  }
  if (error instanceof ApiError) {
    switch (error.code) {
      case 'pin_required':
        return 'Запуск синхронизации с другого устройства требует PIN. Задайте security.pin в config.yaml и перезапустите сервер.';
      case 'auth_required':
        return 'Действие требует PIN-код: введите PIN и повторите.';
      case 'invalid_pin':
        return 'Неверный PIN.';
      case 'pin_not_configured':
        return 'PIN на сервере не настроен: задайте security.pin в config.yaml.';
      case 'too_many_attempts':
        return 'Слишком много попыток ввода PIN. Подождите минуту и повторите.';
      case 'sync_running':
        return 'Синхронизация уже идёт: дождитесь завершения или отмените текущий прогон.';
      case 'sync_not_running':
        return 'Синхронизация не идёт: отменять нечего.';
      case 'sync_disabled':
        return 'Синхронизация выключена в конфиге сервера (sync.enabled=false).';
      case 'yandex_auth_required':
      case 'auth_required_yandex':
        return 'Яндекс Музыка не подключена: авторизуйтесь в разделе «Яндекс» и повторите.';
      case 'not_found':
        return 'Сервер не нашёл запрошенные данные синхронизации.';
      case 'unavailable':
        return 'Сервис синхронизации недоступен на сервере.';
      case 'sync_unavailable':
        return 'Сервис синхронизации выключен или ещё не готов на сервере.';
      case 'network_error':
        return 'Сервер MusicNX не отвечает: проверьте соединение.';
      case 'invalid_response':
        return 'Сервер вернул некорректный ответ синхронизации.';
      default:
        return error.message;
    }
  }
  return 'Не удалось выполнить запрос к разделу синхронизации.';
}
