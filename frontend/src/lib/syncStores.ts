/**
 * P6 sync stores — the only client-side source of truth for the Sync Center.
 *
 * App-wide stores (`lib/stores.ts`) stay untouched: this module owns the status
 * snapshot, the newest 20 runs, the 2 s polling loop while a run is active and
 * the WebSocket bridge.
 *
 * WS integration (done by the App integrator, App.svelte is not edited here):
 *   case 'sync.started':
 *   case 'sync.progress':
 *   case 'sync.completed':
 *   case 'sync.failed':
 *   case 'sync.cancelled':
 *     applySyncEvent(event);   // live progress; terminal events re-read REST
 *     break;
 *
 * Polling is reference-counted: `startSyncPolling()` on mount returns a stop
 * function. While a run is active `GET /sync/status` is re-read every 2 s.
 * Without an active run a slow 30 s refresh stays subscribed so a scheduled
 * (hourly) run is noticed even when the WS is down; the interval is retried
 * with backoff when the server is unreachable. A `404` means the backend has
 * no P6 routes: polling stops and the UI says so honestly.
 *
 * The server remains the source of truth. WS events update progress instantly,
 * but terminal events trigger a REST re-read, so counts, `next_run_at` and
 * `last_success_at` are never fabricated on the client.
 */
import { derived, get, writable } from 'svelte/store';
import {
  compareRunsNewestFirst,
  describeSyncError,
  isSyncRouteMissing,
  normalizeSyncHistory,
  normalizeSyncProgress,
  normalizeSyncRun,
  syncApi,
  withSyncPinRetry,
  SYNC_HISTORY_LIMIT,
} from './sync';
import type {
  SyncHistoryPage,
  SyncPinPrompt,
  SyncProgress,
  SyncRun,
  SyncStatus,
} from './sync';
import type { WsEvent } from './types';

/** Server-owned status snapshot; null until the first successful read. */
export const syncStatus = writable<SyncStatus | null>(null);
/** Newest 20 runs, exactly what `GET /sync/history` returned last. */
export const syncHistory = writable<SyncRun[]>([]);
export const syncHistoryTotal = writable(0);
/** True only until the first status request settles (skeleton, not every poll). */
export const syncLoading = writable(true);
export const syncError = writable<string | null>(null);
/** The backend has no P6 routes: the UI must say so instead of faking a schedule. */
export const syncRouteMissing = writable(false);
export const syncUpdatedAt = writable(0);

export const syncRunning = derived(syncStatus, (status) => status?.running === true);
export const syncProgress = derived(syncStatus, (status) => status?.progress ?? null);
/** Flat counters some backends put on the status itself. */
export const syncCounts = derived(syncStatus, (status) => status?.counts ?? null);

const RUNNING_POLL_MS = 2000;
const IDLE_POLL_MS = 30_000;
const MAX_BACKOFF_MS = 60_000;

let subscribers = 0;
let timer: number | undefined;
let statusSeq = 0;
let historySeq = 0;
let failures = 0;
let firstLoadDone = false;
let historyLoadedOnce = false;

/** Delay before the next status read: 2 s while running, 30 s while idle. */
function nextDelay(): number {
  const base = get(syncStatus)?.running === true ? RUNNING_POLL_MS : IDLE_POLL_MS;
  if (failures === 0) return base;
  return Math.min(MAX_BACKOFF_MS, base * 2 ** Math.min(failures, 4));
}

function clearTimer(): void {
  if (timer !== undefined) {
    window.clearTimeout(timer);
    timer = undefined;
  }
}

/** Idempotent: at most one pending status refresh exists at a time. */
function schedule(delay: number): void {
  if (subscribers <= 0 || timer !== undefined || get(syncRouteMissing)) return;
  timer = window.setTimeout(() => {
    timer = undefined;
    void refreshSyncStatus();
  }, Math.max(0, delay));
}

/**
 * Replaces a pending refresh with one at `delay`. Needed when the cadence
 * changes mid-flight: a 30 s idle timer must not keep a just-started run
 * waiting, and a leftover 2 s timer must not fire after the run finished.
 */
function rearm(delay: number): void {
  clearTimer();
  if (subscribers <= 0 || get(syncRouteMissing)) return;
  schedule(delay);
}

/** Reads `GET /sync/status` once. Answers of overlapping requests are dropped. */
export async function refreshSyncStatus(): Promise<void> {
  const token = ++statusSeq;
  // The skeleton is for the first read only; later polls must not disable the
  // refresh button and flash the loading state every two seconds.
  if (get(syncStatus) === null) syncLoading.set(true);
  try {
    const next = await syncApi.status();
    if (token !== statusSeq) return;
    const wasRunning = get(syncStatus)?.running === true;
    failures = 0;
    syncRouteMissing.set(false);
    syncError.set(null);
    syncStatus.set(next);
    syncUpdatedAt.set(Date.now());
    firstLoadDone = true;
    // The run just finished: counts and next_run_at moved into history.
    if (wasRunning && !next.running) void refreshSyncHistory();
  } catch (error) {
    if (token !== statusSeq) return;
    failures += 1;
    const missing = isSyncRouteMissing(error);
    syncRouteMissing.set(missing);
    syncError.set(describeSyncError(error));
    if (missing) {
      // Do not keep a stale schedule that a P6-less backend never sent.
      syncStatus.set(null);
      syncHistory.set([]);
      syncHistoryTotal.set(0);
    }
  } finally {
    if (token === statusSeq) {
      firstLoadDone = true;
      syncLoading.set(false);
      rearm(nextDelay());
    }
  }
}

/** Reads `GET /sync/history?limit=20` once. */
export async function refreshSyncHistory(): Promise<void> {
  const token = ++historySeq;
  try {
    const page: SyncHistoryPage = await syncApi.history(SYNC_HISTORY_LIMIT);
    if (token !== historySeq) return;
    syncRouteMissing.set(false);
    // A successful history read does not clear a status error: status drives
    // the screen, and its error must survive a parallel history response.
    syncHistory.set(page.items);
    syncHistoryTotal.set(page.total);
    historyLoadedOnce = true;
    syncUpdatedAt.set(Date.now());
  } catch (error) {
    if (token !== historySeq) return;
    if (isSyncRouteMissing(error)) {
      syncRouteMissing.set(true);
      syncStatus.set(null);
      syncHistory.set([]);
      syncHistoryTotal.set(0);
      historyLoadedOnce = true;
    }
    syncError.set(describeSyncError(error));
  }
}

/** Manual "Обновить": status and history in parallel. */
export async function refreshSync(): Promise<void> {
  await Promise.all([refreshSyncStatus(), refreshSyncHistory()]);
}

/**
 * Enables polling for one consumer. Returns its stop function; polling stops
 * as soon as no subscriber is left. A WS event or a manual refresh re-arms it.
 */
export function startSyncPolling(): () => void {
  subscribers += 1;
  schedule(0);
  if (!historyLoadedOnce) void refreshSyncHistory();
  let stopped = false;
  return () => {
    if (stopped) return;
    stopped = true;
    subscribers = Math.max(0, subscribers - 1);
    if (subscribers === 0) clearTimer();
  };
}

// A run may start from the backend schedule or from WS; re-arm the fast loop
// as soon as the store learns that something is running.
syncStatus.subscribe((status) => {
  if (subscribers <= 0) return;
  if (status?.running === true) rearm(RUNNING_POLL_MS);
});

/** Upserts one run into the newest-first history, keeping at most 20 items. */
export function upsertSyncRun(run: SyncRun): void {
  let inserted = false;
  syncHistory.update((items) => {
    const index = items.findIndex((item) => item.id === run.id);
    if (index === -1) {
      inserted = true;
      return [run, ...items].sort(compareRunsNewestFirst).slice(0, SYNC_HISTORY_LIMIT);
    }
    const next = items.slice();
    next[index] = run;
    return next.sort(compareRunsNewestFirst);
  });
  if (inserted) syncHistoryTotal.update((total) => total + 1);
}

/** Synthetic status for a `sync.started` event that beats the initial GET. */
function runningStatus(progress: SyncProgress | null): SyncStatus {
  return {
    running: true,
    progress,
    last: null,
    next_run_at: null,
    interval_s: null,
    counts: null,
    last_attempt_at: null,
    last_attempt_status: 'running',
    last_success_at: null,
    last_error: null,
  };
}

/** Merges a terminal run into the current status without inventing counters. */
function applyTerminal(current: SyncStatus | null, run: SyncRun | null): SyncStatus | null {
  if (current === null && run === null) return null;
  const base = current ?? runningStatus(null);
  const last = run ?? base.last;
  const finishedAt = run?.finished_at ?? run?.started_at ?? null;
  return {
    ...base,
    running: false,
    progress: null,
    last,
    counts: run?.counts ?? base.counts,
    last_attempt_at: finishedAt ?? base.last_attempt_at,
    last_attempt_status: run?.status ?? base.last_attempt_status,
    // A successful run clears the previous error; a failed one replaces it.
    last_error: run === null ? base.last_error : run.error,
    last_success_at:
      run?.status === 'success'
        ? (run.finished_at ?? run.started_at ?? base.last_success_at)
        : base.last_success_at,
  };
}

/**
 * WS bridge for `sync.*` events (docs/API.md §10). Returns true when the event
 * belonged to sync, so the integrator can call it for every event. Live
 * progress is applied immediately; terminal events re-read REST, which stays
 * the source of truth for counts, schedule and `last_success_at`.
 */
export function applySyncEvent(event: Pick<WsEvent, 'type' | 'payload'>): boolean {
  if (!event.type.startsWith('sync.')) return false;
  const payload = event.payload ?? {};

  switch (event.type) {
    case 'sync.started': {
      // The server puts the fresh (still running) run into history; mirror it
      // so the run and its start time appear before the next REST poll.
      const run = normalizeSyncRun(payload.run);
      if (run !== null) upsertSyncRun(run);
      const progress = normalizeSyncProgress(payload.progress ?? payload);
      syncStatus.update((current) =>
        current
          ? { ...current, running: true, progress: progress ?? current.progress }
          : runningStatus(progress),
      );
      rearm(RUNNING_POLL_MS);
      break;
    }
    case 'sync.progress': {
      const progress = normalizeSyncProgress(payload.progress ?? payload);
      if (progress !== null) {
        syncStatus.update((current) =>
          current
            ? { ...current, running: true, progress }
            : runningStatus(progress),
        );
      }
      rearm(RUNNING_POLL_MS);
      break;
    }
    case 'sync.completed':
    case 'sync.failed':
    case 'sync.cancelled': {
      const run = normalizeSyncRun(payload.run) ?? normalizeSyncRun(payload);
      if (run !== null) upsertSyncRun(run);
      syncStatus.update((current) => applyTerminal(current, run));
      syncUpdatedAt.set(Date.now());
      if (subscribers > 0) {
        // Ordering and totals are server-owned: re-read after a finished run.
        void refreshSyncStatus();
        void refreshSyncHistory();
      }
      break;
    }
    default:
      break;
  }
  return true;
}

/**
 * `POST /sync/start`; PIN-protected. Adopts the 202 `SyncStatus` and arms the
 * 2 s loop. A 202 with an empty body falls back to a status re-read.
 */
export async function requestSyncStart(
  askPin?: SyncPinPrompt | null,
): Promise<SyncStatus | null> {
  const status = await withSyncPinRetry(() => syncApi.start(), askPin);
  failures = 0;
  syncRouteMissing.set(false);
  syncError.set(null);
  if (status !== null) {
    syncStatus.set(status);
    syncUpdatedAt.set(Date.now());
  }
  rearm(RUNNING_POLL_MS);
  if (status === null) await refreshSyncStatus();
  return status;
}

/**
 * `POST /sync/cancel`; PIN-protected. Cancellation is cooperative (between
 * playlists), so the server's status is re-read instead of assuming the run
 * stopped the moment the request returned.
 */
export async function requestSyncCancel(
  askPin?: SyncPinPrompt | null,
): Promise<SyncRun | null> {
  const run = await withSyncPinRetry(() => syncApi.cancel(), askPin);
  failures = 0;
  syncRouteMissing.set(false);
  syncError.set(null);
  if (run !== null) upsertSyncRun(run);
  await refreshSyncStatus();
  await refreshSyncHistory();
  return run;
}

