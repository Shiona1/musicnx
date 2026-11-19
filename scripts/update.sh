#!/bin/sh
# MusicNX update for the Raspberry Pi (prebuilt static, no Node on the board).
#
# Order: validate -> database backup (backup.sh) -> code snapshot -> stop the
# systemd user service -> update the app tree (git ff-only or a prebuilt release
# directory) -> pip install from requirements.txt -> apply migrations -> start
# -> health check. Any failure after the snapshot restores the previous app tree
# and restarts the service; the pre-update database backup stays on disk.
#
# A prebuilt `app/static` is required: the Pi never builds the frontend and has
# no Node.js. Migrations are forward-only; rolling the code back cannot roll the
# schema back, so restore the database with restore.sh if a downgrade is needed.
#
# Usage:
#   update.sh [--source DIR] [--branch NAME] [--skip-deps] [--no-service]
#
# Environment:
#   MUSICNX_APP_DIR       app tree ($MUSICNX_APP_DIR/app/main.py), default
#                         /opt/musicnx/app (WorkingDirectory of the unit)
#   MUSICNX_VENV          virtualenv, default /opt/musicnx/venv
#   MUSICNX_PYTHON        python override (default $MUSICNX_VENV/bin/python)
#   MUSICNX_PIP           pip override (default $MUSICNX_VENV/bin/pip)
#   MUSICNX_CONFIG        config file for migrations, default
#                         /opt/musicnx/config/config.yaml
#   MUSICNX_DB            database file, default /var/lib/musicnx/database/musicnx.db
#   MUSICNX_BACKUP_DIR    database backups, default /var/lib/musicnx/backups
#   MUSICNX_ROLLBACK_DIR  code snapshots, default <parent of APP_DIR>/rollbacks
#   MUSICNX_SERVICE       systemd user unit, default musicnx (empty disables)
#   MUSICNX_HEALTH_URL    default http://127.0.0.1:8080/api/v1/system/health
#   MUSICNX_HEALTH_TIMEOUT  seconds to wait for health, default 30
#
# With --source the script copies a release tree from a dev machine
# (rsync if available, otherwise cp -a). Without --source APP_DIR must be a git
# checkout and is updated with `git pull --ff-only`.
#
# Requires: sh, sqlite3, git (git mode) and tar. Node.js is not used.

set -eu
LC_ALL=C
export LC_ALL

usage() {
    cat <<'EOF'
MusicNX update: backup, prebuilt app update, migrations, restart, health.

Usage: update.sh [options]

Options:
  --source DIR   copy DIR as the new app tree (release built on a dev machine)
  --branch NAME  git branch to update to (git mode only, default current)
  --skip-deps    skip pip install -r requirements.txt
  --no-service   do not stop/start systemd (service started manually)
  -h, --help     show this help

Environment (paths, see the script header): MUSICNX_APP_DIR, MUSICNX_VENV,
MUSICNX_PYTHON, MUSICNX_PIP, MUSICNX_CONFIG, MUSICNX_DB, MUSICNX_BACKUP_DIR,
MUSICNX_ROLLBACK_DIR, MUSICNX_SERVICE, MUSICNX_HEALTH_URL,
MUSICNX_HEALTH_TIMEOUT.

Exit codes: 0 success; 1 validation, backup, update, migration or health
failure after which the previous tree was restored (when possible).
EOF
}

die() {
    printf 'update: ERROR: %s\n' "$*" >&2
    exit 1
}

note() {
    printf 'update: %s\n' "$*"
}

app_dir=${MUSICNX_APP_DIR:-/opt/musicnx/app}
venv=${MUSICNX_VENV:-/opt/musicnx/venv}
python=${MUSICNX_PYTHON:-$venv/bin/python}
pip=${MUSICNX_PIP:-$venv/bin/pip}
config=${MUSICNX_CONFIG:-/opt/musicnx/config/config.yaml}
db=${MUSICNX_DB:-/var/lib/musicnx/database/musicnx.db}
backup_dir=${MUSICNX_BACKUP_DIR:-/var/lib/musicnx/backups}
service=${MUSICNX_SERVICE-musicnx}
health_url=${MUSICNX_HEALTH_URL:-http://127.0.0.1:8080/api/v1/system/health}
health_timeout=${MUSICNX_HEALTH_TIMEOUT:-30}
rollback_dir=${MUSICNX_ROLLBACK_DIR:-}
source_dir=""
branch=""
skip_deps=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --source)
            [ "$#" -ge 2 ] || die "--source requires a value"
            source_dir=$2
            shift 2
            ;;
        --branch)
            [ "$#" -ge 2 ] || die "--branch requires a value"
            branch=$2
            shift 2
            ;;
        --skip-deps)
            skip_deps=1
            shift
            ;;
        --no-service)
            service=""
            shift
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

case "$health_timeout" in
    ''|*[!0-9]*) die "MUSICNX_HEALTH_TIMEOUT must be a positive integer" ;;
esac
[ "$health_timeout" -ge 1 ] || die "MUSICNX_HEALTH_TIMEOUT must be >= 1"

# ---------------------------------------------------------------------------
# Validation before any change.
# ---------------------------------------------------------------------------
if [ "$(id -u)" = "0" ] && [ "${MUSICNX_ALLOW_ROOT:-0}" != "1" ]; then
    die "run as the MusicNX user, not root (user service + file ownership)"
fi

case "$app_dir" in
    /*) ;;
    *) die "MUSICNX_APP_DIR must be absolute: $app_dir" ;;
esac
[ -d "$app_dir" ] || die "app directory not found: $app_dir"
[ -f "$app_dir/app/main.py" ] || die "not a MusicNX app tree (app/main.py missing): $app_dir"
[ -f "$app_dir/requirements.txt" ] || die "requirements.txt missing in $app_dir"

case "$config" in
    /*) ;;
    *) die "MUSICNX_CONFIG must be absolute: $config" ;;
esac
[ -f "$config" ] || die "config file not found: $config"

case "$db" in
    /*) ;;
    *) die "MUSICNX_DB must be absolute: $db" ;;
esac
[ -f "$db" ] || die "database not found: $db (update an installed instance)"

case "$backup_dir" in
    /*) ;;
    *) die "MUSICNX_BACKUP_DIR must be absolute: $backup_dir" ;;
esac

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
[ -f "$script_dir/backup.sh" ] ||
    die "backup.sh not found next to update.sh ($script_dir); update aborted"

if [ -z "$rollback_dir" ]; then
    app_parent=$(dirname -- "$app_dir")
    rollback_dir="$app_parent/rollbacks"
fi
case "$rollback_dir" in
    /*) ;;
    *) die "MUSICNX_ROLLBACK_DIR must be absolute: $rollback_dir" ;;
esac

if [ "$skip_deps" -eq 0 ]; then
    [ -x "$pip" ] || die "pip not found: $pip (set MUSICNX_PIP or use --skip-deps)"
fi
[ -x "$python" ] || die "python not found: $python (set MUSICNX_PYTHON)"

command -v sqlite3 >/dev/null 2>&1 || die "sqlite3 CLI is required"
command -v tar >/dev/null 2>&1 || die "tar is required"

if [ -n "$source_dir" ]; then
    case "$source_dir" in
        /*) ;;
        *) die "--source must be an absolute path: $source_dir" ;;
    esac
    [ -d "$source_dir" ] || die "source directory not found: $source_dir"
    if [ -d "$app_dir/.git" ]; then
        die "--source cannot be combined with a git checkout in $app_dir"
    fi
    [ -f "$source_dir/app/main.py" ] || die "source has no app/main.py: $source_dir"
    [ -f "$source_dir/requirements.txt" ] ||
        die "source has no requirements.txt: $source_dir"
    [ -f "$source_dir/app/static/index.html" ] ||
        die "source has no prebuilt app/static/index.html; build the frontend on the dev machine"
    for forbidden in .venv venv var .git; do
        if [ -e "$source_dir/$forbidden" ]; then
            die "source must be a clean release tree (found $forbidden): $source_dir"
        fi
    done
else
    [ -d "$app_dir/.git" ] ||
        die "neither a git checkout nor --source given for $app_dir"
    dirty=$(git -C "$app_dir" status --porcelain --untracked-files=no 2>/dev/null || true)
    [ -z "$dirty" ] ||
        die "working tree has local changes; commit/stash them or use --source"
fi

managed=0
if [ -n "$service" ]; then
    command -v systemctl >/dev/null 2>&1 ||
        die "systemctl not found; pass --no-service if MusicNX is not systemd-managed"
    systemctl --user show-environment >/dev/null 2>&1 ||
        die "cannot reach the systemd user bus; run as the MusicNX user or pass --no-service"
    unit_state=$(systemctl --user show "$service" -p LoadState --value 2>/dev/null || true)
    case "$unit_state" in
        ""|not-found|masked|error)
            die "systemd user unit is not loaded: $service (pass --no-service or install the unit)"
            ;;
    esac
    managed=1
fi

note "app: $app_dir"
note "database: $db"

# ---------------------------------------------------------------------------
# 1. Database backup before any write.
# ---------------------------------------------------------------------------
note "backing up the database before the update"
if ! MUSICNX_DB="$db" MUSICNX_BACKUP_DIR="$backup_dir" sh "$script_dir/backup.sh"; then
    die "database backup failed; update aborted"
fi

# ---------------------------------------------------------------------------
# 2. Code snapshot for rollback.
# ---------------------------------------------------------------------------
timestamp=$(date +%Y%m%d-%H%M%S)
mkdir -p -- "$rollback_dir" || die "cannot create rollback directory: $rollback_dir"
snapshot="$rollback_dir/app-$timestamp.tar.gz"
suffix=0
while [ -e "$snapshot" ]; do
    suffix=$((suffix + 1))
    snapshot="$rollback_dir/app-$timestamp-$suffix.tar.gz"
done
note "code snapshot: $snapshot"
tar -C "$app_dir" -czf "$snapshot" \
    --exclude=.git --exclude=.venv --exclude=venv --exclude=var \
    --exclude=__pycache__ --exclude=node_modules . ||
    die "cannot snapshot $app_dir; update aborted"
[ -s "$snapshot" ] || die "snapshot is empty; update aborted"

update_applied=0
service_stopped=0
deps_installed=0

rollback_code() {
    [ -f "$snapshot" ] || {
        note "WARNING: rollback snapshot missing: $snapshot"
        return 0
    }
    note "restoring code from $snapshot"
    for entry in "$app_dir"/*; do
        [ -e "$entry" ] || continue
        rm -rf -- "$entry" 2>/dev/null || true
    done
    tar -C "$app_dir" -xzf "$snapshot" 2>/dev/null ||
        note "WARNING: rollback extraction failed; inspect $snapshot manually"
}

on_exit() {
    status=$?
    if [ "$status" -ne 0 ] && [ "$update_applied" -eq 1 ]; then
        note "update failed; rolling back" >&2
        if [ "$managed" -eq 1 ]; then
            systemctl --user stop "$service" >/dev/null 2>&1 || true
        fi
        rollback_code
        if [ "$deps_installed" -eq 1 ] && [ -x "$pip" ]; then
            "$pip" install -r "$app_dir/requirements.txt" >/dev/null 2>&1 ||
                note "WARNING: could not reinstall the previous dependencies" >&2
        fi
        update_applied=0
        if [ "$managed" -eq 1 ]; then
            systemctl --user start "$service" >/dev/null 2>&1 ||
                note "WARNING: could not start $service after rollback" >&2
            service_stopped=0
        fi
        note "rollback finished; the pre-update database backup is in $backup_dir" >&2
        note "schema migrations are forward-only; use restore.sh if a downgrade is required" >&2
    elif [ "$status" -ne 0 ] && [ "$service_stopped" -eq 1 ] && [ "$managed" -eq 1 ]; then
        systemctl --user start "$service" >/dev/null 2>&1 || true
    fi
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

# ---------------------------------------------------------------------------
# 3. Stop the service: no file changes or migrations while it runs.
# ---------------------------------------------------------------------------
if [ "$managed" -eq 1 ]; then
    if systemctl --user is-active --quiet "$service"; then
        note "stopping $service"
        systemctl --user stop "$service" || die "cannot stop $service; update aborted"
        service_stopped=1
    else
        note "$service is not active"
        service_stopped=1
    fi
fi

# ---------------------------------------------------------------------------
# 4. Update the app tree.
# ---------------------------------------------------------------------------
if [ -n "$source_dir" ]; then
    update_applied=1
    note "installing prebuilt tree from $source_dir"
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete --exclude=.git --exclude=.venv --exclude=venv \
            --exclude=var --exclude=__pycache__ "$source_dir"/ "$app_dir"/ ||
            die "rsync failed"
    else
        note "WARNING: rsync not installed; copying without deleting stale files"
        cp -a "$source_dir"/. "$app_dir"/ || die "copy failed"
    fi
else
    update_applied=1
    if [ -n "$branch" ]; then
        note "fetching origin/$branch"
        git -C "$app_dir" fetch --prune origin || die "git fetch failed"
        git -C "$app_dir" checkout "$branch" || die "git checkout $branch failed"
        git -C "$app_dir" merge --ff-only "origin/$branch" ||
            die "git merge --ff-only origin/$branch failed"
    else
        note "git pull --ff-only"
        git -C "$app_dir" pull --ff-only || die "git pull failed"
    fi
fi

[ -f "$app_dir/app/static/index.html" ] ||
    die "prebuilt app/static/index.html is missing; build the frontend on the dev machine"
note "static frontend present"

# ---------------------------------------------------------------------------
# 5. Python dependencies.
# ---------------------------------------------------------------------------
if [ "$skip_deps" -eq 0 ]; then
    note "installing Python dependencies from requirements.txt"
    "$pip" install -r "$app_dir/requirements.txt" || die "pip install failed"
    deps_installed=1
else
    note "dependency sync skipped (--skip-deps)"
fi

# ---------------------------------------------------------------------------
# 6. Migrations (same cwd and config as the service).
# ---------------------------------------------------------------------------
note "applying database migrations"
if schema_version=$(
    cd "$app_dir" || exit 1
    MUSICNX_CONFIG="$config" "$python" -c '
import asyncio

from app.config import Settings
from app.database.connection import Database
from app.database.migrations import apply_migrations


async def _main() -> None:
    settings = Settings()
    db = Database(settings.paths.database)
    await db.connect()
    try:
        version = await apply_migrations(db)
    finally:
        await db.close()
    print(version)


asyncio.run(_main())
'
); then
    note "schema version: $schema_version"
else
    die "database migrations failed"
fi

# ---------------------------------------------------------------------------
# 7. Start and health check.
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
    systemctl --user start "$service" || die "could not start $service"
    service_stopped=0
    note "started $service"
    if ! wait_health; then
        die "service started but health check failed; see: journalctl --user -u $service -n 100"
    fi
else
    note "systemd service not managed (--no-service); start MusicNX manually"
fi

update_applied=0
note "update complete (schema $schema_version)"
note "rollback snapshot kept: $snapshot"
note "database backups: $backup_dir"

# Keep the three newest code snapshots; failures here do not fail the update.
count=0
for f in "$rollback_dir"/app-*.tar.gz; do
    [ -f "$f" ] || continue
    count=$((count + 1))
done
if [ "$count" -gt 3 ]; then
    remove=$((count - 3))
    for f in "$rollback_dir"/app-*.tar.gz; do
        [ "$remove" -le 0 ] && break
        [ -f "$f" ] || continue
        [ "$f" = "$snapshot" ] && continue
        rm -f -- "$f" 2>/dev/null || true
        remove=$((remove - 1))
    done
fi
