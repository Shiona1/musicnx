#!/bin/sh
# MusicNX database backup.
#
# Creates a consistent SQLite snapshot with the sqlite3 CLI (`.backup`), checks
# it with PRAGMA integrity_check, forces mode 0600 and rotates old snapshots,
# keeping the newest MUSICNX_BACKUP_KEEP (default 14) files named
# musicnx-YYYYmmdd-HHMMSS.db. The script never copies config.yaml, the Yandex
# token file or WAL/SHM sidecars: the snapshot is self-contained, and only the
# data the application itself stores in SQLite is present in it.
#
# Safe to run while MusicNX is running: `.backup` uses the SQLite online backup
# API, it does not copy a live database file byte by byte (SPEC 11.7).
#
# Usage:
#   backup.sh [--db PATH] [--dir DIR] [--keep N] [--copy-to DIR]
#
# Environment (flags win over environment):
#   MUSICNX_DB              database file (default /var/lib/musicnx/database/musicnx.db)
#   MUSICNX_BACKUP_DIR      snapshot directory (default /var/lib/musicnx/backups)
#   MUSICNX_BACKUP_KEEP     snapshots to keep (default 14, must be >= 1)
#   MUSICNX_BACKUP_COPY_DIR optional second location (USB/NAS), no rotation there
#
# Requires: sqlite3 CLI or python3. Does not require root.

set -eu
LC_ALL=C
export LC_ALL

usage() {
    cat <<'EOF'
MusicNX backup: consistent SQLite snapshot + integrity check + rotation.

Usage: backup.sh [options]

Options:
  --db PATH        database file (default: MUSICNX_DB or
                   /var/lib/musicnx/database/musicnx.db)
  --dir DIR        snapshot directory (default: MUSICNX_BACKUP_DIR or
                   /var/lib/musicnx/backups)
  --keep N         snapshots to keep, 14 by default (minimum 1)
  --copy-to DIR    also copy the new snapshot to DIR (USB/NAS); DIR is never
                   rotated by this script
  -h, --help       show this help

Environment: MUSICNX_DB, MUSICNX_BACKUP_DIR, MUSICNX_BACKUP_KEEP,
MUSICNX_BACKUP_COPY_DIR. All paths must be absolute.

Exit codes: 0 success; 1 configuration, sqlite3 or integrity failure.
EOF
}

die() {
    printf 'backup: ERROR: %s\n' "$*" >&2
    exit 1
}

note() {
    printf 'backup: %s\n' "$*"
}

db=${MUSICNX_DB:-/var/lib/musicnx/database/musicnx.db}
backup_dir=${MUSICNX_BACKUP_DIR:-/var/lib/musicnx/backups}
keep=${MUSICNX_BACKUP_KEEP:-14}
copy_dir=${MUSICNX_BACKUP_COPY_DIR:-}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --db)
            [ "$#" -ge 2 ] || die "--db requires a value"
            db=$2
            shift 2
            ;;
        --dir)
            [ "$#" -ge 2 ] || die "--dir requires a value"
            backup_dir=$2
            shift 2
            ;;
        --keep)
            [ "$#" -ge 2 ] || die "--keep requires a value"
            keep=$2
            shift 2
            ;;
        --copy-to)
            [ "$#" -ge 2 ] || die "--copy-to requires a value"
            copy_dir=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown argument: $1 (see --help)"
            ;;
    esac
done

case "$keep" in
    ''|*[!0-9]*) die "--keep must be a positive integer, got: $keep" ;;
esac
[ "$keep" -ge 1 ] || die "--keep must be >= 1"

# Refuse ambiguous or unsafe database paths early. backup.sh only reads the DB.
case "$db" in
    /*) ;;
    *) die "database path must be absolute: $db" ;;
esac
case "$backup_dir" in
    /*) ;;
    *) die "backup directory must be absolute: $backup_dir" ;;
esac
if [ -n "$copy_dir" ]; then
    case "$copy_dir" in
        /*) ;;
        *) die "--copy-to must be absolute: $copy_dir" ;;
    esac
fi

# sqlite3 dot-command argument quoting cannot carry single quotes reliably.
case "$db$backup_dir$copy_dir" in
    *"'"*|*'"'*)
        die "paths must not contain single or double quotes"
        ;;
esac
case "$db$backup_dir$copy_dir" in
    *'
'*)
        die "paths must not contain newlines"
        ;;
esac

[ -e "$db" ] || die "database not found: $db (set MUSICNX_DB)"
[ -f "$db" ] || die "database is not a regular file: $db"
[ -r "$db" ] || die "database is not readable: $db"
if [ -L "$db" ]; then
    die "refusing symlinked database path: $db"
fi

if command -v sqlite3 >/dev/null 2>&1; then
    have_sqlite3=1
else
    have_sqlite3=0
    command -v python3 >/dev/null 2>&1 || die "sqlite3 CLI or python3 is required"
    note "sqlite3 CLI not found: using the Python sqlite3 fallback"
fi

sqlite_backup() {
    # $1 source, $2 destination; online backup API (never a byte copy)
    if [ "$have_sqlite3" = 1 ]; then
        sqlite3 "$1" ".backup '$2'"
    else
        python3 - "$1" "$2" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close(); src.close()
PY
    fi
}

sqlite_integrity() {
    # $1 snapshot; prints integrity_check output
    if [ "$have_sqlite3" = 1 ]; then
        sqlite3 "$1" 'PRAGMA integrity_check;'
    else
        python3 - "$1" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
print(conn.execute("PRAGMA integrity_check").fetchone()[0])
conn.close()
PY
    fi
}

umask 077
mkdir -p -- "$backup_dir" || die "cannot create backup directory: $backup_dir"
[ -d "$backup_dir" ] || die "backup path is not a directory: $backup_dir"
[ -w "$backup_dir" ] || die "backup directory is not writable: $backup_dir"

note "database: $db"
note "directory: $backup_dir (keep $keep)"

ts=$(date +%Y%m%d-%H%M%S)
dest="$backup_dir/musicnx-$ts.db"
suffix=0
while [ -e "$dest" ]; do
    suffix=$((suffix + 1))
    dest="$backup_dir/musicnx-$ts-$suffix.db"
done

partial=""
cleanup() {
    if [ -n "$partial" ] && [ -f "$partial" ]; then
        rm -f -- "$partial" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM HUP

partial="$backup_dir/.musicnx-partial-$$.db"
note "creating snapshot: $dest"

if ! sqlite_backup "$db" "$partial"; then
    die "sqlite backup failed for $db"
fi
[ -s "$partial" ] || die "snapshot is empty; database may be uninitialized: $db"
chmod 600 -- "$partial" 2>/dev/null || die "cannot chmod 600 the snapshot"

check=$(sqlite_integrity "$partial" 2>&1) ||
    die "integrity_check could not run on the snapshot"
case "$check" in
    ok) ;;
    *)
        printf 'backup: integrity_check output:\n%s\n' "$check" >&2
        die "integrity_check failed; snapshot discarded"
        ;;
esac

mv -- "$partial" "$dest" || die "cannot move snapshot into place: $dest"
partial=""
bytes=$(wc -c < "$dest" | tr -d '[:space:]')

if [ -n "$copy_dir" ]; then
    mkdir -p -- "$copy_dir" || die "cannot create copy directory: $copy_dir"
    [ -d "$copy_dir" ] || die "copy target is not a directory: $copy_dir"
    cp -p -- "$dest" "$copy_dir/" || die "cannot copy snapshot to $copy_dir"
    copy_path="$copy_dir/$(basename "$dest")"
    copy_bytes=$(wc -c < "$copy_path" | tr -d '[:space:]')
    [ "$bytes" = "$copy_bytes" ] ||
        die "copied snapshot size mismatch: $copy_path"
    note "copied to: $copy_path"
fi

# Rotation: only files matching musicnx-*.db, oldest names first. Manual copies
# should use a different prefix if they must never be rotated away.
count=0
for f in "$backup_dir"/musicnx-*.db; do
    [ -f "$f" ] || continue
    count=$((count + 1))
done
if [ "$count" -gt "$keep" ]; then
    remove=$((count - keep))
    note "rotation: $count snapshots, removing $remove oldest (keep $keep)"
    for f in "$backup_dir"/musicnx-*.db; do
        [ "$remove" -le 0 ] && break
        [ -f "$f" ] || continue
        [ "$f" = "$dest" ] && continue
        if rm -f -- "$f"; then
            note "removed: $f"
            remove=$((remove - 1))
        else
            note "WARNING: cannot remove $f"
        fi
    done
fi

note "done: $dest ($bytes bytes, integrity_check=ok)"
