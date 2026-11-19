-- P1 library tombstone: hide deleted tracks without destroying files/history.
ALTER TABLE tracks ADD COLUMN deleted_at TEXT;
CREATE INDEX IF NOT EXISTS idx_tracks_deleted_at ON tracks (deleted_at);
