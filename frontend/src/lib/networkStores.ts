/**
 * P8 network / Wi-Fi stores — the only client-side source of truth for the
 * Network panel.
 *
 * App-wide stores (`lib/stores.ts`) stay untouched. docs/API.md §13 defines no
 * `network.*` WebSocket event, so the panel reads `GET /network/status` on
 * mount and on demand; `POST /network/scan|connect|disconnect` answer with the
 * server's fresh state and these stores adopt exactly that answer.
 *
 * The Wi-Fi password never reaches a store: `connectWifi(ssid, password)` hands
 * it straight to `networkApi.connect` and forgets it. The PIN lives only in the
 * caller-provided prompt. A `404` means this backend build has no P8 network
 * routes: `networkRouteMissing` / `wifiScanRouteMissing` let the panel say so
 * instead of rendering fake controls (AGENTS.md §12).
 *
 * Mutation failures are thrown to the caller so the panel can phrase a
 * cancelled PIN prompt and retry a wrong PIN; read/scan failures are also kept
 * in the stores so the cards can show them in place.
 */
import { get, writable } from 'svelte/store';
import {
  NetworkPinCancelledError,
  describeNetworkError,
  isNetworkPinRequired,
  isNetworkRouteMissing,
  networkApi,
  withNetworkPinRetry,
} from './network';
import type {
  NetworkPinPrompt,
  NetworkStatus,
  WifiNetwork,
  WifiScanPage,
} from './network';

/** Server-owned status from `GET /network/status`; null until a successful read. */
export const networkStatus = writable<NetworkStatus | null>(null);
/** True only until the first status request settles (skeleton, not every refresh). */
export const networkLoading = writable(true);
export const networkError = writable<string | null>(null);
/** The backend has no P8 network routes: the panel must say so honestly. */
export const networkRouteMissing = writable(false);
export const networkUpdatedAt = writable(0);

/** Exactly what the last successful scan returned, connected network first. */
export const wifiNetworks = writable<WifiNetwork[]>([]);
export const wifiNetworksTotal = writable(0);
export const wifiScanBackend = writable<string | null>(null);
export const wifiScanLoading = writable(false);
export const wifiScanError = writable<string | null>(null);
/** The scan route answered 404: scanning is genuinely unavailable. */
export const wifiScanRouteMissing = writable(false);
export const wifiScanUpdatedAt = writable(0);

/** One mutation at a time: connect, disconnect or scan. */
export type NetworkBusyAction = 'scan' | 'connect' | 'disconnect';
export const networkBusy = writable<NetworkBusyAction | null>(null);

let statusSeq = 0;
let statusLoadedOnce = false;
let scanSeq = 0;

/**
 * Adopts a server status answer and mirrors its `connected` flag onto the scan
 * list. The status is the authority; the list itself stays the last scan.
 */
export function applyNetworkStatus(status: NetworkStatus): void {
  networkStatus.set(status);
  networkUpdatedAt.set(Date.now());
  const connectedSsid = status.connected ? status.ssid : null;
  wifiNetworks.update((items) =>
    items.map((item) => ({
      ...item,
      connected: connectedSsid !== null && item.ssid === connectedSsid,
    })),
  );
}

/** Reads `GET /network/status` once. Answers of overlapping requests are dropped. */
export async function refreshNetworkStatus(): Promise<void> {
  const token = ++statusSeq;
  // The skeleton is for the first read only; later refreshes must not flash it.
  if (!statusLoadedOnce) networkLoading.set(true);
  try {
    const status = await networkApi.status();
    if (token !== statusSeq) return;
    networkRouteMissing.set(false);
    networkError.set(null);
    applyNetworkStatus(status);
  } catch (error) {
    if (token !== statusSeq) return;
    const missing = isNetworkRouteMissing(error);
    networkRouteMissing.set(missing);
    networkError.set(describeNetworkError(error));
    if (missing) {
      // Do not keep a state a P8-less backend never sent.
      networkStatus.set(null);
    }
  } finally {
    if (token === statusSeq) {
      statusLoadedOnce = true;
      networkLoading.set(false);
    }
  }
}

/**
 * `POST /network/scan`; PIN-protected. The list is replaced only on success — a
 * failed rescan keeps the previous server answer (with an error above it)
 * rather than pretending the airwaves are empty.
 */
export async function scanWifi(askPin?: NetworkPinPrompt | null): Promise<WifiScanPage> {
  const token = ++scanSeq;
  networkBusy.set('scan');
  wifiScanLoading.set(true);
  wifiScanError.set(null);
  try {
    const page = await withNetworkPinRetry(() => networkApi.scan(), askPin);
    if (token !== scanSeq) return page;
    wifiScanRouteMissing.set(false);
    wifiScanError.set(null);
    wifiNetworks.set(page.items);
    wifiNetworksTotal.set(page.total);
    wifiScanBackend.set(page.backend);
    wifiScanUpdatedAt.set(Date.now());
    return page;
  } catch (error) {
    if (token !== scanSeq) throw error;
    const missing = isNetworkRouteMissing(error);
    wifiScanRouteMissing.set(missing);
    if (missing) {
      wifiNetworks.set([]);
      wifiNetworksTotal.set(0);
      wifiScanBackend.set(null);
    }
    // A PIN prompt/retry is the panel's concern; only real scan failures stay
    // in the card as an error.
    if (!(error instanceof NetworkPinCancelledError) && !isNetworkPinRequired(error)) {
      wifiScanError.set(describeNetworkError(error));
    }
    throw error;
  } finally {
    if (token === scanSeq) {
      wifiScanLoading.set(false);
      if (get(networkBusy) === 'scan') networkBusy.set(null);
    }
  }
}

/**
 * `POST /network/connect`; PIN-protected. The password argument exists only for
 * the duration of this call and is handed straight to the API client. The
 * server answer is adopted as-is, including its SSID/IP/signal.
 */
export async function connectWifi(
  ssid: string,
  password: string,
  askPin?: NetworkPinPrompt | null,
): Promise<NetworkStatus> {
  networkBusy.set('connect');
  try {
    const status = await withNetworkPinRetry(
      () => networkApi.connect(ssid, password),
      askPin,
    );
    networkRouteMissing.set(false);
    networkError.set(null);
    wifiScanError.set(null);
    applyNetworkStatus(status);
    return status;
  } catch (error) {
    if (isNetworkRouteMissing(error)) networkRouteMissing.set(true);
    throw error;
  } finally {
    if (get(networkBusy) === 'connect') networkBusy.set(null);
  }
}

/** `POST /network/disconnect`; PIN-protected. Adopts the returned status. */
export async function disconnectWifi(
  askPin?: NetworkPinPrompt | null,
): Promise<NetworkStatus> {
  networkBusy.set('disconnect');
  try {
    const status = await withNetworkPinRetry(() => networkApi.disconnect(), askPin);
    networkRouteMissing.set(false);
    networkError.set(null);
    wifiScanError.set(null);
    applyNetworkStatus(status);
    return status;
  } catch (error) {
    if (isNetworkRouteMissing(error)) networkRouteMissing.set(true);
    throw error;
  } finally {
    if (get(networkBusy) === 'disconnect') networkBusy.set(null);
  }
}
