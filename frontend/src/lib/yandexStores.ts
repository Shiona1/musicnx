/**
 * P3 stores for Yandex Music screens.
 *
 * Shared App state (`lib/stores.ts`) stays untouched: these stores are owned by
 * the Yandex UI only, and components are free to keep their own local state on
 * top. All reads go through the cookie session; no token is kept in the client.
 */
import { derived, writable } from 'svelte/store';
import { ApiError, yandexApi } from './yandex';
import type {
  WaveAction,
  WaveState,
  YandexAuthStatus,
  YandexPlaylist,
  YandexTrack,
} from './yandex';

/** Session state of the Yandex account (never contains a token). */
export const yandexAuth = writable<YandexAuthStatus | null>(null);
export const yandexAuthLoading = writable(false);
export const yandexAuthError = writable<string | null>(null);
/** True when the server answered 401/403 for a mutation and PIN is required. */
export const yandexPinRequired = writable(false);

export const yandexLikes = writable<YandexTrack[]>([]);
export const yandexLikesTotal = writable(0);
export const yandexPlaylists = writable<YandexPlaylist[]>([]);
export const yandexPlaylistsTotal = writable(0);
export const yandexCatalogLoading = writable(false);
export const yandexCatalogError = writable<string | null>(null);

export const yandexWave = writable<WaveState | null>(null);
export const yandexWaveLoading = writable(false);
export const yandexWaveError = writable<string | null>(null);

export const yandexAuthenticated = derived(
  yandexAuth,
  ($auth) => $auth?.authenticated === true,
);

/** Human readable message for any P3 API failure. */
export function describeYandexError(error: unknown): string {
  if (error instanceof ApiError) {
    switch (error.code) {
      case 'pin_required':
        return 'Действие требует PIN: задайте security.pin в конфиге сервера.';
      case 'auth_required':
        return 'Нужен PIN-код. Введите PIN и повторите действие.';
      case 'invalid_pin':
        return 'Неверный PIN.';
      case 'too_many_attempts':
        return 'Слишком много попыток ввода PIN. Подождите минуту и повторите.';
      case 'invalid_token':
        return 'Яндекс не принял токен. Скопируйте его целиком и попробуйте снова.';
      case 'stream_unavailable':
        return 'Поток недоступен: ссылка Yandex истекла или сервер не отвечает.';
      case 'unavailable':
        return 'Яндекс Музыка недоступна. Проверьте соединение и токен.';
      case 'not_found':
        return 'На стороне Яндекса ничего не найдено.';
      case 'network_error':
        return 'Сервер MusicNX не отвечает: проверьте соединение.';
      default:
        return error.message;
    }
  }
  return 'Не удалось выполнить запрос к Яндекс Музыке.';
}

/** True when the failure is about the mini-PIN session, not Yandex auth. */
export function isPinRequiredError(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    (error.code === 'pin_required' || error.code === 'auth_required')
  );
}

function markAuthFailure(error: unknown): void {
  if (isPinRequiredError(error)) yandexPinRequired.set(true);
}

/** Re-reads the session; the backend never returns the token itself. */
export async function refreshYandexAuth(): Promise<YandexAuthStatus | null> {
  yandexAuthLoading.set(true);
  yandexAuthError.set(null);
  try {
    const status = await yandexApi.authStatus();
    yandexAuth.set(status);
    yandexPinRequired.set(false);
    return status;
  } catch (error) {
    yandexAuthError.set(describeYandexError(error));
    markAuthFailure(error);
    return null;
  } finally {
    yandexAuthLoading.set(false);
  }
}

/** Applies a status returned by the token endpoint / device flow. */
export function applyYandexAuth(status: YandexAuthStatus): void {
  yandexAuth.set(status);
  yandexAuthError.set(null);
  yandexPinRequired.set(false);
}

export async function loadYandexLikes(limit = 24, offset = 0): Promise<void> {
  const page = await yandexApi.likes({ limit, offset });
  yandexLikes.set(page.items);
  yandexLikesTotal.set(page.total);
}

export async function loadYandexPlaylists(): Promise<void> {
  const page = await yandexApi.playlists();
  yandexPlaylists.set(page.items);
  yandexPlaylistsTotal.set(page.total);
}

/**
 * Loads both catalog sections. A failing section must not hide the other one,
 * so failures are aggregated instead of aborting on the first rejection.
 */
export async function refreshYandexCatalogs(limit = 24): Promise<void> {
  yandexCatalogLoading.set(true);
  yandexCatalogError.set(null);
  const failures: unknown[] = [];
  const likes = loadYandexLikes(limit).catch((error: unknown) => failures.push(error));
  const playlists = loadYandexPlaylists().catch((error: unknown) => failures.push(error));
  await Promise.all([likes, playlists]);
  if (failures.length > 0) {
    yandexCatalogError.set(describeYandexError(failures[0]));
    markAuthFailure(failures[0]);
  }
  yandexCatalogLoading.set(false);
}

export async function startYandexWave(): Promise<WaveState | null> {
  yandexWaveLoading.set(true);
  yandexWaveError.set(null);
  try {
    const wave = await yandexApi.waveStart();
    yandexWave.set(wave);
    return wave;
  } catch (error) {
    yandexWaveError.set(describeYandexError(error));
    markAuthFailure(error);
    return null;
  } finally {
    yandexWaveLoading.set(false);
  }
}

/**
 * Sends like/dislike/skip feedback. The server refills the station when the
 * batch runs low, so the freshly returned WaveState is the only source of truth.
 */
export async function sendYandexWaveFeedback(
  action: WaveAction,
  trackId: number,
  playedSeconds = 0,
): Promise<WaveState | null> {
  yandexWaveError.set(null);
  try {
    const wave = await yandexApi.waveFeedback({
      action,
      track_id: trackId,
      played_seconds: playedSeconds,
    });
    yandexWave.set(wave);
    return wave;
  } catch (error) {
    yandexWaveError.set(describeYandexError(error));
    markAuthFailure(error);
    return null;
  }
}

/** Drops Yandex data after logout so no stale catalog survives on screen. */
export function resetYandexStores(): void {
  yandexLikes.set([]);
  yandexLikesTotal.set(0);
  yandexPlaylists.set([]);
  yandexPlaylistsTotal.set(0);
  yandexCatalogError.set(null);
  yandexCatalogLoading.set(false);
  yandexWave.set(null);
  yandexWaveError.set(null);
  yandexWaveLoading.set(false);
  yandexPinRequired.set(false);
}
