-- P2 search index: a regular FTS5 table (not contentless) so rows can be
-- updated/deleted when metadata changes. Rebuilt by the importer and at startup.
DROP TABLE IF EXISTS tracks_fts;
CREATE VIRTUAL TABLE tracks_fts USING fts5 (
    title,
    artists,
    album,
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE INDEX IF NOT EXISTS idx_media_files_checksum ON media_files (checksum);

-- Preserve the "label" tag for local files (SPEC P2 metadata list).
ALTER TABLE tracks ADD COLUMN label TEXT;
