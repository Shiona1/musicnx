import { writable } from 'svelte/store';
import type { ConnectionState, PlayerState, QueueState, ToastTone, Track } from './types';

export const player = writable<PlayerState | null>(null);
export const queue = writable<QueueState | null>(null);
export const shelf = writable<Track[]>([]);
export const shelfTotal = writable<number>(0);
export const connection = writable<ConnectionState>('connecting');
export const serverMode = writable<string>('mock');
export const serverVersion = writable<string>('');
export const toastMessage = writable<string | null>(null);
export const toastTone = writable<ToastTone>('info');
export const libraryVersion = writable<number>(0);

/**
 * Server position plus the local arrival time. The UI interpolates between
 * 2 Hz WebSocket updates instead of polling the server.
 */
export const positionClock = writable<{ ms: number; at: number }>({ ms: 0, at: 0 });

/** Anything beyond this cannot be a plausible network transit; treat it as clock skew. */
const MAX_TRANSIT_MS = 2000;

/**
 * Estimates how long a position update spent in transit. Only the interval
 * between server and client timestamps is trusted when the clocks agree;
 * a larger gap means the clocks are out of sync, so interpolation falls back
 * to the local arrival time.
 */
function estimateTransit(serverTimeMs: number | null | undefined): number {
  if (typeof serverTimeMs !== 'number' || !Number.isFinite(serverTimeMs)) return 0;
  const raw = Date.now() - serverTimeMs;
  if (Math.abs(raw) >= MAX_TRANSIT_MS) return 0;
  return Math.min(Math.max(raw, 0), MAX_TRANSIT_MS);
}

let toastTimer: number | undefined;

export function showToast(message: string, tone: ToastTone = 'info'): void {
  toastMessage.set(message);
  toastTone.set(tone);
  if (toastTimer !== undefined) {
    window.clearTimeout(toastTimer);
  }
  toastTimer = window.setTimeout(() => toastMessage.set(null), 4500);
}

export function applyPlayer(state: PlayerState): void {
  player.set(state);
  positionClock.set({ ms: state.position_ms, at: performance.now() });
}

/**
 * Applies a position update. `serverTimeMs` is the server timestamp of the
 * sample: dating the clock slightly in the past keeps the progress bar from
 * lagging behind by the network transit instead of restarting from zero.
 */
export function applyPosition(ms: number, serverTimeMs?: number): void {
  const at = performance.now() - estimateTransit(serverTimeMs);
  positionClock.set({ ms, at });
}
