/**
 * Downloads (P4) REST client — mirrors docs/API.md §8.
 *
 * The browser never holds a Yandex token: every call is same-origin and rides
 * the HttpOnly `musicnx_session` cookie set by `POST /auth/pin`. Mutation
 * routes (`POST`/`DELETE`/`cancel`/`retry`) are PIN/localhost protected, so
 * `withPinRetry` asks for a PIN through a caller-provided prompt, logs in and
 * replays the exact request once. Nothing is persisted client-side.
 *
 * A backend that predates P4 answers `404 not_found` for these paths; callers
 * must surface that honestly instead of pretending a task was created
 * (AGENTS.md §12). `isRouteMissingError` detects that case for `GET /downloads`.
 */
import { ApiError } from './api';

export { ApiError };

export const API_BASE = '/api/v1';

/** `status` of docs/API.md §8.1 */
export type DownloadStatus =
  | 'queued'
  | 'downloading'
  | 'paused'
  | 'completed'
  | 'failed'
  | 'cancelled';

/** One queue/history entry (`DownloadTask`). */
export interface DownloadTask {
  id: number;
  track_id: number;
  /** Track title known to the backend; null until it is resolved. */
  title: string | null;
  status: DownloadStatus;
  priority: number;
  attempts: number;
  bytes_done: number;
  bytes_total: number | null;
  /** Relative path inside the downloads root; null until the file is placed. */
  target_path: string | null;
  /** Requested quality (the promise, e.g. `flac`). */
  quality: string | null;
  /** Actual container/codec found on disk (e.g. `m4a`); never a promise. */
  format: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
}

/** `GET /downloads` body. */
export interface DownloadTaskPage {
  items: DownloadTask[];
  total: number;
}

/** `POST /downloads` body: exactly one target plus optional priority. */
export type DownloadRequest =
  | { track_id: number; priority?: number }
  | { album_id: number; priority?: number }
  | { playlist_id: string; priority?: number };

/** `POST /downloads` answer: one task per track (album/playlist expands). */
export interface DownloadStartResponse {
  tasks: DownloadTask[];
}

export interface PinLoginResult {
  authenticated: boolean;
  pin_configured?: boolean;
}

/** Statuses that mean "the manager still has this task in its queue". */
const QUEUE_STATUSES: ReadonlySet<DownloadStatus> = new Set<DownloadStatus>([
  'queued',
  'downloading',
  'paused',
]);

/** Statuses that actually move bytes; only they justify 1 s polling. */
const RUNNING_STATUSES: ReadonlySet<DownloadStatus> = new Set<DownloadStatus>([
  'queued',
  'downloading',
]);

export function isActiveStatus(status: DownloadStatus): boolean {
  return QUEUE_STATUSES.has(status);
}

export function isRunningStatus(status: DownloadStatus): boolean {
  return RUNNING_STATUSES.has(status);
}

export function isTerminalStatus(status: DownloadStatus): boolean {
  return !QUEUE_STATUSES.has(status);
}

function query(params: Record<string, string | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) search.set(key, value);
  }
  return search.toString();
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
      // Mutations depend on the HttpOnly PIN session cookie; never a token.
      credentials: 'same-origin',
    });
  } catch {
    throw new ApiError('Сервер MusicNX недоступен: проверьте соединение.', 'network_error', 0);
  }

  if (!response.ok) throw await parseError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const downloadsApi = {
  /** `GET /downloads?status=…` — queue plus history, newest first. */
  list(status?: DownloadStatus): Promise<DownloadTaskPage> {
    const search = query({ status });
    return request<DownloadTaskPage>(`/downloads${search ? `?${search}` : ''}`);
  },

  /** `POST /downloads` — 202 with one task per track; PIN/localhost protected. */
  async create(payload: DownloadRequest): Promise<DownloadTask[]> {
    const body = await request<DownloadStartResponse>('/downloads', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    return Array.isArray(body.tasks) ? body.tasks : [];
  },

  /** `DELETE /downloads/{id}` — 204; cancels an active task and drops the record. */
  remove(id: number): Promise<void> {
    return request<void>(`/downloads/${id}`, { method: 'DELETE' });
  },

  /** `POST /downloads/{id}/cancel` — soft cancel, partial file kept for resume. */
  cancel(id: number): Promise<DownloadTask> {
    return request<DownloadTask>(`/downloads/${id}/cancel`, { method: 'POST', body: '{}' });
  },

  /** `POST /downloads/{id}/retry` — resets attempts/error and queues again. */
  retry(id: number): Promise<DownloadTask> {
    return request<DownloadTask>(`/downloads/${id}/retry`, { method: 'POST', body: '{}' });
  },

  /** `POST /auth/pin` — sets the HttpOnly session cookie used by mutations. */
  loginPin(pin: string): Promise<PinLoginResult> {
    return request<PinLoginResult>('/auth/pin', {
      method: 'POST',
      body: JSON.stringify({ pin }),
    });
  },
};

/**
 * Codes that must not open a PIN dialog: an Origin/Host rejection cannot be
 * fixed by a PIN, `pin_required` means the server has no PIN configured at all,
 * and the login-stage codes are reported by the dialog itself.
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
export function isPinRequiredError(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  if (error.status !== 401 && error.status !== 403) return false;
  return !PIN_EXEMPT_CODES.has(error.code);
}

/** True for failures of `POST /auth/pin` itself (wrong/disabled/too many PINs). */
export function isPinLoginError(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    (error.code === 'invalid_pin' ||
      error.code === 'pin_not_configured' ||
      error.code === 'too_many_attempts')
  );
}

/** Only a wrong PIN is worth asking again; the other login failures are final. */
export function isRetryablePinLoginError(error: unknown): boolean {
  return error instanceof ApiError && error.code === 'invalid_pin';
}

/** A `404` on the downloads list means the backend has no P4 routes yet. */
export function isRouteMissingError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

/** Thrown when the user closed the PIN prompt: the operation was not replayed. */
export class PinCancelledError extends Error {
  constructor(message = 'PIN input was cancelled') {
    super(message);
    this.name = 'PinCancelledError';
  }
}

/** Resolves a PIN, or null when the user cancelled the prompt. */
export type PinPrompt = () => Promise<string | null>;

/**
 * Runs a PIN-protected mutation. If the server answers `401 auth_required` /
 * `403 pin_required`, the prompt supplies a PIN, `POST /auth/pin` opens the
 * session cookie and the exact same request is replayed once. Login failures
 * (`invalid_pin`, …) and a cancelled prompt surface as errors; the caller
 * decides whether to ask again.
 */
export async function withPinRetry<T>(
  run: () => Promise<T>,
  askPin?: PinPrompt | null,
): Promise<T> {
  try {
    return await run();
  } catch (error) {
    if (!isPinRequiredError(error) || !askPin) throw error;
    const pin = await askPin();
    if (pin === null || pin.trim() === '') throw new PinCancelledError();
    // Wrong PIN / rate limit / no PIN configured throws here, before any replay.
    await downloadsApi.loginPin(pin);
    return run();
  }
}

/** Human-readable message for any downloads API failure. */
export function describeDownloadError(error: unknown): string {
  if (error instanceof PinCancelledError) {
    return 'Ввод PIN отменён — действие не выполнено.';
  }
  if (error instanceof ApiError) {
    switch (error.code) {
      case 'pin_required':
        return 'Скачивание с другого устройства требует PIN. Задайте security.pin в config.yaml и перезапустите сервер.';
      case 'auth_required':
        return 'Действие требует PIN-код: введите PIN и повторите.';
      case 'invalid_pin':
        return 'Неверный PIN.';
      case 'pin_not_configured':
        return 'PIN на сервере не настроен: задайте security.pin в config.yaml.';
      case 'too_many_attempts':
        return 'Слишком много попыток ввода PIN. Подождите минуту и повторите.';
      case 'validation_error':
        return 'Сервер отклонил запрос загрузки: проверьте трек, альбом или плейлист.';
      case 'not_found':
        return 'Сервер не нашёл объект загрузки. Возможно, backend ещё не включает маршруты /downloads (P4).';
      case 'unavailable':
        return 'Сервис загрузок недоступен на сервере.';
      case 'network_error':
        return 'Сервер MusicNX не отвечает: проверьте соединение.';
      default:
        return error.message;
    }
  }
  return 'Не удалось выполнить запрос к разделу загрузок.';
}

/** Russian labels for the status chip; "completed" is a verified offline file. */
export const DOWNLOAD_STATUS_LABELS: Record<DownloadStatus, string> = {
  queued: 'В очереди',
  downloading: 'Скачивается',
  paused: 'Пауза',
  completed: 'Офлайн (проверено)',
  failed: 'Ошибка',
  cancelled: 'Отменено',
};

/** Badge shows the *actual* format; null until the backend resolved it. */
export function formatBadge(task: Pick<DownloadTask, 'format'>): string {
  return task.format ? task.format.toUpperCase() : '—';
}

export function qualityNotice(
  task: Pick<DownloadTask, 'quality' | 'format'>,
): string | null {
  if (!task.quality) return null;
  if (task.format && task.format.toLowerCase() === task.quality.toLowerCase()) return null;
  return `запрос: ${task.quality.toUpperCase()}`;
}

/** Percentage of bytes written; 100 for a verified task, null when unknown. */
export function progressPercent(
  task: Pick<DownloadTask, 'status' | 'bytes_done' | 'bytes_total'>,
): number | null {
  if (task.status === 'completed') return 100;
  if (task.bytes_total === null || task.bytes_total <= 0) return null;
  const ratio = task.bytes_done / task.bytes_total;
  return Math.min(100, Math.max(0, Math.round(ratio * 100)));
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes) || bytes < 0) {
    return '—';
  }
  if (bytes < 1024) return `${bytes} Б`;
  const units = ['КБ', 'МБ', 'ГБ', 'ТБ'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const digits = value >= 10 ? 0 : 1;
  return `${value.toFixed(digits)} ${units[unit]}`;
}

/** Short local timestamp for the history list; falls back to the raw string. */
export function formatTaskTime(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}
