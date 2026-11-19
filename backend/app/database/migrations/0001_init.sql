-- MusicNX initial schema. All timestamps are UTC ISO-8601 TEXT values.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS artists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    sort_name TEXT,
    cover_path TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_artists_name ON artists (name);

CREATE TABLE IF NOT EXISTS artist_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist_id INTEGER NOT NULL REFERENCES artists (id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    external_id TEXT NOT NULL,
    UNIQUE (source_id, external_id),
    UNIQUE (artist_id, source_id)
);

CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    artist_id INTEGER REFERENCES artists (id) ON DELETE SET NULL,
    year INTEGER,
    cover_path TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_albums_artist ON albums (artist_id);
CREATE INDEX IF NOT EXISTS idx_albums_title ON albums (title);

CREATE TABLE IF NOT EXISTS album_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id INTEGER NOT NULL REFERENCES albums (id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    external_id TEXT NOT NULL,
    UNIQUE (source_id, external_id),
    UNIQUE (album_id, source_id)
);

CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    album_id INTEGER REFERENCES albums (id) ON DELETE SET NULL,
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    genre TEXT,
    year INTEGER,
    track_number INTEGER,
    disc_number INTEGER,
    has_local_file INTEGER NOT NULL DEFAULT 0 CHECK (has_local_file IN (0, 1)),
    has_online_source INTEGER NOT NULL DEFAULT 0 CHECK (has_online_source IN (0, 1)),
    file_missing INTEGER NOT NULL DEFAULT 0 CHECK (file_missing IN (0, 1)),
    source_unavailable INTEGER NOT NULL DEFAULT 0 CHECK (source_unavailable IN (0, 1)),
    last_verified_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_tracks_album ON tracks (album_id);
CREATE INDEX IF NOT EXISTS idx_tracks_title ON tracks (title);
CREATE INDEX IF NOT EXISTS idx_tracks_availability
    ON tracks (has_local_file, has_online_source);

CREATE TABLE IF NOT EXISTS track_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id INTEGER NOT NULL REFERENCES tracks (id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    external_id TEXT NOT NULL,
    availability TEXT NOT NULL DEFAULT 'unknown'
        CHECK (availability IN ('available', 'unavailable', 'unknown', 'region_restricted')),
    last_checked_at TEXT,
    UNIQUE (source_id, external_id),
    UNIQUE (track_id, source_id)
);
CREATE INDEX IF NOT EXISTS idx_track_sources_track ON track_sources (track_id);

CREATE TABLE IF NOT EXISTS track_artists (
    track_id INTEGER NOT NULL REFERENCES tracks (id) ON DELETE CASCADE,
    artist_id INTEGER NOT NULL REFERENCES artists (id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (track_id, artist_id)
);
CREATE INDEX IF NOT EXISTS idx_track_artists_artist ON track_artists (artist_id);

CREATE TABLE IF NOT EXISTS media_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id INTEGER NOT NULL REFERENCES tracks (id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'library'
        CHECK (role IN ('library', 'download', 'cache')),
    path TEXT NOT NULL UNIQUE,
    format TEXT,
    codec TEXT,
    bitrate INTEGER,
    sample_rate INTEGER,
    channels INTEGER,
    size_bytes INTEGER,
    duration_ms INTEGER,
    checksum TEXT,
    verified_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_media_files_track ON media_files (track_id);

CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    source_id INTEGER REFERENCES sources (id) ON DELETE SET NULL,
    external_id TEXT,
    revision TEXT,
    kind TEXT NOT NULL DEFAULT 'user'
        CHECK (kind IN ('user', 'likes', 'wave', 'local')),
    description TEXT,
    cover_path TEXT,
    track_count INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (source_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_playlists_kind ON playlists (kind);

CREATE TABLE IF NOT EXISTS playlist_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL REFERENCES playlists (id) ON DELETE CASCADE,
    track_id INTEGER NOT NULL REFERENCES tracks (id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    added_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    removed_at TEXT,
    UNIQUE (playlist_id, position)
);
CREATE INDEX IF NOT EXISTS idx_playlist_tracks_playlist ON playlist_tracks (playlist_id, position);
CREATE INDEX IF NOT EXISTS idx_playlist_tracks_track ON playlist_tracks (track_id);

CREATE TABLE IF NOT EXISTS lyrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id INTEGER NOT NULL REFERENCES tracks (id) ON DELETE CASCADE,
    language TEXT NOT NULL DEFAULT 'und',
    format TEXT NOT NULL CHECK (format IN ('plain', 'lrc')),
    content TEXT NOT NULL,
    source TEXT,
    offset_ms INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (track_id, language, format)
);
CREATE INDEX IF NOT EXISTS idx_lyrics_track ON lyrics (track_id);

CREATE TABLE IF NOT EXISTS queue_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id INTEGER NOT NULL REFERENCES tracks (id) ON DELETE CASCADE,
    position INTEGER NOT NULL UNIQUE CHECK (position >= 0),
    source_playlist_id INTEGER REFERENCES playlists (id) ON DELETE SET NULL,
    added_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_queue_items_track ON queue_items (track_id);
CREATE INDEX IF NOT EXISTS idx_queue_items_playlist ON queue_items (source_playlist_id);

CREATE TABLE IF NOT EXISTS playback_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    track_id INTEGER REFERENCES tracks (id) ON DELETE SET NULL,
    queue_item_id INTEGER REFERENCES queue_items (id) ON DELETE SET NULL,
    position_ms INTEGER NOT NULL DEFAULT 0 CHECK (position_ms >= 0),
    volume INTEGER NOT NULL DEFAULT 70 CHECK (volume BETWEEN 0 AND 100),
    status TEXT NOT NULL DEFAULT 'stopped'
        CHECK (status IN ('stopped', 'playing', 'paused', 'buffering', 'error')),
    zone_id TEXT,
    queue_index INTEGER NOT NULL DEFAULT 0 CHECK (queue_index >= 0),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS play_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id INTEGER NOT NULL REFERENCES tracks (id) ON DELETE CASCADE,
    played_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    zone_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_play_history_played ON play_history (played_at DESC);
CREATE INDEX IF NOT EXISTS idx_play_history_track ON play_history (track_id);

CREATE TABLE IF NOT EXISTS download_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id INTEGER NOT NULL UNIQUE REFERENCES tracks (id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'downloading', 'paused', 'completed', 'failed', 'cancelled')),
    priority INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    bytes_done INTEGER NOT NULL DEFAULT 0,
    bytes_total INTEGER,
    target_path TEXT,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_download_tasks_status ON download_tasks (status);
CREATE INDEX IF NOT EXISTS idx_download_tasks_priority ON download_tasks (priority DESC);

CREATE TABLE IF NOT EXISTS audio_zones (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('local_sink', 'browser', 'bt')),
    sink_name TEXT,
    volume INTEGER NOT NULL DEFAULT 100 CHECK (volume BETWEEN 0 AND 100),
    delay_offset_ms INTEGER NOT NULL DEFAULT 0,
    last_seen TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL
        CHECK (status IN ('running', 'success', 'partial', 'failed', 'cancelled')),
    playlists_added INTEGER NOT NULL DEFAULT 0,
    playlists_updated INTEGER NOT NULL DEFAULT 0,
    playlists_removed INTEGER NOT NULL DEFAULT 0,
    tracks_added INTEGER NOT NULL DEFAULT 0,
    tracks_updated INTEGER NOT NULL DEFAULT 0,
    tracks_removed INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_sync_runs_started ON sync_runs (started_at DESC);
