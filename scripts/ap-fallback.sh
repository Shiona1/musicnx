#!/bin/sh
# P8 AP fallback: start the onboarding hotspot only when no usable network is
# already up. This is an explicit boot-time helper, not part of the web app:
# eth0/Wi-Fi connectivity always wins, so a configured Pi never starts an AP.
#
# Install (owner, on the Pi):
#   sudo install -m 755 scripts/ap-fallback.sh /usr/local/sbin/musicnx-ap-fallback
#   # run it from a root oneshot unit after NetworkManager, or manually:
#   sudo /usr/local/sbin/musicnx-ap-fallback
#
# It calls ap-onboarding.sh start --open; the portal is
# http://10.42.0.1:8080/setup. Stop with `ap-onboarding.sh stop`.

set -eu

APP_DIR=${MUSICNX_APP_DIR:-"$HOME/musicnx"}
AP_SCRIPT="$APP_DIR/scripts/ap-onboarding.sh"

die() { printf 'ap-fallback: ERROR: %s\n' "$*" >&2; exit 1; }
[ -x "$AP_SCRIPT" ] || die "missing $AP_SCRIPT"

# Any active non-loopback connection means the Pi is already reachable.
if nmcli -t -f TYPE,STATE,DEVICE device status |
    grep -E '^(ethernet|wifi):connected:' >/dev/null 2>&1; then
    printf 'ap-fallback: network already connected; AP not needed\n'
    exit 0
fi

# A known Wi-Fi profile should be tried before exposing an open AP.
if nmcli -t -f TYPE,NAME connection show |
    grep -q '^802-11-wireless:'; then
    printf 'ap-fallback: known Wi-Fi profile exists; not overriding it\n'
    exit 0
fi

printf 'ap-fallback: no network; starting MusicNX-Setup\n'
exec "$AP_SCRIPT" start --ifname "${MUSICNX_AP_IFACE:-wlan0}" --open
