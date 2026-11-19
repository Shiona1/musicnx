#!/bin/sh
# Enable A2DP (Bluetooth audio) for a headless MusicNX Pi.
#
# Root cause: WirePlumber 0.5 starts its BlueZ monitor only when logind reports
# an "active" seat. A linger user service on a headless Pi only ever sees
# "online", so no A2DP profile is registered and every connect fails with
# "br-connection-profile-unavailable" / "Protocol not available".
#
# Run as the service user (not root), then reconnect the speaker:
#   sh scripts/fix-bluetooth-headless.sh
set -eu

conf_dir="${XDG_CONFIG_HOME:-$HOME/.config}/wireplumber/wireplumber.conf.d"
mkdir -p "$conf_dir"
cat > "$conf_dir/50-headless-bt.conf" <<'EOF'
# Headless Pi: logind never reports an "active" seat for a linger user, so the
# bluez monitor would never start and BlueZ would expose no A2DP profile.
wireplumber.profiles = {
  main = {
    monitor.bluez.seat-monitoring = disabled
  }
}
EOF

systemctl --user restart wireplumber
echo "WirePlumber restarted with Bluetooth seat monitoring disabled."
echo "Check: bluetoothctl show | grep 'Audio Source'"
