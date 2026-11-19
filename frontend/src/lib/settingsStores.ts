/**
 * P8 settings + service stores — the only client-side source of truth for the
 * Settings panel.
 *
 * App-wide stores (`lib/stores.ts`) stay untouched: this module owns the server
 * snapshot, its load/save state, the read-only PIN/localhost facts and the
 * «Сервис» section (backup list, backup/maintenance actions).
 *
 * There is no settings WebSocket event in docs/API.md §12, so nothing here
 * listens for push updates: the panel re-reads `GET /settings` on mount and after
 * every save (a `PUT` that does not echo a snapshot falls back to a GET). That is
 * deliberate — a newer backend cannot silently desynchronise the form.
 *
 * Load failures are kept in the stores (`settingsError`, `serviceError`) so the
 * panel can render them without guessing. Mutation failures are *thrown* to the
 * caller instead: the panel owns the PIN dialog and decides how to phrase a
 * cancelled prompt, while the stores only track busy flags.
 *
 * A `404` means this backend build has no P8 routes: `settingsRouteMissing` /
 * `serviceRouteMissing` let the panel hide the form and the service card instead
 * of faking switches that cannot save anything (AGENTS.md §12).
 */
import { writable } from 'svelte/store';
import { api, ApiError } from './api';
import {
  describeServiceError,
  describeSettingsError,
  isServiceRouteMissing,
  isSettingsRouteMissing,
  serviceApi,
  settingsApi,
  withSettingsPinRetry,
} from './settings';
import type {
  BackupItem,
  BackupsPage,
  SettingsPinPrompt,
  SettingsSnapshot,
} from './settings';
import type { AuthStatus } from './types';

/** Server-owned settings snapshot; null until the first successful read. */
export const settingsSnapshot = writable<SettingsSnapshot | null>(null);
/** True only until the first settings request settles (skeleton, not every refresh). */
export const settingsLoading = writable(true);
export const settingsError = writable<string | null>(null);
/** The backend has no P8 settings route: the panel must say so honestly. */
export const settingsRouteMissing = writable(false);
export const settingsSaving = writable(false);
export const settingsUpdatedAt = writable(0);

/** `GET /auth/status` facts shown read-only (PIN configured, session, localhost). */
export const authInfo = writable<AuthStatus | null>(null);
export const authInfoError = writable<string | null>(null);

/** Newest backup first; exactly what `GET /system/backups` returned last. */
export const backups = writable<BackupItem[]>([]);
export const backupsTotal = writable(0);
export const backupsDirectory = writable<string | null>(null);
export const serviceLoading = writable(true);
export const serviceError = writable<string | null>(null);
/** The backend answered 404 on `/system/backups`: hide the service card. */
export const serviceRouteMissing = writable(false);
export const serviceBusy = writable<'backup' | 'maintenance' | null>(null);
export const serviceUpdatedAt = writable(0);

let settingsSeq = 0;
let settingsLoadedOnce = false;
let serviceSeq = 0;
let serviceLoadedOnce = false;

/** Reads `GET /settings` once. Answers of overlapping requests are dropped. */
export async function refreshSettings(): Promise<void> {
  const token = ++settingsSeq;
  // The skeleton is for the first read only; later refreshes must not flash it.
  if (!settingsLoadedOnce) settingsLoading.set(true);
  try {
    const next = await settingsApi.get();
    if (token !== settingsSeq) return;
    settingsRouteMissing.set(false);
    settingsError.set(null);
    settingsSnapshot.set(next);
    settingsUpdatedAt.set(Date.now());
  } catch (error) {
    if (token !== settingsSeq) return;
    const missing = isSettingsRouteMissing(error);
    settingsRouteMissing.set(missing);
    settingsError.set(describeSettingsError(error));
    if (missing) {
      // Do not keep stale values a P8-less backend never sent.
      settingsSnapshot.set(null);
    }
  } finally {
    if (token === settingsSeq) {
      settingsLoadedOnce = true;
      settingsLoading.set(false);
    }
  }
}

/**
 * Reads `GET /auth/status` for the read-only «Доступ» card. A failure is not a
 * settings failure: the panel simply says the PIN state is unknown.
 */
export async function refreshAuthInfo(): Promise<void> {
  try {
    authInfo.set(await api.authStatus());
    authInfoError.set(null);
  } catch (error) {
    authInfo.set(null);
    authInfoError.set(
      error instanceof ApiError && error.status === 404
        ? 'Сервер не сообщает статус PIN (404).'
        : 'Статус PIN сейчас недоступен: сервер не ответил.',
    );
  }
}

/** Reads `GET /system/backups` once; a 404 hides the whole «Сервис» block. */
export async function refreshService(): Promise<void> {
  const token = ++serviceSeq;
  if (!serviceLoadedOnce) serviceLoading.set(true);
  try {
    const page: BackupsPage = await serviceApi.backups();
    if (token !== serviceSeq) return;
    serviceRouteMissing.set(false);
    serviceError.set(null);
    backups.set(page.items);
    backupsTotal.set(page.total);
    backupsDirectory.set(page.directory);
    serviceUpdatedAt.set(Date.now());
  } catch (error) {
    if (token !== serviceSeq) return;
    const missing = isServiceRouteMissing(error);
    serviceRouteMissing.set(missing);
    serviceError.set(describeServiceError(error));
    if (missing) {
      backups.set([]);
      backupsTotal.set(0);
      backupsDirectory.set(null);
    }
  } finally {
    if (token === serviceSeq) {
      serviceLoadedOnce = true;
      serviceLoading.set(false);
    }
  }
}

/** Manual «Обновить»: settings, PIN facts and the service probe in parallel. */
export async function refreshAllSettings(): Promise<void> {
  await Promise.all([refreshSettings(), refreshAuthInfo(), refreshService()]);
}

/**
 * `PUT /settings` with the partial whitelist patch; PIN-protected. The store
 * rethrows so the panel can retry a wrong PIN and phrase the failure. A `PUT`
 * without an echoed snapshot re-reads `GET /settings` instead of assuming what
 * the server stored.
 */
export async function saveSettings(
  changes: Record<string, number | boolean>,
  askPin?: SettingsPinPrompt | null,
): Promise<void> {
  if (Object.keys(changes).length === 0) return;
  settingsSaving.set(true);
  try {
    const next = await withSettingsPinRetry(() => settingsApi.update(changes), askPin);
    if (next !== null) {
      settingsRouteMissing.set(false);
      settingsError.set(null);
      settingsSnapshot.set(next);
    } else {
      await refreshSettings();
    }
    settingsUpdatedAt.set(Date.now());
  } finally {
    settingsSaving.set(false);
  }
}

/**
 * `POST /system/backups`; PIN-protected. Returns the created backup when the
 * server echoed one. The list is re-read afterwards: the server owns it.
 */
export async function requestBackup(
  askPin?: SettingsPinPrompt | null,
): Promise<BackupItem | null> {
  serviceBusy.set('backup');
  try {
    const item = await withSettingsPinRetry(() => serviceApi.createBackup(), askPin);
    await refreshService();
    return item;
  } finally {
    serviceBusy.set(null);
  }
}

/**
 * `POST /system/maintenance`; PIN-protected. The payload of `MaintenanceResult`
 * is not interpreted here — the store only reports success and re-reads backups,
 * so the UI never invents counters the server did not send.
 */
export async function requestMaintenance(askPin?: SettingsPinPrompt | null): Promise<void> {
  serviceBusy.set('maintenance');
  try {
    await withSettingsPinRetry(() => serviceApi.maintenance(), askPin);
    await refreshService();
  } finally {
    serviceBusy.set(null);
  }
}
