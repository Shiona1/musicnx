-- Full-text search index for tracks (contentless). Populated by P2 import/sync.
CREATE VIRTUAL TABLE IF NOT EXISTS tracks_fts USING fts5 (
    title,
    artists,
    album,
    tokenize = 'unicode61 remove_diacritics 2',
    content = ''
);
