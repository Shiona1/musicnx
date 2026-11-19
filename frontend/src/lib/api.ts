import type {
  AlbumDetail,
  AlbumPage,
  ArtistDetail,
  ArtistPage,
  AuthStatus,
  ImportJob,
  LibraryPage,
  HistoryPage,
  PlayerState,
  RuntimeInfo,
  SystemShare,
  Track,
  QueueState,
  SearchResults,
  SystemMetrics,
  SystemStatus,
} from './types';

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(message: string, code: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {  const headers = new Headers(init.headers);
  headers.set('Accept', 'application/json');
  if (init.body !== undefined) {
    headers.set('Content-Type', 'application/json');
  }
  const response = await fetch(`/api/v1${path}`, { ...init, headers });
  if (!response.ok) {
    let message = `Ошибка запроса (HTTP ${response.status})`;
    let code = 'http_error';
    try {
      const body = (await response.json()) as { error?: { code?: string; message?: string } };
      if (body.error?.message) message = body.error.message;
      if (body.error?.code) code = body.error.code;
    } catch {
      // Non-JSON error body: keep the HTTP status message.
    }
    throw new ApiError(message, code, response.status);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export interface UploadedAudioFile {
  name: string;
  size: number;
  path: string;
}

/**
 * Upload one audio file as multipart/form-data with progress.
 *
 * XMLHttpRequest is used instead of fetch because it reports upload progress
 * reliably in every browser; the server caps the size and stores the file under
 * the first import root.
 */
export function uploadAudioFile(
  file: File,
  onProgress?: (sent: number, total: number) => void,
): Promise<UploadedAudioFile> {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append('file', file, file.name);
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/v1/library/upload');
    xhr.responseType = 'text';
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress?.(event.loaded, event.total);
    };
    xhr.onerror = () =>
      reject(new ApiError('Сервер MusicNX недоступен: проверьте соединение.', 'network_error', 0));
    xhr.onload = () => {
      let body: { file?: UploadedAudioFile; error?: { code?: string; message?: string } } = {};
      try {
        body = JSON.parse(xhr.responseText || '{}') as typeof body;
      } catch {
        // Keep the HTTP status error below.
      }
      if (xhr.status >= 200 && xhr.status < 300 && body.file) {
        resolve(body.file);
        return;
      }
      reject(
        new ApiError(
          body.error?.message ?? `Не удалось загрузить файл (HTTP ${xhr.status})`,
          body.error?.code ?? 'upload_failed',
          xhr.status,
        ),
      );
    };
    xhr.send(form);
  });
}

export const api = {
  playerState: () => request<PlayerState>('/player/state'),  play: (trackId?: number) =>
    request<PlayerState>('/player/play', {
      method: 'POST',
      body: JSON.stringify(trackId === undefined ? {} : { track_id: trackId }),
    }),
  pause: () => request<PlayerState>('/player/pause', { method: 'POST' }),
  stop: () => request<PlayerState>('/player/stop', { method: 'POST' }),
  next: () => request<PlayerState>('/player/next', { method: 'POST' }),
  previous: () => request<PlayerState>('/player/previous', { method: 'POST' }),
  seek: (positionMs: number) =>
    request<PlayerState>('/player/seek', {
      method: 'POST',
      body: JSON.stringify({ position_ms: positionMs }),
    }),
  setVolume: (volume: number) =>
    request<PlayerState>('/player/volume', {
      method: 'POST',
      body: JSON.stringify({ volume }),
    }),
  setPlayerMode: (mode: { shuffle?: boolean; repeat?: 'off' | 'one' | 'all' }) =>
    request<PlayerState>('/player/mode', {
      method: 'POST',
      body: JSON.stringify(mode),
    }),
  queue: () => request<QueueState>('/queue'),
  libraryAlbum: (albumId: number) => request<AlbumDetail>(`/library/albums/${albumId}`),
  libraryArtist: (artistId: number) => request<ArtistDetail>(`/library/artists/${artistId}`),
  addToQueue: (trackIds: number[]) =>
    request<QueueState>('/queue', {
      method: 'POST',
      body: JSON.stringify({ track_ids: trackIds }),
    }),
  removeFromQueue: (itemId: number) =>
    request<QueueState>(`/queue/${itemId}`, { method: 'DELETE' }),
  clearQueue: () => request<QueueState>('/queue', { method: 'DELETE' }),
  reorderQueue: (itemIds: number[]) =>
    request<QueueState>('/queue/reorder', {
      method: 'POST',
      body: JSON.stringify({ item_ids: itemIds }),
    }),
  library: (limit = 24) => request<LibraryPage>(`/library/tracks?limit=${limit}`),
  deleteTrack: (trackId: number, deleteFile = false) =>
    request<{ deleted: boolean; soft_deleted: boolean; files_deleted: string[] }>(
      `/library/tracks/${trackId}?delete_file=${deleteFile ? 'true' : 'false'}`,
      { method: 'DELETE' },
    ),
  restoreTrack: (trackId: number) =>
    request<Track>(`/library/tracks/${trackId}/restore`, { method: 'POST' }),
  updateTrack: (trackId: number, changes: Record<string, string | number | string[]>) =>
    request<{ track: Track; file_tags: string; warning: string | null }>(
      `/library/tracks/${trackId}`,
      { method: 'PATCH', body: JSON.stringify(changes) },
    ),
  history: (limit = 20, offset = 0) =>
    request<HistoryPage>(`/history?limit=${limit}&offset=${offset}`),
  clearHistory: () => request<{ deleted: number }>('/history', { method: 'DELETE' }),
  libraryTracks: (params: {
    limit?: number;
    offset?: number;
    search?: string;
    availability?: string;
  } = {}) => {
    const searchParams = new URLSearchParams();
    searchParams.set('limit', String(params.limit ?? 50));
    searchParams.set('offset', String(params.offset ?? 0));
    if (params.search) searchParams.set('search', params.search);
    if (params.availability && params.availability !== 'all') {
      searchParams.set('availability', params.availability);
    }
    return request<LibraryPage>(`/library/tracks?${searchParams.toString()}`);
  },
  albums: (params: { limit?: number; offset?: number; search?: string } = {}) => {
    const searchParams = new URLSearchParams();
    searchParams.set('limit', String(params.limit ?? 50));
    searchParams.set('offset', String(params.offset ?? 0));
    if (params.search) searchParams.set('search', params.search);
    return request<AlbumPage>(`/library/albums?${searchParams.toString()}`);
  },
  artists: (params: { limit?: number; offset?: number; search?: string } = {}) => {
    const searchParams = new URLSearchParams();
    searchParams.set('limit', String(params.limit ?? 50));
    searchParams.set('offset', String(params.offset ?? 0));
    if (params.search) searchParams.set('search', params.search);
    return request<ArtistPage>(`/library/artists?${searchParams.toString()}`);
  },
  search: (query: string, limit = 25) =>
    request<SearchResults>(
      `/library/search?q=${encodeURIComponent(query)}&limit=${limit}`,
    ),
  startImport: (paths?: string[]) =>
    request<ImportJob>('/library/import', {
      method: 'POST',
      body: JSON.stringify(paths && paths.length > 0 ? { paths } : {}),
    }),
  importStatus: () => request<{ job: ImportJob | null }>('/library/import/status'),
  cancelImport: () => request<ImportJob>('/library/import/cancel', { method: 'POST' }),
  authStatus: () => request<AuthStatus>('/auth/status'),
  systemStatus: () => request<SystemStatus>('/system/status'),
  systemMetrics: (minutes = 60) => request<SystemMetrics>(`/system/metrics?minutes=${minutes}`),
  systemRuntime: () => request<RuntimeInfo>('/system/runtime'),
  systemShare: () => request<SystemShare>('/system/share'),
  reboot: () =>
    request<{ status: string }>('/system/reboot', {
      method: 'POST',
      body: JSON.stringify({ confirm: true }),
    }),
  shutdown: () =>
    request<{ status: string }>('/system/shutdown', {
      method: 'POST',
      body: JSON.stringify({ confirm: true }),
    }),
  login: (pin: string) =>
    request<{ authenticated: boolean }>('/auth/pin', {
      method: 'POST',
      body: JSON.stringify({ pin }),
    }),
  logout: () => request<{ authenticated: boolean }>('/auth/logout', { method: 'POST' }),
};
