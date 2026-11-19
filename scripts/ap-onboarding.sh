#!/bin/sh
# MusicNX AP onboarding helper (P8).
#
# Creates a temporary NetworkManager Wi-Fi hotspot so a phone can join the Pi
# and open http://10.42.0.1:8080 before the home Wi-Fi is configured. This is a
# deliberately manual helper: it changes the Wi-Fi interface, so it is never
# started automatically by the service. Requires NetworkManager/nmcli and a
# Wi-Fi interface that is not carrying the management connection.
#
# Usage:
#   ap-onboarding.sh start [--ifname wlan0] [--ssid MusicNX-Setup] [--password ...]
#   ap-onboarding.sh stop  [--ifname wlan0]
#   ap-onboarding.sh status
#
# The password is used only in the nmcli argv; it is not written to disk by this
# script. NetworkManager stores the hotspot profile on the Pi (root-owned),
# which is expected for a hotspot.

set -eu

SSID_DEFAULT="MusicNX-Setup"
IFACE_DEFAULT="wlan0"
CON_NAME="musicnx-onboarding"

usage() {
    cat <<'EOF'
MusicNX AP onboarding: temporary NetworkManager Wi-Fi hotspot.

Usage:
  ap-onboarding.sh start [--ifname IFACE] [--ssid NAME] [--password PASS]
  ap-onboarding.sh stop  [--ifname IFACE]
  ap-onboarding.sh status

Options:
  --ifname IFACE   Wi-Fi interface (default wlan0)
  --ssid NAME      hotspot SSID (default MusicNX-Setup, max 32 chars)
  --password PASS  WPA2 password (8..63 chars); generated when omitted
  -h, --help       this help

The Pi is reachable at http://10.42.0.1:8080 after joining. Run `stop` to remove
the hotspot and return the interface to NetworkManager. This script is not run
by the MusicNX service and is not an internet-sharing router.
EOF
}

die() {
    printf 'ap-onboarding: ERROR: %s\n' "$*" >&2
    exit 1
}

note() {
    printf 'ap-onboarding: %s\n' "$*"
}

command -v nmcli >/dev/null 2>&1 || die "nmcli is required (NetworkManager)"
[ "$(id -u)" -ne 0 ] || die "run as the desktop user, not root (systemctl --user context)"

ifname=$IFACE_DEFAULT
ssid=$SSID_DEFAULT
password=""
action=""
open_network=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        start|stop|status)
            [ -z "$action" ] || die "only one action may be given"
            action=$1
            shift
            ;;
        --ifname)
            [ "$#" -ge 2 ] || die "--ifname requires a value"
            ifname=$2
            shift 2
            ;;
        --ssid)
            [ "$#" -ge 2 ] || die "--ssid requires a value"
            ssid=$2
            shift 2
            ;;
        --password)
            [ "$#" -ge 2 ] || die "--password requires a value (use --password '' to generate)"
            password=$2
            shift 2
            ;;
        --open)
            open_network=1
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

[ -n "$action" ] || { usage >&2; die "action is required"; }

case "$ifname" in
    *[!A-Za-z0-9_.:-]*) die "invalid interface name: $ifname" ;;
esac
[ "${#ssid}" -ge 1 ] && [ "${#ssid}" -le 32 ] || die "SSID must be 1..32 characters"
case "$ssid" in
    *'
'*) die "SSID must not contain a newline" ;;
esac

case "$action" in
    start)
        if [ "$open_network" = 1 ]; then
            note "WARNING: open onboarding AP; use only while configuring the Pi"
            nmcli connection delete "$CON_NAME" >/dev/null 2>&1 || true
            nmcli connection add type wifi ifname "$ifname" con-name "$CON_NAME" ssid "$ssid" >/dev/null
            nmcli connection modify "$CON_NAME" 802-11-wireless.mode ap 802-11-wireless.band bg ipv4.method shared
            nmcli connection up "$CON_NAME"
            note "open hotspot active: SSID=$ssid iface=$ifname"
            note "join it, then open http://10.42.0.1:8080/setup"
            exit 0
        fi
        if [ -z "$password" ]; then
            # 16 URL-safe characters; never printed to logs by the service.
            password=$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(12)[:16])
PY
)
            note "generated hotspot password (shown once): $password"
        fi
        [ "${#password}" -ge 8 ] && [ "${#password}" -le 63 ] ||
            die "WPA2 password must be 8..63 characters"
        if nmcli -t -f NAME connection show | grep -Fxq "$CON_NAME"; then
            nmcli connection delete "$CON_NAME" >/dev/null
        fi
        nmcli device wifi hotspot ifname "$ifname" con-name "$CON_NAME" \
            ssid "$ssid" password "$password"
        note "hotspot active: SSID=$ssid iface=$ifname"
        note "join it, then open http://10.42.0.1:8080"
        ;;
    stop)
        if nmcli -t -f NAME connection show | grep -Fxq "$CON_NAME"; then
            nmcli connection down "$CON_NAME" >/dev/null 2>&1 || true
            nmcli connection delete "$CON_NAME" >/dev/null
            note "hotspot stopped and profile removed"
        else
            note "hotspot profile not present; nothing to stop"
        fi
        ;;
    status)
        if nmcli -t -f NAME connection show | grep -Fxq "$CON_NAME"; then
            nmcli -t -f GENERAL.STATE,GENERAL.CONNECTION device show "$ifname" 2>/dev/null || true
            note "hotspot profile exists: $CON_NAME"
        else
            note "hotspot profile absent"
        fi
        ;;
esac
