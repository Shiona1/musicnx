/**
 * P4 download stores — the only client-side source of truth for the Downloads
 * screen and DownloadButton.
 *
 * App-wide stores (`lib/stores.ts`) stay untouched: this module owns the queue,
 * the 1 s polling loop while active tasks exist and the WebSocket bridge.
 *
 * WS integration (done by the App integrator, App.svelte is not edited here):
 *   case 'download.queued':
 *   case 'download.progress':
 *   case 'download.done':
 *   case 'download.failed':
 *     applyDownloadEvent(event);   // upserts `payload.task`, then refreshes
 *     break;
 *
 * Polling is reference-counted: `startDownloadsPolling()` on mount returns a
 * stop function. While at least one component is subscribed and the manager has
 * `queued|downloading` tasks, `GET /downloads` is re-read every second. A
 * `paused` task stays in the queue but does not move bytes, so it does not
 * justify the 1 s loop on its own.
 */
import { derived, get, writable } from 'svelte/store';
import {
  describeDownloadError,
  downloadsApi,
  isActiveStatus,
  isRouteMissingError,
  isRunningStatus,
  withPinRetry,
} from './downloads';
import type { DownloadRequest, DownloadTask, PinPrompt } from './downloads';
import type { WsEvent } from './types';

/** All tasks, newest first — exactly what `GET /downloads` returned last. */
export const downloadTasks = writable<DownloadTask[]>([]);
export const downloadsTotal = writable(0);
/** True only until the first list request settles (skeleton, not every poll). */
export const downloadsLoading = writable(true);
export const downloadsError = writable<string | null>(null);
/** The backend has no P4 routes: the UI must say so instead of showing a fake queue. */
export const downloadsRouteMissing = writable(false);
export const downloadsUpdatedAt = writable(0);

/** Queue = tasks the manager has not finished with (paused included). */
export const activeDownloads = derived(downloadTasks, (tasks) =>
  tasks.filter((task) => isActiveStatus(task.status)),
);
export const historyDownloads = derived(downloadTasks, (tasks) =>
  tasks.filter((task) => !isActiveStatus(task.status)),
);
export const hasActiveDownloads = derived(activeDownloads, (tasks) => tasks.length > 0);

const POLL_INTERVAL_MS = 1000;
const MAX_BACKOFF_MS = 15000;

let subscribers = 0;
let timer: number | undefined;
let requestSeq = 0;
let firstLoadDone = false;
let failures = 0;

/** Poll at 1 Hz; if the server is unreachable, back off instead of hammering it. */
function pollDelay(): number {
  if (failures === 0) return POLL_INTERVAL_MS;
  return Math.min(MAX_BACKOFF_MS, POLL_INTERVAL_MS * 2 ** Math.min(failures, 4));
}

function hasRunningTask(tasks: DownloadTask[]): boolean {
  return tasks.some((task) => isRunningStatus(task.status));
}

function clearTimer(): void {
  if (timer !== undefined) {
    window.clearTimeout(timer);
    timer = undefined;
  }
}

/** Idempotent: a single pending refresh exists at a time. */
function schedule(delay: number): void {
  if (subscribers <= 0 || timer !== undefined) return;
  timer = window.setTimeout(() => {
    timer = undefined;
    void refreshDownloads();
  }, Math.max(0, delay));
}

/** Reads the queue once. Late answers of overlapping requests are dropped. */
export async function refreshDownloads(): Promise<void> {
  const token = ++requestSeq;
  downloadsLoading.set(true);
  try {
    const page = await downloadsApi.list();
    if (token !== requestSeq) return;
    failures = 0;
    downloadsRouteMissing.set(false);
    downloadsError.set(null);
    downloadTasks.set(Array.isArray(page.items) ? page.items : []);
    downloadsTotal.set(
      typeof page.total === 'number' ? page.total : Array.isArray(page.items) ? page.items.length : 0,
    );
    downloadsUpdatedAt.set(Date.now());
  } catch (error) {
    if (token !== requestSeq) return;
    failures += 1;
    const missing = isRouteMissingError(error);
    downloadsRouteMissing.set(missing);
    downloadsError.set(describeDownloadError(error));
    if (missing) {
      // Do not keep a stale queue that a P4-less backend never sent.
      downloadTasks.set([]);
      downloadsTotal.set(0);
    }
  } finally {
    if (token === requestSeq) {
      firstLoadDone = true;
      downloadsLoading.set(false);
      if (subscribers > 0 && hasRunningTask(get(downloadTasks))) {
        schedule(pollDelay());
      }
    }
  }
}

/**
 * Enables the 1 s refresh loop for one consumer. Returns its stop function;
 * polling stops as soon as no subscriber is left or the queue has no active
 * tasks (a WS event or a new task re-arms it).
 */
export function startDownloadsPolling(): () => void {
  subscribers += 1;
  schedule(0);
  let stopped = false;
  return () => {
    if (stopped) return;
    stopped = true;
    subscribers = Math.max(0, subscribers - 1);
    if (subscribers === 0) clearTimer();
  };
}

// A task can appear from a button click in another view or from a WS event;
// re-arm the loop whenever the queue becomes active again.
downloadTasks.subscribe((tasks) => {
  if (subscribers <= 0) return;
  if (hasRunningTask(tasks)) schedule(POLL_INTERVAL_MS);
});

/** Upserts one task (WS payload, POST/cancel/retry answer) into the list. */
export function applyDownloadTask(task: DownloadTask): void {
  let inserted = false;
  downloadTasks.update((tasks) => {
    const index = tasks.findIndex((item) => item.id === task.id);
    if (index === -1) {
      inserted = true;
      return [task, ...tasks];
    }
    const next = tasks.slice();
    next[index] = task;
    return next;
  });
  if (inserted) downloadsTotal.update((total) => total + 1);
}

export function removeDownloadTask(id: number): void {
  let removed = false;
  downloadTasks.update((tasks) => {
    const next = tasks.filter((task) => task.id !== id);
    removed = next.length !== tasks.length;
    return next;
  });
  if (removed) downloadsTotal.update((total) => Math.max(0, total - 1));
}

/**
 * WS bridge for `download.*` events (docs/API.md §7). Returns true when the
 * event belonged to downloads, so the integrator can call it for every event.
 */
export function applyDownloadEvent(event: Pick<WsEvent, 'type' | 'payload'>): boolean {
  if (!event.type.startsWith('download.')) return false;
  const task = event.payload.task as DownloadTask | undefined;
  if (!task || typeof task.id !== 'number') return true;
  applyDownloadTask(task);
  if (
    subscribers > 0 &&
    (event.type === 'download.queued' ||
      event.type === 'download.done' ||
      event.type === 'download.failed')
  ) {
    // Ordering and totals are server-owned: re-read after coarse-grained events.
    void refreshDownloads();
  }
  return true;
}

/** `POST /downloads`; PIN-protected. Upserts the created tasks immediately. */
export async function startDownload(
  request: DownloadRequest,
  askPin?: PinPrompt | null,
): Promise<DownloadTask[]> {
  const tasks = await withPinRetry(() => downloadsApi.create(request), askPin);
  for (const task of tasks) applyDownloadTask(task);
  if (subscribers > 0) schedule(POLL_INTERVAL_MS);
  return tasks;
}

/** `POST /downloads/{id}/cancel`; PIN-protected. */
export async function cancelDownloadTask(
  id: number,
  askPin?: PinPrompt | null,
): Promise<DownloadTask | null> {
  const task = await withPinRetry(() => downloadsApi.cancel(id), askPin);
  if (task && typeof task.id === 'number') applyDownloadTask(task);
  return task;
}

/** `POST /downloads/{id}/retry`; PIN-protected. */
export async function retryDownloadTask(
  id: number,
  askPin?: PinPrompt | null,
): Promise<DownloadTask | null> {
  const task = await withPinRetry(() => downloadsApi.retry(id), askPin);
  if (task && typeof task.id === 'number') applyDownloadTask(task);
  return task;
}

/** `DELETE /downloads/{id}`; PIN-protected, 204 with no body. */
export async function deleteDownloadTask(
  id: number,
  askPin?: PinPrompt | null,
): Promise<void> {
  await withPinRetry(() => downloadsApi.remove(id), askPin);
  removeDownloadTask(id);
}
