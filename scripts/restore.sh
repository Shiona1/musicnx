#!/bin/sh
# MusicNX database restore.
#
# Restores a snapshot created by backup.sh (or by the Backup API, same SQLite
# format). The script refuses unsafe paths, verifies the snapshot with
# PRAGMA quick_check, stops the systemd user service, keeps a timestamped
# safety copy of the current database (including WAL sidecars when readable),
# atomically replaces the database, starts the service and waits for health.
#
# There is no live restore API: replacing a running database is a stopped-service
# operation by design.
#
# Usage:
#   restore.sh [--yes] [--db PATH] [--backup-dir DIR] [--service NAME]
#              [--no-service] [--health-url URL] [--timeout SECONDS] BACKUP.db
#
# Environment:
#   MUSICNX_DB           database file (default /var/lib/musicnx/database/musicnx.db)
#   MUSICNX_BACKUP_DIR   safety-copy directory (default /var/lib/musicnx/backups)
#   MUSICNX_SERVICE      systemd user unit (default musicnx; empty disables it)
#   MUSICNX_HEALTH_URL   health endpoint (default http://127.0.0.1:8080/api/v1/system/health)
#   MUSICNX_HEALTH_TIMEOUT  health wait in seconds (default 30)
#   MUSICNX_ALLOW_ROOT   set to 1 only for a system-level install (not supported)
#
# Requires: sqlite3 CLI or python3, systemctl --user (unless --no-service), curl (optional
# for the health check). Do not run it as root for a user service.

set -eu
LC_ALL=C
export LC_ALL

usage() {
    cat <<'EOF'
MusicNX restore: replace the database from a verified snapshot.

Usage: restore.sh [options] BACKUP.db

Options:
  --yes                 do not ask for confirmation (non-interactive use)
  --db PATH             database to replace (absolute path, *.db)
  --backup-dir DIR      where the pre-restore safety copy is written
  --service NAME        systemd user unit (default: musicnx)
  --no-service          do not stop/start systemd (database must be stopped)
  --health-url URL      health endpoint checked after start
  --timeout SECONDS     health wait, default 30
  -h, --help            show this help

The backup file must be absolute, a regular file (not a symlink), end in .db
and pass PRAGMA quick_check. The current database gets a safety copy named
pre-restore-YYYYmmdd-HHMMSS.db in the backup directory before it is replaced.

Exit codes: 0 success; 1 validation, service, sqlite3 or health failure.
EOF
}

die() {
    printf 'restore: ERROR: %s\n' "$*" >&2
    exit 1
}

note() {
    printf 'restore: %s\n' "$*"
}

db=${MUSICNX_DB:-/var/lib/musicnx/database/musicnx.db}
backup_dir=${MUSICNX_BACKUP_DIR:-/var/lib/musicnx/backups}
service=${MUSICNX_SERVICE-musicnx}
health_url=${MUSICNX_HEALTH_URL:-http://127.0.0.1:8080/api/v1/system/health}
health_timeout=${MUSICNX_HEALTH_TIMEOUT:-30}
assume_yes=0
backup_file=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        --yes)
            assume_yes=1
            shift
            ;;
        --db)
            [ "$#" -ge 2 ] || die "--db requires a value"
            db=$2
            shift 2
            ;;
        --backup-dir)
            [ "$#" -ge 2 ] || die "--backup-dir requires a value"
            backup_dir=$2
            shift 2
            ;;
        --service)
            [ "$#" -ge 2 ] || die "--service requires a value"
            service=$2
            shift 2
            ;;
        --no-service)
            service=""
            shift
            ;;
        --health-url)
            [ "$#" -ge 2 ] || die "--health-url requires a value"
            health_url=$2
            shift 2
            ;;
        --timeout)
            [ "$#" -ge 2 ] || die "--timeout requires a value"
            health_timeout=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        -*)
            die "unknown option: $1 (see --help)"
            ;;
        *)
            [ -z "$backup_file" ] || die "only one backup file may be given"
            backup_file=$1
            shift
            ;;
    esac
done

[ -n "$backup_file" ] || die "backup file is required (see --help)"
case "$health_timeout" in
    ''|*[!0-9]*) die "--timeout must be a positive integer" ;;
esac
[ "$health_timeout" -ge 1 ] || die "--timeout must be >= 1"

# ---------------------------------------------------------------------------
# Path safety. Only absolute *.db paths are accepted; symlinks are refused so a
# restore cannot be redirected outside the intended location.
# ---------------------------------------------------------------------------
case "$backup_file" in
    /*) ;;
    *) die "backup file must be an absolute path: $backup_file" ;;
esac
case "$backup_file" in
    *.db) ;;
    *) die "backup file must end with .db: $backup_file" ;;
esac
[ -e "$backup_file" ] || die "backup not found: $backup_file"
[ -f "$backup_file" ] || die "backup is not a regular file: $backup_file"
if [ -L "$backup_file" ]; then
    die "refusing symlinked backup file: $backup_file"
fi
[ -r "$backup_file" ] || die "backup is not readable: $backup_file"

case "$db" in
    /*) ;;
    *) die "database path must be absolute: $db" ;;
esac
case "$db" in
    *.db) ;;
    *) die "database path must end with .db: $db" ;;
esac
case "$db" in
    /dev/*|/proc/*|/sys/*) die "refusing database path: $db" ;;
esac
if [ -L "$db" ]; then
    die "refusing symlinked database path: $db"
fi
if [ -d "$db" ]; then
    die "database path is a directory: $db"
fi
db_dir=$(dirname -- "$db")
db_base=$(basename -- "$db")
[ -d "$db_dir" ] || die "database directory does not exist: $db_dir"
[ -w "$db_dir" ] || die "database directory is not writable: $db_dir"

case "$backup_dir" in
    /*) ;;
    *) die "backup directory must be absolute: $backup_dir" ;;
esac

case "$db$backup_file$backup_dir" in
    *"'"*|*'"'*|*'
'*)
        die "paths must not contain quotes or newlines"
        ;;
esac
case "$backup_dir" in
    /dev/*|/proc/*|/sys/*) die "refusing backup directory: $backup_dir" ;;
esac

if [ "$(id -u)" = "0" ] && [ "${MUSICNX_ALLOW_ROOT:-0}" != "1" ]; then
    die "run as the MusicNX user, not root (user service + file ownership)"
fi

if command -v sqlite3 >/dev/null 2>&1; then
    have_sqlite3=1
else
    have_sqlite3=0
    command -v python3 >/dev/null 2>&1 || die "sqlite3 CLI or python3 is required"
    note "sqlite3 CLI not found: using the Python sqlite3 fallback"
fi

sqlite_quick_check() {
    # $1 database; prints quick_check output
    if [ "$have_sqlite3" = 1 ]; then
        sqlite3 "$1" 'PRAGMA quick_check;'
    else
        python3 - "$1" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
print(conn.execute("PRAGMA quick_check").fetchone()[0])
conn.close()
PY
    fi
}

sqlite_backup_file() {
    # $1 source, $2 destination
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

backup_real="$(cd "$(dirname -- "$backup_file")" && pwd -P)/$(basename -- "$backup_file")"
db_real="$(cd "$db_dir" && pwd -P)/$db_base"
if [ "$backup_real" = "$db_real" ]; then
    die "backup file and database are the same file: $db_real"
fi

if [ -f "$db" ] && command -v stat >/dev/null 2>&1; then
    db_owner=$(stat -c '%u' "$db" 2>/dev/null || true)
    if [ -n "$db_owner" ] && [ "$db_owner" != "$(id -u)" ]; then
        die "database is owned by uid $db_owner, current user is $(id -u); run as the MusicNX user"
    fi
fi

umask 077
mkdir -p -- "$backup_dir" || die "cannot create backup directory: $backup_dir"
[ -d "$backup_dir" ] || die "backup path is not a directory: $backup_dir"

# ---------------------------------------------------------------------------
# Verify the candidate snapshot before touching anything.
# ---------------------------------------------------------------------------
size=$(wc -c < "$backup_file" | tr -d '[:space:]')
[ "$size" -ge 512 ] || die "backup file is too small to be a SQLite database: $backup_file"
check=$(sqlite_quick_check "$backup_file" 2>&1) ||
    die "cannot open backup as SQLite database: $backup_file"
case "$check" in
    ok) ;;
    *)
        printf 'restore: quick_check output:\n%s\n' "$check" >&2
        die "quick_check failed for $backup_file; nothing was changed"
        ;;
esac
note "snapshot verified: $backup_file ($size bytes, quick_check=ok)"

# ---------------------------------------------------------------------------
# Service control (systemd user unit).
# ---------------------------------------------------------------------------
managed=0
if [ -n "$service" ]; then
    command -v systemctl >/dev/null 2>&1 ||
        die "systemctl not found; pass --no-service if MusicNX is not systemd-managed"
    if ! systemctl --user show-environment >/dev/null 2>&1; then
        die "cannot reach the systemd user bus; run as the MusicNX user or pass --no-service"
    fi
    unit_state=$(systemctl --user show "$service" -p LoadState --value 2>/dev/null || true)
    case "$unit_state" in
        ""|not-found|masked|error)
            die "systemd user unit is not loaded: $service (pass --no-service for a stopped database)"
            ;;
    esac
    managed=1
fi

if [ "$assume_yes" -ne 1 ]; then
    if [ ! -t 0 ]; then
        die "refusing a non-interactive restore without --yes"
    fi
    printf 'Replace %s with %s? A safety copy of the current database is made first. [y/N] ' \
        "$db" "$backup_file"
    answer=""
    read -r answer || answer=""
    case "$answer" in
        y|Y|yes|YES) ;;
        *) die "aborted by the operator" ;;
    esac
fi

service_stopped=0
partial=""
on_exit() {
    status=$?
    if [ -n "$partial" ] && [ -f "$partial" ]; then
        rm -f -- "$partial" 2>/dev/null || true
    fi
    if [ "$status" -ne 0 ] && [ "$service_stopped" -eq 1 ] && [ "$managed" -eq 1 ]; then
        printf 'restore: failure; restarting %s\n' "$service" >&2
        systemctl --user start "$service" >/dev/null 2>&1 ||
            printf 'restore: WARNING: could not restart %s; run: systemctl --user start %s\n' \
                "$service" "$service" >&2
    fi
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

if [ "$managed" -eq 1 ]; then
    if systemctl --user is-active --quiet "$service"; then
        note "stopping $service"
        systemctl --user stop "$service" || die "cannot stop $service; database untouched"
        service_stopped=1
    else
        note "$service is not active; replacing a stopped database"
    fi
fi

# ---------------------------------------------------------------------------
# Safety copy of the current database. `.backup` includes committed WAL data.
# If the current database is damaged, keep a raw copy instead of aborting: the
# restore is exactly the case where the old file may be unreadable.
# ---------------------------------------------------------------------------
ts=$(date +%Y%m%d-%H%M%S)
if [ -f "$db" ]; then
    safety="$backup_dir/pre-restore-$ts.db"
    suffix=0
    while [ -e "$safety" ]; do
        suffix=$((suffix + 1))
        safety="$backup_dir/pre-restore-$ts-$suffix.db"
    done
    safety_mode="backup"
    if sqlite_backup_file "$db" "$safety" 2>/dev/null; then
        chmod 600 -- "$safety" 2>/dev/null || true
        safety_check=$(sqlite_quick_check "$safety" 2>&1 || true)
        if [ "$safety_check" != "ok" ]; then
            note "WARNING: safety copy did not pass quick_check: $safety"
        fi
    else
        note "WARNING: sqlite3 .backup failed; keeping a raw copy of the current file"
        safety_mode="raw"
        rm -f -- "$safety" 2>/dev/null || true
        cp -p -- "$db" "$safety" || die "cannot create safety copy; database untouched"
    fi
    note "safety copy: $safety"

    # A raw copy does not include WAL data, so keep the sidecars next to it.
    # A successful .backup already contains committed WAL contents.
    if [ "$safety_mode" = "raw" ] && [ -f "$db-wal" ]; then
        cp -p -- "$db-wal" "$safety-wal" 2>/dev/null ||
            note "WARNING: cannot preserve WAL sidecar next to the safety copy"
    fi
    if [ "$safety_mode" = "raw" ] && [ -f "$db-shm" ]; then
        cp -p -- "$db-shm" "$safety-shm" 2>/dev/null ||
            note "WARNING: cannot preserve SHM sidecar next to the safety copy"
    fi
fi

# A restored database must never be combined with stale WAL/SHM sidecars.
rm -f -- "$db-wal" "$db-shm" || die "cannot remove WAL sidecars for $db"

# ---------------------------------------------------------------------------
# Atomic replacement inside the database directory.
# ---------------------------------------------------------------------------
partial="$db_dir/.$db_base.restore-$$.tmp"
cp -- "$backup_file" "$partial" || die "cannot copy backup into $db_dir"
chmod 600 -- "$partial" 2>/dev/null || die "cannot chmod 600 the restored database"
restored_check=$(sqlite_quick_check "$partial" 2>&1) ||
    die "restored copy cannot be opened; original database kept"
case "$restored_check" in
    ok) ;;
    *)
        printf 'restore: quick_check output:\n%s\n' "$restored_check" >&2
        die "restored copy failed quick_check; original database kept"
        ;;
esac
mv -- "$partial" "$db" || die "cannot move the restored database into place"
partial=""
note "database replaced: $db"

final_check=$(sqlite_quick_check "$db" 2>&1) || final_check="error"
case "$final_check" in
    ok) note "installed database quick_check=ok" ;;
    *) note "WARNING: installed database quick_check output: $final_check" ;;
esac

# ---------------------------------------------------------------------------
# Start and health check.
# ---------------------------------------------------------------------------
wait_health() {
    if [ -z "$health_url" ]; then
        note "no health URL configured; skipped health check"
        return 0
    fi
    if ! command -v curl >/dev/null 2>&1; then
        note "WARNING: curl not found; skipped health check"
        return 0
    fi
    elapsed=0
    while [ "$elapsed" -lt "$health_timeout" ]; do
        code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 "$health_url" 2>/dev/null || true)
        if [ "$code" = "200" ]; then
            note "health OK: $health_url"
            return 0
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done
    return 1
}

if [ "$managed" -eq 1 ]; then
    systemctl --user start "$service" ||
        die "database restored, but $service failed to start"
    service_stopped=0
    note "started $service"
    if ! wait_health; then
        die "database restored, but health check failed; see: journalctl --user -u $service -n 100"
    fi
else
    note "systemd service not managed (--no-service); start MusicNX manually"
fi

note "restore complete"
