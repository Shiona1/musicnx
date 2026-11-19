/**
 * Lyrics (P5) REST client and pure helpers — mirrors docs/API.md §9.
 *
 * `GET /api/v1/lyrics/{track_id}?language=` is open read-only: the backend owns
 * the LRC cache and answers either timed `lines` (format `lrc`) or a `plain`
 * fallback (format `text`), and `available:false` when there is no text at all.
 * `PUT /api/v1/lyrics/{track_id}/offset` is PIN/localhost protected: the browser
 * never holds a Yandex token, it only rides the HttpOnly `musicnx_session`
 * cookie set by `POST /auth/pin`. `withLyricsPinRetry` asks for a PIN once,
 * logs in and replays the exact request.
 *
 * Offset convention (same as the server): `offset_ms` shifts the lyrics against
 * the audio, so a line with `time_ms` becomes the active one once
 * `position >= time_ms + offset_ms`; seeking to a line targets
 * `time_ms + offset_ms`.
 *
 * Nothing is persisted client-side (no tokens, no localStorage) — the saved
 * offset lives on the server and every reload re-reads it.
 */
import { ApiError } from './api';

/** Offset buttons move the lyrics by half a second, as required by P5. */
export const LYRICS_OFFSET_STEP_MS = 500;

/**
 * UI guard only: a nonsensical offset (minutes) is rejected before the request.
 * The server remains the authority — if it validates tighter, its error wins.
 */
export const LYRICS_OFFSET_LIMIT_MS = 60_000;

/** `format` from docs/API.md §9; unknown backend values stay strings. */
export type LyricsFormat = 'lrc' | 'text' | 'none';

/** One timed line of an LRC lyric. */
export interface LyricsLine {
  time_ms: number;
  text: string;
}

/** Body of `GET /lyrics/{track_id}` and `PUT /lyrics/{track_id}/offset`. */
export interface LyricsDTO {
  track_id: number;
  available: boolean;
  format: LyricsFormat | string;
  language: string | null;
  /** `yandex | lrc | plain` in P5; kept as a string for forward compatibility. */
  source: string | null;
  /** Saved offset in milliseconds (server-owned). */
  offset_ms: number;
  lines: LyricsLine[];
  plain: string | null;
}

/** Resolves a PIN, or null when the user cancelled the prompt. */
export type LyricsPinPrompt = () => Promise<string | null>;

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

function inferFormat(available: boolean, lineCount: number, plain: string | null): LyricsFormat {
  if (!available) return 'none';
  if (lineCount > 0) return 'lrc';
  if (plain !== null) return 'text';
  return 'none';
}

/** Defensive parsing of one line: a line without a numeric time is dropped. */
export function normalizeLyricsLine(value: unknown): LyricsLine | null {
  const record = asRecord(value);
  if (record === null) return null;
  const time = toFiniteNumber(record.time_ms);
  if (time === null) return null;
  return {
    time_ms: Math.max(0, Math.round(time)),
    text: typeof record.text === 'string' ? record.text : '',
  };
}

/**
 * Normalizes an unknown API body into `LyricsDTO`. The server stays the source
 * of truth: nothing is synthesised, malformed lines are dropped and `lines` is
 * sorted so the binary search in `activeLineIndex` is valid.
 */
export function normalizeLyrics(raw: unknown, fallbackTrackId: number): LyricsDTO {
  const record = asRecord(raw) ?? {};
  const trackId = toFiniteNumber(record.track_id);

  const lines: LyricsLine[] = [];
  if (Array.isArray(record.lines)) {
    for (const item of record.lines) {
      const line = normalizeLyricsLine(item);
      if (line !== null) lines.push(line);
    }
  }
  lines.sort((left, right) => left.time_ms - right.time_ms);

  const available = record.available === true;
  const plain =
    typeof record.plain === 'string' && record.plain.trim() !== '' ? record.plain : null;
  const format =
    typeof record.format === 'string' && record.format !== ''
      ? record.format
      : inferFormat(available, lines.length, plain);
  const rawLanguage =
    typeof record.language === 'string' && record.language !== '' ? record.language : null;
  // The backend canonicalises "no language" to `und`; never show that as a tag.
  const language = rawLanguage !== null && rawLanguage.toLowerCase() === 'und' ? null : rawLanguage;
  const source = typeof record.source === 'string' && record.source !== '' ? record.source : null;
  const offset = toFiniteNumber(record.offset_ms);

  return {
    track_id: trackId ?? fallbackTrackId,
    available,
    format,
    language,
    source,
    offset_ms: offset === null ? 0 : Math.round(offset),
    lines,
    plain,
  };
}

/** True when the response actually carries something worth rendering. */
export function hasLyricsContent(
  data: Pick<LyricsDTO, 'available' | 'lines' | 'plain'> | null,
): boolean {
  if (data === null || !data.available) return false;
  if (data.lines.length > 0) return true;
  return data.plain !== null && data.plain.trim() !== '';
}

/**
 * Index of the line active at `positionMs`, or -1 before the first line.
 * `lines` must be sorted by `time_ms` (normalizeLyrics guarantees it).
 */
export function activeLineIndex(
  lines: readonly LyricsLine[],
  positionMs: number,
  offsetMs = 0,
): number {
  if (lines.length === 0 || !Number.isFinite(positionMs)) return -1;
  const target = positionMs - offsetMs;
  let low = 0;
  let high = lines.length - 1;
  let found = -1;
  while (low <= high) {
    const middle = (low + high) >> 1;
    if (lines[middle].time_ms <= target) {
      found = middle;
      low = middle + 1;
    } else {
      high = middle - 1;
    }
  }
  return found;
}

/** Enforces the UI limit and integral milliseconds before a PUT. */
export function clampOffsetMs(value: number): number {
  const rounded = Math.round(Number.isFinite(value) ? value : 0);
  return Math.max(-LYRICS_OFFSET_LIMIT_MS, Math.min(LYRICS_OFFSET_LIMIT_MS, rounded));
}

/** Russian label: `0,0 с`, `+0,5 с`, `−1,0 с`. */
export function formatOffsetMs(value: number): string {
  const ms = Math.round(Number.isFinite(value) ? value : 0);
  if (ms === 0) return '0,0 с';
  const sign = ms > 0 ? '+' : '−';
  return `${sign}${(Math.abs(ms) / 1000).toFixed(1).replace('.', ',')} с`;
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
    response = await fetch(`/api/v1${path}`, {
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
  return (await response.json()) as T;
}

export const lyricsApi = {
  /** `GET /lyrics/{track_id}?language=` — open read-only; cached by the server. */
  async get(trackId: number, language?: string): Promise<LyricsDTO> {
    const search = new URLSearchParams();
    if (language) search.set('language', language);
    const suffix = search.toString();
    const raw = await request<unknown>(`/lyrics/${trackId}${suffix ? `?${suffix}` : ''}`);
    return normalizeLyrics(raw, trackId);
  },

  /** `PUT /lyrics/{track_id}/offset` — PIN/localhost protected; returns the DTO. */
  async setOffset(trackId: number, offsetMs: number): Promise<LyricsDTO> {
    const raw = await request<unknown>(`/lyrics/${trackId}/offset`, {
      method: 'PUT',
      body: JSON.stringify({ offset_ms: Math.round(offsetMs) }),
    });
    return normalizeLyrics(raw, trackId);
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

/** True when the offset PUT answered "PIN needed" and a PIN entry can help. */
export function isLyricsPinRequired(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  if (error.status !== 401 && error.status !== 403) return false;
  return !PIN_EXEMPT_CODES.has(error.code);
}

/** Only a wrong PIN is worth asking again. */
export function isRetryableLyricsPinError(error: unknown): boolean {
  return error instanceof ApiError && error.code === 'invalid_pin';
}

/**
 * A `404` that is not a domain `not_found` means the backend has no P5 lyrics
 * routes yet (FastAPI's default 404 body has no `error.code`). The UI must say
 * so instead of pretending the track has no text (AGENTS.md §12).
 */
export function isLyricsRouteMissing(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    error.status === 404 &&
    error.code !== 'not_found' &&
    error.code !== 'track_not_found'
  );
}

/** Thrown when the user closed the PIN prompt: the PUT was not replayed. */
export class LyricsPinCancelledError extends Error {
  constructor(message = 'PIN input was cancelled') {
    super(message);
    this.name = 'LyricsPinCancelledError';
  }
}

async function loginPin(pin: string): Promise<void> {
  await request<{ authenticated?: boolean }>('/auth/pin', {
    method: 'POST',
    body: JSON.stringify({ pin }),
  });
}

/**
 * Runs the PIN-protected offset PUT. On `401 auth_required` / `403
 * pin_required` the prompt supplies a PIN, `POST /auth/pin` opens the session
 * cookie and the exact request is replayed once. Login failures (`invalid_pin`,
 * …) and a cancelled prompt surface as errors; the caller decides whether to
 * ask again.
 */
export async function withLyricsPinRetry<T>(
  run: () => Promise<T>,
  askPin?: LyricsPinPrompt | null,
): Promise<T> {
  try {
    return await run();
  } catch (error) {
    if (!isLyricsPinRequired(error) || !askPin) throw error;
    const pin = await askPin();
    if (pin === null || pin.trim() === '') throw new LyricsPinCancelledError();
    // Wrong PIN / rate limit / no PIN configured throws here, before any replay.
    await loginPin(pin);
    return run();
  }
}

/** Human-readable message for any lyrics API failure. */
export function describeLyricsError(error: unknown): string {
  if (error instanceof LyricsPinCancelledError) {
    return 'Смещение не сохранено: PIN не введён.';
  }
  if (error instanceof ApiError) {
    switch (error.code) {
      case 'pin_required':
        return 'Сохранение смещения с другого устройства требует PIN. Задайте security.pin в config.yaml и перезапустите сервер.';
      case 'auth_required':
        return 'Действие требует PIN-код: введите PIN и повторите.';
      case 'invalid_pin':
        return 'Неверный PIN.';
      case 'pin_not_configured':
        return 'PIN на сервере не настроен: задайте security.pin в config.yaml.';
      case 'too_many_attempts':
        return 'Слишком много попыток ввода PIN. Подождите минуту и повторите.';
      case 'network_error':
        return 'Сервер MusicNX не отвечает: проверьте соединение.';
      case 'not_found':
        return 'Сервер не нашёл трек или текст для него.';
      case 'lyrics_unavailable':
        return 'Для этого трека нет текста: смещение сохранить нельзя.';
      case 'unavailable':
        return 'Тексты песен недоступны на сервере.';
      case 'validation_error':
        return 'Сервер отклонил смещение: значение вне допустимого диапазона.';
      default:
        return error.message;
    }
  }
  return 'Не удалось загрузить текст песни.';
}

/** Russian source badge for the panel header. */
export function lyricsSourceLabel(data: LyricsDTO | null): string | null {
  if (data === null) return null;
  switch (data.source) {
    case 'yandex':
      return 'Яндекс';
    case 'lrclib':
      return 'LRCLIB';
    case 'lrc':
      return 'локальный LRC';
    case 'plain':
      return 'обычный текст';
    case 'mock':
      return 'пример (mock)';
    default:
      if (data.format === 'lrc') return 'LRC';
      // docs/API.md says `text`, the P5 backend answers `plain`: accept both.
      if (data.format === 'text' || data.format === 'plain') return 'обычный текст';
      return null;
  }
}
