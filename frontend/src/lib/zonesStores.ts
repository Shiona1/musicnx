/**
 * P7 zones & Bluetooth stores — the client-side snapshot of `GET /zones` and
 * `GET /bluetooth/devices` for the Zones panel.
 *
 * App-wide stores (`lib/stores.ts`) stay untouched: this module owns the zone
 * list, the Bluetooth device list, the adapter kind, the polling loop and the
 * WebSocket bridge.
 *
 * WS integration (done by the App integrator, App.svelte is not edited here):
 *   case 'zones.updated':
 *   case 'zone.changed':
 *   case 'bt.device_*':
 *     applyZonesEvent(event);   // payload zone/device is merged, REST re-reads
 *     break;
 *
 * Polling is reference-counted: `startZonesPolling()` on mount returns a stop
 * function. The first consumer reads both sections once; afterwards
 * `GET /zones` is re-read every 20 s (backoff up to 60 s while the server is
 * unreachable) so a zone appearing/disappearing outside WS is noticed. The
 * Bluetooth list is re-read after every Bluetooth action and on `bt.device_*`
 * events; an explicit poll window follows `POST /bluetooth/scan` because
 * discovery is asynchronous and the WS may be down.
 *
 * The server remains the source of truth: mutations adopt the returned zone /
 * device, then re-read the list (activation moves the `active` flag between
 * zones, and an A2DP sink registers itself as a zone only on the server).
 * Volume/offset slider traffic is de-duplicated per zone so an out-of-order
 * answer cannot resurrect a stale value.
 */
import { derived, get, writable } from 'svelte/store';
import {
  compareBluetoothDevices,
  compareZones,
  describeZonesError,
  isZonesRouteMissing,
  normalizeAudioZone,
  normalizeBluetoothDevice,
  normalizeZonePage,
  zonesApi,
  withZonesPinRetry,
} from './zones';
import type {
  AudioZone,
  BluetoothDevice,
  BluetoothDevicePage,
  ZonesPinPrompt,
} from './zones';
import type { WsEvent } from './types';

/** Zones snapshot as last returned by `GET /zones`. */
export const zones = writable<AudioZone[]>([]);
export const zonesTotal = writable(0);
/** True only until the first zones request settles (skeleton, not every poll). */
export const zonesLoading = writable(true);
export const zonesError = writable<string | null>(null);
/** The backend has no P7 routes: the UI must say so instead of faking zones. */
export const zonesRouteMissing = writable(false);
export const zonesUpdatedAt = writable(0);
/** Backend selected by the server and whether real mode fell back to mock. */
export const zonesBackend = writable<string | null>(null);
export const zonesDegraded = writable(false);
/** The active output zone, or null when the server reported none. */
export const activeZone = derived(zones, (items) => items.find((zone) => zone.active) ?? null);

/** Bluetooth devices as last returned by `GET /bluetooth/devices`. */
export const bluetoothDevices = writable<BluetoothDevice[]>([]);
export const bluetoothDevicesTotal = writable(0);
/** `mock | real` from the server; null until the first successful read. */
export const bluetoothBackend = writable<string | null>(null);
export const bluetoothLoading = writable(true);
export const bluetoothError = writable<string | null>(null);
export const bluetoothRouteMissing = writable(false);
export const bluetoothUpdatedAt = writable(0);
/** True while `POST /bluetooth/scan` and its bounded refresh window run. */
export const bluetoothScanning = writable(false);

export type ZonesBusyAction =
  | 'activate'
  | 'volume'
  | 'offset'
  | 'scan'
  | 'pair'
  | 'connect'
  | 'forget';

/** One in-flight mutation; the panel disables the controls it would race. */
export interface ZonesBusyState {
  action: ZonesBusyAction;
  /** Zone id for zone actions, device address for Bluetooth actions. */
  targetId: string | null;
}

export const zonesBusy = writable<ZonesBusyState | null>(null);

const ZONES_POLL_MS = 20_000;
const MAX_BACKOFF_MS = 60_000;
/** `POST /bluetooth/scan` discovery window and cadence of list re-reads. */
const SCAN_WINDOW_MS = 8_000;
const SCAN_POLL_MS = 2_000;

let subscribers = 0;
let timer: number | undefined;
let failures = 0;
let zonesSeq = 0;
let bluetoothSeq = 0;
let scanSeq = 0;
/** Bumped by WS `bt.*` events so the scan window can stop early. */
let bluetoothEventSeq = 0;

const volumeSeq = new Map<string, number>();
const offsetSeq = new Map<string, number>();

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

function nextDelay(): number {
  if (failures === 0) return ZONES_POLL_MS;
  return Math.min(MAX_BACKOFF_MS, ZONES_POLL_MS * 2 ** Math.min(failures, 3));
}

function clearTimer(): void {
  if (timer !== undefined) {
    window.clearTimeout(timer);
    timer = undefined;
  }
}

/** Idempotent: at most one pending zones refresh exists at a time. */
function schedule(delayMs: number): void {
  if (subscribers <= 0 || timer !== undefined || get(zonesRouteMissing)) return;
  timer = window.setTimeout(() => {
    timer = undefined;
    void refreshZones();
  }, Math.max(0, delayMs));
}

/** Replaces a pending refresh with one at `delayMs` (cadence may change). */
function rearm(delayMs: number): void {
  clearTimer();
  if (subscribers <= 0 || get(zonesRouteMissing)) return;
  schedule(delayMs);
}

/** Replaces one zone in the list, keeping the server's ordering rules. */
export function upsertZone(zone: AudioZone): void {
  let inserted = false;
  zones.update((items) => {
    const index = items.findIndex((item) => item.id === zone.id);
    if (index === -1) {
      inserted = true;
      return [...items, zone].sort(compareZones);
    }
    const next = items.slice();
    next[index] = zone;
    return next.sort(compareZones);
  });
  if (inserted) zonesTotal.update((total) => total + 1);
}

/** Merges one Bluetooth device by address. */
export function upsertBluetoothDevice(device: BluetoothDevice): void {
  let inserted = false;
  bluetoothDevices.update((items) => {
    const index = items.findIndex((item) => item.address === device.address);
    if (index === -1) {
      inserted = true;
      return [...items, device].sort(compareBluetoothDevices);
    }
    const next = items.slice();
    next[index] = device;
    return next.sort(compareBluetoothDevices);
  });
  if (inserted) bluetoothDevicesTotal.update((total) => total + 1);
}

function applyDevicePage(page: BluetoothDevicePage): void {
  bluetoothDevices.set(page.items);
  bluetoothDevicesTotal.set(page.total);
  bluetoothBackend.set(page.backend);
  bluetoothUpdatedAt.set(Date.now());
}

/** Reads `GET /zones` once. Overlapping answers are dropped (last one wins). */
export async function refreshZones(): Promise<void> {
  const token = ++zonesSeq;
  // The skeleton is for the first read only; later polls must not flash it.
  if (get(zonesUpdatedAt) === 0) zonesLoading.set(true);
  try {
    const page = await zonesApi.list();
    if (token !== zonesSeq) return;
    failures = 0;
    zonesRouteMissing.set(false);
    zonesError.set(null);
    zones.set(page.items);
    zonesTotal.set(page.total);
    zonesBackend.set(page.backend ?? null);
    zonesDegraded.set(page.degraded === true);
    zonesUpdatedAt.set(Date.now());
  } catch (error) {
    if (token !== zonesSeq) return;
    failures += 1;
    const missing = isZonesRouteMissing(error);
    zonesRouteMissing.set(missing);
    zonesError.set(describeZonesError(error));
    if (missing) {
      // Do not keep a stale zone list that a P7-less backend never sent.
      zones.set([]);
      zonesTotal.set(0);
      zonesUpdatedAt.set(0);
    }
  } finally {
    if (token === zonesSeq) {
      zonesLoading.set(false);
      rearm(nextDelay());
    }
  }
}

/** Reads `GET /bluetooth/devices` once. */
export async function refreshBluetoothDevices(): Promise<void> {
  const token = ++bluetoothSeq;
  if (get(bluetoothUpdatedAt) === 0) bluetoothLoading.set(true);
  try {
    const page = await zonesApi.devices();
    if (token !== bluetoothSeq) return;
    bluetoothRouteMissing.set(false);
    bluetoothError.set(null);
    applyDevicePage(page);
  } catch (error) {
    if (token !== bluetoothSeq) return;
    const missing = isZonesRouteMissing(error);
    bluetoothRouteMissing.set(missing);
    bluetoothError.set(describeZonesError(error));
    if (missing) {
      bluetoothDevices.set([]);
      bluetoothDevicesTotal.set(0);
      bluetoothBackend.set(null);
      bluetoothUpdatedAt.set(0);
    }
  } finally {
    if (token === bluetoothSeq) bluetoothLoading.set(false);
  }
}

/** Manual "Обновить": both sections in parallel. */
export async function refreshZonesSection(): Promise<void> {
  await Promise.all([refreshZones(), refreshBluetoothDevices()]);
}

/**
 * Enables polling for one consumer. Returns its stop function; polling stops
 * as soon as no subscriber is left. A WS event or a manual refresh re-arms it.
 */
export function startZonesPolling(): () => void {
  subscribers += 1;
  // The first consumer also needs the Bluetooth list, which is not polled.
  if (subscribers === 1) void refreshZonesSection();
  let stopped = false;
  return () => {
    if (stopped) return;
    stopped = true;
    subscribers = Math.max(0, subscribers - 1);
    if (subscribers === 0) clearTimer();
  };
}

/**
 * `POST /bluetooth/scan`. Discovery is asynchronous, so the device list is
 * re-read for a bounded window (or until a `bt.*` event arrives). The window
 * only covers a *read* — it never pretends to know scan progress.
 */
export async function requestBluetoothScan(askPin?: ZonesPinPrompt | null): Promise<void> {
  const token = ++scanSeq;
  const eventMark = bluetoothEventSeq;
  bluetoothScanning.set(true);
  bluetoothError.set(null);
  zonesBusy.set({ action: 'scan', targetId: null });
  try {
    const page = await withZonesPinRetry(() => zonesApi.scan(), askPin);
    if (token !== scanSeq) return;
    if (page !== null) applyDevicePage(page);
    await refreshBluetoothDevices();

    const deadline = Date.now() + SCAN_WINDOW_MS;
    while (
      token === scanSeq &&
      Date.now() < deadline &&
      bluetoothEventSeq === eventMark &&
      get(bluetoothScanning)
    ) {
      await delay(SCAN_POLL_MS);
      if (token !== scanSeq || bluetoothEventSeq !== eventMark) break;
      await refreshBluetoothDevices();
    }
  } catch (error) {
    if (token !== scanSeq) return;
    bluetoothError.set(describeZonesError(error));
    throw error;
  } finally {
    if (token === scanSeq) {
      bluetoothScanning.set(false);
      const current = get(zonesBusy);
      if (current !== null && current.action === 'scan') zonesBusy.set(null);
    }
  }
}

/** Stops the scan refresh window (panel unmounted, or user asked). */
export function stopBluetoothScan(): void {
  scanSeq += 1;
  bluetoothScanning.set(false);
  const current = get(zonesBusy);
  if (current !== null && current.action === 'scan') zonesBusy.set(null);
}

/**
 * `POST /zones/{id}/activate`; PIN-protected. The server moves the mpv
 * sink-input, so the track keeps playing — nothing is optimistically toggled
 * here: the returned zone is adopted, then the whole list is re-read because
 * the `active` flag moved between zones.
 */
export async function requestActivateZone(
  zoneId: string,
  askPin?: ZonesPinPrompt | null,
): Promise<void> {
  zonesBusy.set({ action: 'activate', targetId: zoneId });
  zonesError.set(null);
  try {
    const zone = await withZonesPinRetry(() => zonesApi.activate(zoneId), askPin);
    if (zone !== null) upsertZone(zone);
    await refreshZones();
  } catch (error) {
    zonesError.set(describeZonesError(error));
    throw error;
  } finally {
    const current = get(zonesBusy);
    if (current !== null && current.action === 'activate' && current.targetId === zoneId) {
      zonesBusy.set(null);
    }
  }
}

/**
 * `POST /zones/{id}/volume {volume}`; PIN-protected. Per-zone sequence guards
 * drop a stale answer when the user is still dragging the slider.
 */
export async function requestZoneVolume(
  zoneId: string,
  volume: number,
  askPin?: ZonesPinPrompt | null,
): Promise<void> {
  const token = (volumeSeq.get(zoneId) ?? 0) + 1;
  volumeSeq.set(zoneId, token);
  zonesBusy.set({ action: 'volume', targetId: zoneId });
  zonesError.set(null);
  try {
    const zone = await withZonesPinRetry(() => zonesApi.setVolume(zoneId, volume), askPin);
    if (token !== volumeSeq.get(zoneId)) return;
    if (zone !== null) upsertZone(zone);
    else await refreshZones();
  } catch (error) {
    if (token !== volumeSeq.get(zoneId)) return;
    zonesError.set(describeZonesError(error));
    throw error;
  } finally {
    if (token === volumeSeq.get(zoneId)) {
      const current = get(zonesBusy);
      if (current !== null && current.action === 'volume' && current.targetId === zoneId) {
        zonesBusy.set(null);
      }
    }
  }
}

/** `POST /zones/{id}/offset {offset_ms}`; PIN-protected; per-zone sequence. */
export async function requestZoneOffset(
  zoneId: string,
  offsetMs: number,
  askPin?: ZonesPinPrompt | null,
): Promise<void> {
  const token = (offsetSeq.get(zoneId) ?? 0) + 1;
  offsetSeq.set(zoneId, token);
  zonesBusy.set({ action: 'offset', targetId: zoneId });
  zonesError.set(null);
  try {
    const zone = await withZonesPinRetry(() => zonesApi.setOffset(zoneId, offsetMs), askPin);
    if (token !== offsetSeq.get(zoneId)) return;
    if (zone !== null) upsertZone(zone);
    else await refreshZones();
  } catch (error) {
    if (token !== offsetSeq.get(zoneId)) return;
    zonesError.set(describeZonesError(error));
    throw error;
  } finally {
    if (token === offsetSeq.get(zoneId)) {
      const current = get(zonesBusy);
      if (current !== null && current.action === 'offset' && current.targetId === zoneId) {
        zonesBusy.set(null);
      }
    }
  }
}

/** Shared body of the PIN-protected Bluetooth mutations. */
async function runBluetoothMutation(
  action: 'pair' | 'connect' | 'forget',
  address: string,
  run: () => Promise<void>,
): Promise<void> {
  zonesBusy.set({ action, targetId: address });
  bluetoothError.set(null);
  try {
    await run();
    await refreshBluetoothDevices();
    // An A2DP sink can register as a zone; pair/forget can remove it again.
    await refreshZones();
  } catch (error) {
    bluetoothError.set(describeZonesError(error));
    throw error;
  } finally {
    const current = get(zonesBusy);
    if (current !== null && current.action === action && current.targetId === address) {
      zonesBusy.set(null);
    }
  }
}

/** `POST /bluetooth/pair {address}`; PIN-protected. */
export async function requestBluetoothPair(
  address: string,
  askPin?: ZonesPinPrompt | null,
): Promise<void> {
  await runBluetoothMutation('pair', address, async () => {
    const device = await withZonesPinRetry(() => zonesApi.pair(address), askPin);
    if (device !== null) upsertBluetoothDevice(device);
  });
}

/** `POST /bluetooth/connect {address}`; PIN-protected; A2DP zone appears. */
export async function requestBluetoothConnect(
  address: string,
  askPin?: ZonesPinPrompt | null,
): Promise<void> {
  await runBluetoothMutation('connect', address, async () => {
    const device = await withZonesPinRetry(() => zonesApi.connect(address), askPin);
    if (device !== null) upsertBluetoothDevice(device);
  });
}

/** `POST /bluetooth/forget {address}`; PIN-protected; drops pairing. */
export async function requestBluetoothForget(
  address: string,
  askPin?: ZonesPinPrompt | null,
): Promise<void> {
  await runBluetoothMutation('forget', address, async () => {
    const device = await withZonesPinRetry(() => zonesApi.forget(address), askPin);
    if (device !== null) upsertBluetoothDevice(device);
    // Confirmed by the server: drop it locally, then let the re-read confirm.
    bluetoothDevices.update((items) => items.filter((item) => item.address !== address));
  });
}

/**
 * WS bridge for `zones.updated` / `zone.changed` / `bt.device_*`
 * (docs/API.md §11, docs/P7_NOTES.md §1): `zones.updated` carries the whole
 * `{"zones": ZonePage}` page, `bt.device_paired|connected|forgotten` carries
 * `{"device": BluetoothDevice}`. A full page is adopted as-is; otherwise the
 * known zone/device is merged and REST is re-read, which stays the source of
 * truth for ordering, totals and the active flag.
 */
export function applyZonesEvent(event: Pick<WsEvent, 'type' | 'payload'>): boolean {
  const payload = event.payload ?? {};

  if (
    event.type === 'zones.updated' ||
    event.type === 'zone.changed' ||
    event.type === 'zone.updated'
  ) {
    const page = normalizeZonePage(payload.zones);
    if (page.items.length > 0) {
      zones.set(page.items);
      zonesTotal.set(page.total);
      zonesUpdatedAt.set(Date.now());
    } else {
      const zone = normalizeAudioZone(payload.zone) ?? normalizeAudioZone(payload);
      if (zone !== null) upsertZone(zone);
    }
    if (subscribers > 0) void refreshZones();
    return true;
  }

  if (event.type.startsWith('bt.')) {
    const device =
      normalizeBluetoothDevice(payload.device) ?? normalizeBluetoothDevice(payload);
    if (device !== null) upsertBluetoothDevice(device);
    bluetoothEventSeq += 1;
    if (subscribers > 0) void refreshBluetoothDevices();
    return true;
  }

  return false;
}
