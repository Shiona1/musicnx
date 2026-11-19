export type PlayerStatus = 'stopped' | 'playing' | 'paused' | 'buffering' | 'error';

export type Availability = 'offline' | 'online' | 'metadata_only' | 'missing' | 'unavailable';

export type ConnectionState = 'connecting' | 'online' | 'reconnecting' | 'offline';

/** Visual tone of the global toast: neutral feedback vs. failure. */
export type ToastTone = 'info' | 'error';

export interface ArtistRef {
  id: number;
  name: string;
}

export interface AlbumRef {
  id: number;
  title: string;
  year: number | null;
  cover_path: string | null;
}

export interface Track {
  id: number;
  title: string;
  artists: ArtistRef[];
  album: AlbumRef | null;
  duration_ms: number | null;
  genre?: string | null;
  label?: string | null;
  availability: Availability;
  has_local_file: boolean;
  has_online_source: boolean;
  file_missing: boolean;
  source_unavailable: boolean;
  cover_url: string | null;
  source?: 'yandex' | 'local' | null;
  external_id?: string | null;
  preview?: boolean;
  liked?: boolean | null;
}

export interface AlbumSummary {
  id: number;
  title: string;
  year: number | null;
  artist: ArtistRef | null;
  cover_url: string | null;
  track_count: number;
}

export interface ArtistSummary {
  id: number;
  name: string;
  cover_url: string | null;
  track_count: number;
  album_count: number;
}

export interface AlbumPage {
  items: AlbumSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface ArtistPage {
  items: ArtistSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface AlbumDetail {
  album: AlbumSummary;
  tracks: Track[];
}

export interface ArtistDetail {
  artist: ArtistSummary;
  tracks: Track[];
}

export interface SearchResults {
  tracks: LibraryPage;
  albums: AlbumPage;
  artists: ArtistPage;
}

export interface ImportJob {
  id: string;
  status:
    | 'queued'
    | 'running'
    | 'verifying'
    | 'cancelling'
    | 'completed'
    | 'failed'
    | 'cancelled';
  phase: string;
  total: number;
  processed: number;
  imported: number;
  updated: number;
  skipped: number;
  missing: number;
  verify_total: number;
  verify_processed: number;
  percent: number;
  errors: string[];
  roots: string[];
  target_paths: string[];
  started_at: string | null;
  finished_at: string | null;
  cancel_requested: boolean;
}

export interface AuthStatus {
  pin_configured: boolean;
  authenticated: boolean;
  localhost: boolean;
}

export type RepeatMode = 'off' | 'one' | 'all';

export interface PlayerState {
  status: PlayerStatus;
  track: Track | null;
  position_ms: number;
  duration_ms: number;
  volume: number;
  shuffle: boolean;
  repeat: RepeatMode;
  zone_id: string;
  queue_index: number | null;
  queue_item_id: number | null;
  can_play: boolean;
  can_pause: boolean;
  can_next: boolean;
  can_previous: boolean;
  error_code?: string | null;
  error_detail?: string | null;
  updated_at: string;
}

export interface QueueItem {
  id: number;
  position: number;
  source_playlist_id: number | null;
  track: Track;
}

export interface QueueState {
  items: QueueItem[];
  current_item_id: number | null;
  current_index: number | null;
  total: number;
  total_duration_ms: number;
}

export interface ServerInfo {
  version: string;
  mode: string;
  started_at: string;
  uptime_s: number;
}

export interface SystemStatus {
  mode: string;
  audio?: { browser_zone?: boolean; [key: string]: unknown };
  adapters?: { yandex?: { authenticated?: boolean; mode?: string } };
  [key: string]: unknown;
}

export interface SnapshotPayload {
  server: ServerInfo;
  player: PlayerState;
  queue: QueueState;
  library: { shelf: Track[]; total: number };
}

export interface LibraryPage {
  items: Track[];
  total: number;
  limit: number;
  offset: number;
}

export interface HistoryEntry {
  id: number;
  track: Track;
  played_at: string;
  zone_id: string | null;
}

export interface HistoryPage {
  items: HistoryEntry[];
  total: number;
  limit: number;
  offset: number;
}

export interface SystemShare {
  /** Web UI address usable from other devices on the LAN. */
  url: string;
  /** PNG data-URI QR of the URL; null means the UI shows the URL only. */
  qr_data_uri: string | null;
  /** True when even this address is loopback-only (a phone cannot open it). */
  loopback: boolean;
}

export interface RuntimeInfo {
  uptime_s: number;
  ws_clients: number;
  queue_depth: number;
  downloads: { total: number; by_status: Record<string, number> };
  database: { size_bytes: number };
  audio: { ready: boolean | null; restarts: number | null; error: string | null };
  security: { auth: string };
}

export interface PositionPayload {
  position_ms: number;
  duration_ms: number;
  status: PlayerStatus;
  server_time_ms: number;
}

export interface SystemSample {
  ts: string;
  uptime_s: number | null;
  cpu_pct: number | null;
  load1: number | null;
  mem_total_kb: number | null;
  mem_used_kb: number | null;
  mem_pct: number | null;
  temp_c: number | null;
  clock_mhz: number | null;
  throttled: string | null;
  disk_free_bytes: number | null;
}

export interface SystemMetrics {
  version: number;
  interval_s: number;
  minutes: number;
  samples: SystemSample[];
}

export interface WsEvent {
  type: string;
  ts: string;
  payload: Record<string, unknown>;
}

export interface WsSnapshot {
  type: 'snapshot';
  ts: string;
  payload: SnapshotPayload;
}
