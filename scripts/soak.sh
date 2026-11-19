#!/bin/sh
# MusicNX soak test (read-only).
#
# Runs for N minutes (or a fixed number of samples) and polls the running
# instance: /system/health, /downloads, /sync/status, plus Raspberry Pi
# telemetry (vcgencmd get_throttled, measure_temp) and backend RSS. Every sample
# is appended to a CSV file and a human-readable log. The script performs GET
# requests only: it never changes the queue, the database or the filesystem
# outside its output directory.
#
# Usage:
#   soak.sh [--minutes N | --samples N] [--interval SECONDS] [--url URL]
#           [--out DIR] [--pid PID] [--no-service]
#
# Environment:
#   MUSICNX_BASE_URL     default http://127.0.0.1:8080
#   MUSICNX_SOAK_MINUTES default 60 (ignored with --samples)
#   MUSICNX_SOAK_SAMPLES fixed sample count instead of a duration
#   MUSICNX_SOAK_INTERVAL default 30 seconds
#   MUSICNX_SOAK_DIR     default /var/lib/musicnx/soak
#   MUSICNX_SOAK_TIMEOUT per-request timeout, default 5 seconds
#   MUSICNX_SOAK_PID     backend PID override (default: systemd MainPID, then
#                        pgrep -f 'uvicorn app.main:app')
#   MUSICNX_SERVICE      systemd user unit used for MainPID, default musicnx
#
# CSV columns:
#   timestamp,elapsed_s,health_http,health_status,health_db,uptime_s,
#   downloads_total,downloads_active,downloads_failed,sync_running,sync_last,
#   temp_c,throttled,rss_kb
#
# Exit codes: 0 completed (health answered at least once); 1 health never
# returned 200/ok; 130 interrupted.

set -eu
LC_ALL=C
export LC_ALL

usage() {
    cat <<'EOF'
MusicNX soak: read-only polling of health/downloads/sync and Pi telemetry.

Usage: soak.sh [options]

Options:
  --minutes N     duration in minutes (default: MUSICNX_SOAK_MINUTES or 60)
  --samples N     stop after N samples (smoke test; overrides --minutes)
  --interval SEC  seconds between samples, default 30 (minimum 1)
  --url URL       base URL, default http://127.0.0.1:8080
  --out DIR       output directory for the CSV and log
  --pid PID       backend PID for RSS (default: auto-detect)
  --no-service    do not use systemd for PID detection
  -h, --help      show this help

Writes soak-<timestamp>.csv and soak-<timestamp>.log into --out.
Only GET requests are made. Stop with Ctrl+C; a summary is still written.
EOF
}

die() {
    printf 'soak: ERROR: %s\n' "$*" >&2
    exit 1
}

note() {
    printf 'soak: %s\n' "$*"
}

base_url=${MUSICNX_BASE_URL:-http://127.0.0.1:8080}
minutes=${MUSICNX_SOAK_MINUTES:-60}
samples=${MUSICNX_SOAK_SAMPLES:-}
interval=${MUSICNX_SOAK_INTERVAL:-30}
out_dir=${MUSICNX_SOAK_DIR:-/var/lib/musicnx/soak}
request_timeout=${MUSICNX_SOAK_TIMEOUT:-5}
pid=${MUSICNX_SOAK_PID:-}
service=${MUSICNX_SERVICE-musicnx}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --minutes)
            [ "$#" -ge 2 ] || die "--minutes requires a value"
            minutes=$2
            samples=""
            shift 2
            ;;
        --samples)
            [ "$#" -ge 2 ] || die "--samples requires a value"
            samples=$2
            shift 2
            ;;
        --interval)
            [ "$#" -ge 2 ] || die "--interval requires a value"
            interval=$2
            shift 2
            ;;
        --url)
            [ "$#" -ge 2 ] || die "--url requires a value"
            base_url=$2
            shift 2
            ;;
        --out)
            [ "$#" -ge 2 ] || die "--out requires a value"
            out_dir=$2
            shift 2
            ;;
        --pid)
            [ "$#" -ge 2 ] || die "--pid requires a value"
            pid=$2
            shift 2
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

case "$interval" in
    ''|*[!0-9]*) die "--interval must be a positive integer" ;;
esac
[ "$interval" -ge 1 ] || die "--interval must be >= 1"
case "$request_timeout" in
    ''|*[!0-9]*) die "MUSICNX_SOAK_TIMEOUT must be a positive integer" ;;
esac
[ "$request_timeout" -ge 1 ] || die "MUSICNX_SOAK_TIMEOUT must be >= 1"

if [ -n "$samples" ]; then
    case "$samples" in
        ''|*[!0-9]*) die "--samples must be a positive integer" ;;
    esac
    [ "$samples" -ge 1 ] || die "--samples must be >= 1"
else
    case "$minutes" in
        ''|*[!0-9]*) die "--minutes must be a positive integer" ;;
    esac
    [ "$minutes" -ge 1 ] || die "--minutes must be >= 1"
fi

case "$base_url" in
    http://*|https://*) ;;
    *) die "--url must start with http:// or https://" ;;
esac
base_url=${base_url%/}

command -v curl >/dev/null 2>&1 ||
    die "curl is required (Raspberry Pi OS: sudo apt install curl)"

python_bin=""
if command -v python3 >/dev/null 2>&1; then
    python_bin=python3
elif command -v python >/dev/null 2>&1; then
    python_bin=python
fi

umask 022
mkdir -p -- "$out_dir" || die "cannot create output directory: $out_dir"
[ -d "$out_dir" ] || die "output path is not a directory: $out_dir"
[ -w "$out_dir" ] || die "output directory is not writable: $out_dir"

stamp=$(date +%Y%m%d-%H%M%S)
csv="$out_dir/soak-$stamp-$$.csv"
log="$out_dir/soak-$stamp-$$.log"

tmp_dir="${TMPDIR:-/tmp}/musicnx-soak-$$"
mkdir -p -- "$tmp_dir" || die "cannot create temporary directory: $tmp_dir"
cleanup() {
    rm -rf -- "$tmp_dir" 2>/dev/null || true
}
trap cleanup EXIT
interrupted=0
on_signal() {
    interrupted=1
}
trap on_signal INT TERM

printf '%s\n' \
    'timestamp,elapsed_s,health_http,health_status,health_db,uptime_s,downloads_total,downloads_active,downloads_failed,sync_running,sync_last,temp_c,throttled,rss_kb' \
    > "$csv"

detect_pid() {
    if [ -n "$pid" ]; then
        printf '%s' "$pid"
        return 0
    fi
    if [ -n "$service" ] && command -v systemctl >/dev/null 2>&1; then
        value=$(systemctl --user show -p MainPID --value "$service" 2>/dev/null || true)
        case "$value" in
            ''|0|*[!0-9]*) ;;
            *)
                printf '%s' "$value"
                return 0
                ;;
        esac
    fi
    if command -v pgrep >/dev/null 2>&1; then
        value=$(pgrep -f 'uvicorn app.main:app' 2>/dev/null | head -n 1 || true)
        case "$value" in
            ''|*[!0-9]*) ;;
            *)
                printf '%s' "$value"
                return 0
                ;;
        esac
    fi
    printf ''
}

backend_pid=$(detect_pid)
if [ -z "$backend_pid" ]; then
    note "WARNING: backend PID not found; rss_kb will be 'na' (use --pid)"
else
    note "backend PID: $backend_pid"
fi

fetch() {
    # fetch URL OUTFILE; sets $http_code and writes the body to OUTFILE
    http_code=$(curl -sS -o "$2" -w '%{http_code}' --max-time "$request_timeout" "$1" 2>/dev/null || true)
    case "$http_code" in
        [0-9][0-9][0-9]) ;;
        *) http_code="000" ;;
    esac
}

measure_temp() {
    if command -v vcgencmd >/dev/null 2>&1; then
        out=$(vcgencmd measure_temp 2>/dev/null || true)
        case "$out" in
            temp=*)
                value=${out#temp=}
                value=${value%%\'*}
                case "$value" in
                    ''|*[!0-9.]*) ;;
                    *)
                        printf '%s' "$value"
                        return 0
                        ;;
                esac
                ;;
        esac
    fi
    # sysfs fallback (millidegrees Celsius)
    if [ -r /sys/class/thermal/thermal_zone0/temp ]; then
        raw=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || true)
        case "$raw" in
            ''|*[!0-9]*) ;;
            *) printf '%s.%s' "$((raw / 1000))" "$(((raw % 1000) / 100))" && return 0 ;;
        esac
    fi
    printf 'na'
}

measure_throttled() {
    if command -v vcgencmd >/dev/null 2>&1; then
        out=$(vcgencmd get_throttled 2>/dev/null || true)
        case "$out" in
            throttled=*)
                printf '%s' "${out#throttled=}"
                return 0
                ;;
        esac
    fi
    printf 'na'
}

measure_rss() {
    # The backend can be restarted (deploy, crash) during a long soak; the PID
    # captured at startup may already belong to a dead process. Re-detect it
    # instead of reporting "na" for the rest of the run.
    if [ -z "$backend_pid" ] || ! kill -0 "$backend_pid" 2>/dev/null; then
        backend_pid=$(detect_pid)
    fi
    if [ -n "$backend_pid" ] && command -v ps >/dev/null 2>&1; then
        value=$(ps -o rss= -p "$backend_pid" 2>/dev/null | tr -d '[:space:]' || true)
        case "$value" in
            ''|*[!0-9]*) ;;
            *)
                printf '%s' "$value"
                return 0
                ;;
        esac
    fi
    printf 'na'
}

parse_row() {
    # parse_row HEALTH_JSON DOWNLOADS_JSON SYNC_JSON HTTP_CODE
    if [ -z "$python_bin" ]; then
        printf 'na,na,na,na,na,na,na,na,na'
        return 0
    fi
    "$python_bin" - "$1" "$2" "$3" "$4" <<'PY'
import json
import sys


def load(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def clean(value):
    if value is None or value == "":
        return "na"
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text.replace(",", ";").replace('"', "'")


health = load(sys.argv[1])
downloads = load(sys.argv[2])
sync = load(sys.argv[3])
http_code = sys.argv[4]

health = health if isinstance(health, dict) else {}
downloads = downloads if isinstance(downloads, dict) else {}
sync = sync if isinstance(sync, dict) else {}

items = downloads.get("items")
items = items if isinstance(items, list) else []
active = sum(
    1
    for item in items
    if isinstance(item, dict) and item.get("status") in ("queued", "downloading", "paused")
)
failed = sum(
    1
    for item in items
    if isinstance(item, dict) and item.get("status") == "failed"
)
total = downloads.get("total", "na")

sync_running = "yes" if sync.get("running") else "no"
sync_last = "na"
last_run = sync.get("last")
if isinstance(last_run, dict):
    sync_last = last_run.get("status", "na")

fields = [
    http_code,
    clean(health.get("status")),
    clean(health.get("db")),
    clean(health.get("uptime_s")),
    clean(total),
    active,
    failed,
    sync_running,
    clean(sync_last),
]
print(",".join(clean(value) for value in fields))
PY
}

is_greater() {
    # is_greater A B (numeric, 'na' compares as not greater)
    awk -v a="$1" -v b="$2" 'BEGIN { exit !(a + 0 > b + 0) }'
}

start=$(date +%s)
if [ -z "$samples" ]; then
    end=$((start + minutes * 60))
else
    end=0
fi

n=0
health_ok=0
health_fail=0
throttle_events=0
max_temp=na
max_rss=na
last_sync=na

note "polling $base_url every ${interval}s"
if [ -n "$samples" ]; then
    note "duration: $samples samples"
else
    note "duration: $minutes minutes"
fi
note "csv: $csv"
note "log: $log"

fetch "$base_url/api/v1/system/health" "$tmp_dir/preflight.json"
if [ "$http_code" != "200" ]; then
    die "health endpoint is not reachable (HTTP $http_code): $base_url/api/v1/system/health"
fi

while :; do
    if [ -n "$samples" ]; then
        [ "$n" -lt "$samples" ] || break
    else
        [ "$(date +%s)" -lt "$end" ] || break
    fi

    now=$(date +%s)
    elapsed=$((now - start))
    ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)

    fetch "$base_url/api/v1/system/health" "$tmp_dir/health.json"
    health_code=$http_code
    fetch "$base_url/api/v1/downloads" "$tmp_dir/downloads.json"
    fetch "$base_url/api/v1/sync/status" "$tmp_dir/sync.json"

    temp=$(measure_temp)
    throttled=$(measure_throttled)
    rss=$(measure_rss)

    fields=$(parse_row "$tmp_dir/health.json" "$tmp_dir/downloads.json" "$tmp_dir/sync.json" "$health_code" || true)
    comma_count=$(printf '%s' "$fields" | tr -cd ',' | wc -c | tr -d '[:space:]')
    if [ -z "$fields" ] || [ "$comma_count" -ne 8 ]; then
        fields="na,na,na,na,na,na,na,na,na"
    fi

    health_status=$(printf '%s' "$fields" | cut -d, -f2)
    health_db=$(printf '%s' "$fields" | cut -d, -f3)
    uptime=$(printf '%s' "$fields" | cut -d, -f4)
    downloads_total=$(printf '%s' "$fields" | cut -d, -f5)
    downloads_active=$(printf '%s' "$fields" | cut -d, -f6)
    downloads_failed=$(printf '%s' "$fields" | cut -d, -f7)
    sync_running=$(printf '%s' "$fields" | cut -d, -f8)
    sync_last=$(printf '%s' "$fields" | cut -d, -f9)
    last_sync=$sync_last

    if [ "$health_code" = "200" ] && [ "$health_status" = "ok" ]; then
        health_ok=$((health_ok + 1))
    else
        health_fail=$((health_fail + 1))
    fi
    if [ "$throttled" != "na" ] && [ "$throttled" != "0x0" ]; then
        throttle_events=$((throttle_events + 1))
    fi
    if [ "$temp" != "na" ]; then
        if [ "$max_temp" = "na" ] || is_greater "$temp" "$max_temp"; then
            max_temp=$temp
        fi
    fi
    if [ "$rss" != "na" ]; then
        if [ "$max_rss" = "na" ] || is_greater "$rss" "$max_rss"; then
            max_rss=$rss
        fi
    fi

    printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
        "$ts" "$elapsed" "$health_code" "$health_status" "$health_db" "$uptime" \
        "$downloads_total" "$downloads_active" "$downloads_failed" \
        "$sync_running" "$sync_last" "$temp" "$throttled" "$rss" >> "$csv"

    printf '[%s] elapsed=%ss health=%s/%s db=%s uptime=%ss downloads=%s active=%s failed=%s sync=%s(last=%s) temp=%s throttled=%s rss=%skB\n' \
        "$ts" "$elapsed" "$health_code" "$health_status" "$health_db" "$uptime" \
        "$downloads_total" "$downloads_active" "$downloads_failed" \
        "$sync_running" "$sync_last" "$temp" "$throttled" "$rss" >> "$log"

    n=$((n + 1))
    [ "$interrupted" -eq 0 ] || break

    if [ -n "$samples" ]; then
        [ "$n" -lt "$samples" ] || break
        sleep "$interval" || break
    else
        now=$(date +%s)
        remaining=$((end - now))
        [ "$remaining" -gt 0 ] || break
        sleep_for=$interval
        [ "$sleep_for" -le "$remaining" ] || sleep_for=$remaining
        sleep "$sleep_for" || break
    fi
done

summary_ok="no"
[ "$health_ok" -gt 0 ] && summary_ok="yes"

{
    printf '\n--- soak summary ---\n'
    printf 'samples=%s health_ok=%s health_fail=%s health_ok_any=%s\n' \
        "$n" "$health_ok" "$health_fail" "$summary_ok"
    printf 'max_temp_c=%s max_rss_kb=%s throttled_nonzero_samples=%s last_sync_status=%s\n' \
        "$max_temp" "$max_rss" "$throttle_events" "$last_sync"
    printf 'csv=%s\nlog=%s\n' "$csv" "$log"
    if [ "$interrupted" -eq 1 ]; then
        printf 'interrupted=yes\n'
    fi
} | tee -a "$log"

if [ "$interrupted" -eq 1 ]; then
    exit 130
fi
if [ "$health_ok" -eq 0 ]; then
    note "health never returned 200/ok; check the service and the URL"
    exit 1
fi
note "done: $n samples, health_fail=$health_fail, throttled_nonzero=$throttle_events"
