/**
 * Yandex Music (P3) REST client.
 *
 * Mirrors docs/API.md §6. The browser never holds a Yandex token: every call is
 * same-origin and relies on the HttpOnly `musicnx_session` cookie, so nothing
 * here touches localStorage. Stream URLs are plain `/api/v1/stream/{id}` links:
 * the server resolves the upstream direct link, the browser only receives audio.
 *
 * PIN-protected mutations may answer `401 auth_required` / `403 pin_required`;
 * callers open the PIN dialog and retry the same operation (see Setup.svelte).
 */
import { ApiError } from './api';
import type { AlbumPage, AlbumSummary, ArtistPage, ArtistSummary, Track } from './types';

export { ApiError };

export const API_BASE = '/api/v1';

/**
 * Yandex auth session. `authenticated:false` means the other fields are null;
 * the token itself is never part of the payload.
 */
export interface YandexAuthStatus {
  authenticated: boolean;
  login: string | null;
  expires_at: string | null;
  source: string | null;
}

/** P3 naming from the task contract; kept as an alias for discoverability. */
export type AuthStatus = YandexAuthStatus;

/** `POST /yandex/auth/device/start` — QR is a data URI built by the backend. */
export interface DeviceStart {
  flow_id: string;
  user_code: string;
  verification_url: string;
  expires_in: number;
  interval: number;
  qr_data_uri: string;
}

export type DeviceState = 'pending' | 'authorized' | 'expired' | 'denied' | 'error';

/** `GET /yandex/auth/device/status?flow_id=…` — polled every `interval` seconds. */
export interface DeviceStatus {
  state: DeviceState;
  flow_id: string;
  login: string | null;
  expires_at: string | null;
  error: string | null;
}

/** TrackDTO plus the P3 additions from docs/API.md §6.2. */
export interface YandexTrack extends Track {
  external_id?: string | null;
  /** 30-second preview: the UI shows a «30 сек» badge. */
  preview?: boolean;
  liked?: boolean | null;
}

/** Page of Yandex tracks (`GET /yandex/likes`, playlist tracks). */
export interface TrackPage {
  items: YandexTrack[];
  total: number;
  limit: number;
  offset: number;
}

export interface YandexPlaylist {
  id: string;
  kind: number;
  title: string;
  track_count: number | null;
  cover_url: string | null;
  owner: string | null;
}

/** P3 naming from the task contract alias. */
export type Playlist = YandexPlaylist;

export interface PlaylistPage {
  items: YandexPlaylist[];
  total: number;
}

export interface PlaylistDetail {
  playlist: YandexPlaylist;
  tracks: TrackPage;
}

export interface AlbumDetail {
  album: AlbumSummary;
  tracks: YandexTrack[];
}

export interface ArtistDetail {
  artist: ArtistSummary;
  tracks: YandexTrack[];
}

export type SearchType = 'all' | 'tracks' | 'albums' | 'artists' | 'playlists';

export interface YandexSearchResults {
  tracks: TrackPage;
  albums: AlbumPage;
  artists: ArtistPage;
  playlists: PlaylistPage;
}

/** `POST /yandex/wave/start` and `POST /yandex/wave/feedback` response. */
export interface WaveState {
  station: string;
  batch_id: string;
  session_id: string | null;
  tracks: YandexTrack[];
  current_index: number;
  updated_at: string;
}

export type WaveAction = 'like' | 'dislike' | 'skip';

export interface WaveFeedback {
  action: WaveAction;
  track_id: number;
  played_seconds: number;
}

export interface PinLoginResult {
  authenticated: boolean;
  pin_configured: boolean;
}

/** Response body of logout is not frozen beyond `authenticated`; refetch status. */
export interface LogoutResult {
  authenticated: boolean;
}

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) search.set(key, String(value));
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
      // The session lives in an HttpOnly cookie; a Yandex token never reaches JS.
      credentials: 'same-origin',
    });
  } catch {
    throw new ApiError('Сервер MusicNX недоступен: проверьте соединение.', 'network_error', 0);
  }

  if (!response.ok) throw await parseError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const yandexApi = {
  authStatus(): Promise<YandexAuthStatus> {
    return request('/yandex/auth/status');
  },

  deviceStart(): Promise<DeviceStart> {
    return request('/yandex/auth/device/start', { method: 'POST', body: '{}' });
  },

  deviceStatus(flowId: string): Promise<DeviceStatus> {
    return request(`/yandex/auth/device/status?${query({ flow_id: flowId })}`);
  },

  loginToken(token: string): Promise<YandexAuthStatus> {
    return request('/yandex/auth/token', { method: 'POST', body: JSON.stringify({ token }) });
  },

  logout(): Promise<LogoutResult> {
    return request('/yandex/auth/logout', { method: 'POST', body: '{}' });
  },

  /** Mini-PIN from docs/API.md §4; sets the session cookie used by mutations. */
  loginPin(pin: string): Promise<PinLoginResult> {
    return request('/auth/pin', { method: 'POST', body: JSON.stringify({ pin }) });
  },

  likes(params: { limit?: number; offset?: number } = {}): Promise<TrackPage> {
    return request(
      `/yandex/likes?${query({ limit: params.limit ?? 50, offset: params.offset ?? 0 })}`,
    );
  },

  playlists(): Promise<PlaylistPage> {
    return request('/yandex/playlists');
  },

  playlist(
    playlistId: string,
    params: { limit?: number; offset?: number } = {},
  ): Promise<PlaylistDetail> {
    return request(
      `/yandex/playlists/${encodeURIComponent(playlistId)}?${query({
        limit: params.limit ?? 50,
        offset: params.offset ?? 0,
      })}`,
    );
  },

  album(albumId: string | number): Promise<AlbumDetail> {
    return request(`/yandex/album/${encodeURIComponent(String(albumId))}`);
  },

  createPlaylist(title: string, visibility: 'private' | 'public' = 'private'): Promise<YandexPlaylist> {
    return request('/yandex/playlists', {
      method: 'POST',
      body: JSON.stringify({ title, visibility }),
    });
  },

  renamePlaylist(playlistId: string, title: string): Promise<YandexPlaylist> {
    return request(`/yandex/playlists/${encodeURIComponent(playlistId)}`, {
      method: 'PATCH',
      body: JSON.stringify({ title }),
    });
  },

  deletePlaylist(playlistId: string): Promise<void> {
    return request(`/yandex/playlists/${encodeURIComponent(playlistId)}`, {
      method: 'DELETE',
    });
  },

  addPlaylistTrack(playlistId: string, trackId: number, at = 0): Promise<YandexPlaylist> {
    return request(`/yandex/playlists/${encodeURIComponent(playlistId)}/tracks`, {
      method: 'POST',
      body: JSON.stringify({ track_id: trackId, at }),
    });
  },

  removePlaylistTrack(playlistId: string, position: number): Promise<YandexPlaylist> {
    return request(
      `/yandex/playlists/${encodeURIComponent(playlistId)}/tracks?${query({ position })}`,
      { method: 'DELETE' },
    );
  },

  artist(artistId: string | number): Promise<ArtistDetail> {
    return request(`/yandex/artist/${encodeURIComponent(String(artistId))}`);
  },

  artistTracks(
    artistId: string | number,
    params: { limit?: number; offset?: number } = {},
  ): Promise<TrackPage> {
    return request(
      `/yandex/artist/${encodeURIComponent(String(artistId))}/tracks?${query({
        limit: params.limit ?? 25,
        offset: params.offset ?? 0,
      })}`,
    );
  },

  search(params: {
    q: string;
    type?: SearchType;
    limit?: number;
    offset?: number;
  }): Promise<YandexSearchResults> {
    return request(
      `/yandex/search?${query({
        q: params.q,
        type: params.type ?? 'all',
        limit: params.limit ?? 25,
        offset: params.offset ?? 0,
      })}`,
    );
  },

  waveStart(): Promise<WaveState> {
    return request('/yandex/wave/start', { method: 'POST', body: '{}' });
  },

  waveFeedback(feedback: WaveFeedback): Promise<WaveState> {
    return request('/yandex/wave/feedback', { method: 'POST', body: JSON.stringify(feedback) });
  },
};

/** Proxied/cached Yandex cover (`GET /yandex/cover?uri=…&size=…`). */
export function yandexCoverUrl(
  uri: string | null | undefined,
  size: 64 | 300 | 1000 = 300,
): string | null {
  if (!uri) return null;
  return `${API_BASE}/yandex/cover?${query({ uri, size })}`;
}

/**
 * Same-origin stream URL for `<audio>`. docs/API.md §6.4 exposes the track
 * bytes under `/api/v1/stream/{track_id}`; cookies for that origin are sent by
 * the media element automatically and no token is ever put in the URL.
 */
export function streamUrl(trackId: number): string {
  return `${API_BASE}/stream/${trackId}`;
}

export function isPreviewTrack(track: Pick<YandexTrack, 'preview'> | null | undefined): boolean {
  return track?.preview === true;
}

/** Feedback reports whole seconds played; WaveCard feeds its position prop. */
export function playedSeconds(positionMs: number | null | undefined): number {
  if (!positionMs || positionMs <= 0) return 0;
  return Math.floor(positionMs / 1000);
}
