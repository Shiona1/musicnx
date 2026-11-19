/**
 * P5 lyrics stores — client-side state for the karaoke panel, no persistence.
 *
 * App-wide stores (`lib/stores.ts`) stay untouched: this module owns the
 * per-track cache, the loading state machine (`idle | loading | available |
 * none | error`) and the PIN-protected offset save. Nothing is written to
 * localStorage: the backend is the only source of truth and the saved offset is
 * re-read with every `GET /lyrics/{track_id}`.
 *
 * WS integration (done by the App integrator, App.svelte is not edited here):
 *   case 'lyrics.updated':
 *     applyLyricsEvent(event);   // re-reads the text when it just appeared
 *     break;
 *
 * The cache is capped (`MAX_CACHED_TRACKS`) because a long session may visit
 * hundreds of tracks; Map insertion order gives a cheap FIFO eviction.
 */
import { get, writable } from 'svelte/store';
import {
  describeLyricsError,
  hasLyricsContent,
  isLyricsRouteMissing,
  lyricsApi,
  withLyricsPinRetry,
} from './lyrics';
import type { LyricsDTO, LyricsPinPrompt } from './lyrics';
import type { WsEvent } from './types';

/** `idle` = no track selected; the other four states mirror the API contract. */
export type LyricsStatus = 'idle' | 'loading' | 'available' | 'none' | 'error';

export interface LyricsEntry {
  trackId: number | null;
  status: LyricsStatus;
  /** Last successful body for this track; kept only while `status=available`. */
  data: LyricsDTO | null;
  error: string | null;
  /** True when the backend has no P5 lyrics routes at all (404, not `not_found`). */
  routeMissing: boolean;
}

const IDLE_ENTRY: LyricsEntry = {
  trackId: null,
  status: 'idle',
  data: null,
  error: null,
  routeMissing: false,
};

/** Current panel state. One panel per app; App can render it conditionally. */
export const lyricsEntry = writable<LyricsEntry>(IDLE_ENTRY);

const MAX_CACHED_TRACKS = 40;
const cache = new Map<number, LyricsDTO>();
const inFlight = new Map<number, Promise<LyricsDTO>>();

let currentTrackId: number | null = null;
let requestSeq = 0;

function remember(trackId: number, data: LyricsDTO): void {
  // Re-insert so a re-read moves the track to the freshest end of the FIFO.
  cache.delete(trackId);
  cache.set(trackId, data);
  while (cache.size > MAX_CACHED_TRACKS) {
    const oldest = cache.keys().next();
    if (oldest.done) break;
    cache.delete(oldest.value);
  }
}

/** `available` only when the response really carries lines or plain text. */
export function lyricsStatusFor(data: LyricsDTO | null): LyricsStatus {
  if (data === null) return 'none';
  return hasLyricsContent(data) ? 'available' : 'none';
}

function entryFor(trackId: number, data: LyricsDTO): LyricsEntry {
  return { trackId, status: lyricsStatusFor(data), data, error: null, routeMissing: false };
}

function fetchLyrics(trackId: number): Promise<LyricsDTO> {
  const pending = inFlight.get(trackId);
  if (pending) return pending;
  const promise = lyricsApi.get(trackId).finally(() => {
    // Only the request that is still registered may clear the slot.
    if (inFlight.get(trackId) === promise) inFlight.delete(trackId);
  });
  inFlight.set(trackId, promise);
  return promise;
}

/**
 * Loads (or re-reads) the lyrics for one track. Safe to call from a reactive
 * statement: cached tracks answer instantly, concurrent calls share one request,
 * and an answer for a track that is no longer selected is dropped.
 */
export async function loadLyrics(
  trackId: number | null,
  options: { force?: boolean } = {},
): Promise<void> {
  const token = ++requestSeq;
  currentTrackId = trackId;

  if (trackId === null) {
    lyricsEntry.set(IDLE_ENTRY);
    return;
  }

  const cached = cache.get(trackId);
  if (cached !== undefined && options.force !== true) {
    lyricsEntry.set(entryFor(trackId, cached));
    return;
  }

  lyricsEntry.set({
    trackId,
    status: 'loading',
    data: null,
    error: null,
    routeMissing: false,
  });

  try {
    const data = await fetchLyrics(trackId);
    if (token !== requestSeq) return;
    remember(trackId, data);
    lyricsEntry.set(entryFor(trackId, data));
  } catch (error) {
    if (token !== requestSeq) return;
    lyricsEntry.set({
      trackId,
      status: 'error',
      data: null,
      error: describeLyricsError(error),
      routeMissing: isLyricsRouteMissing(error),
    });
  }
}

/**
 * WS bridge for `lyrics.updated` (docs/API.md §7). Returns true when the event
 * belonged to lyrics, so the integrator can call it for every event.
 */
export function applyLyricsEvent(event: Pick<WsEvent, 'type' | 'payload'>): boolean {
  if (event.type !== 'lyrics.updated') return false;
  const raw = event.payload.track_id;
  const trackId = typeof raw === 'number' ? raw : Number(raw);
  if (!Number.isFinite(trackId)) return true;

  cache.delete(trackId);
  if (trackId !== currentTrackId) return true;

  if (event.payload.available === true) {
    void loadLyrics(trackId, { force: true });
  } else {
    lyricsEntry.set({
      trackId,
      status: 'none',
      data: null,
      error: null,
      routeMissing: false,
    });
  }
  return true;
}

/**
 * `PUT /lyrics/{track_id}/offset`; PIN-protected. The answer is the new server
 * state, so the cache and the panel adopt its `offset_ms` instead of the value
 * the user typed. A failure leaves the previous state untouched.
 */
export async function saveLyricsOffset(
  trackId: number,
  offsetMs: number,
  askPin?: LyricsPinPrompt | null,
): Promise<LyricsDTO> {
  const data = await withLyricsPinRetry(() => lyricsApi.setOffset(trackId, offsetMs), askPin);
  remember(trackId, data);
  const entry = get(lyricsEntry);
  if (entry.trackId === trackId) lyricsEntry.set(entryFor(trackId, data));
  return data;
}
