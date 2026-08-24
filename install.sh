#!/usr/bin/env bash
# NightShoot installer - Raspberry Pi OS Bookworm/Trixie (Lite, 64-bit).
# Run from the directory containing this script:  sudo ./install.sh
set -euo pipefail

AP_SSID="${AP_SSID:-NightShoot}"
AP_PASS="${AP_PASS:-starrynight}"          # >= 8 chars
AP_ADDR="${AP_ADDR:-192.168.7.1/24}"
APP_DIR=/opt/nightshoot
STATE_DIR=/var/lib/nightshoot
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m !! %s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
[ ${#AP_PASS} -ge 8 ] || { echo "AP_PASS must be at least 8 characters"; exit 1; }

say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update

# Package names drift between releases (e.g. the 64-bit time_t transition renamed
# libgphoto2-port12 -> libgphoto2-port12t64), so resolve each name against what
# apt actually offers instead of hard-coding one spelling.
pick() {
  for candidate in "$@"; do
    if apt-cache policy "$candidate" 2>/dev/null | grep -q 'Candidate: [^(]'; then
      printf '%s' "$candidate"; return 0
    fi
  done
  return 1
}

PKGS=(python3 python3-venv python3-dev python3-pip
      build-essential pkg-config git
      network-manager avahi-daemon
      gphoto2
      # NetworkManager shells out to these for 'ipv4.method shared'. They are
      # only Recommends, and --no-install-recommends would skip them, which
      # makes the hotspot fail to activate with a very unhelpful error.
      dnsmasq-base iw rfkill iptables)

# The -dev package depends on the correct libgphoto2 runtime, so we only need it.
if DEVPKG="$(pick libgphoto2-dev)"; then
  PKGS+=("$DEVPKG")
else
  warn "libgphoto2-dev not found; python-gphoto2 may fall back to a prebuilt wheel"
fi

apt-get install -y --no-install-recommends "${PKGS[@]}"

say "Removing the desktop auto-mounter (it steals the camera from gphoto2)"
# gvfs grabs the camera the instant it is plugged in and gphoto2 then gets
# "Could not claim the USB device". Harmless if not installed.
apt-get purge -y gvfs-backends gvfs-daemons gvfs 2>/dev/null || true
for svc in gvfs-gphoto2-volume-monitor gvfs-daemon; do
  systemctl --global mask "$svc" 2>/dev/null || true
done

say "Disabling USB autosuspend (it drops the camera mid-sequence)"
CMDLINE=/boot/firmware/cmdline.txt
[ -f "$CMDLINE" ] || CMDLINE=/boot/cmdline.txt
if [ -f "$CMDLINE" ] && ! grep -q 'usbcore.autosuspend' "$CMDLINE"; then
  cp "$CMDLINE" "$CMDLINE.nightshoot.bak"
  sed -i '1 s|$| usbcore.autosuspend=-1|' "$CMDLINE"
  REBOOT_NEEDED=1
fi

say "Installing NightShoot to $APP_DIR"
mkdir -p "$APP_DIR" "$STATE_DIR/thumbs" "$STATE_DIR/captures" "$STATE_DIR/scripts"
cp -r "$SRC_DIR/nightshoot" "$APP_DIR/"
cp -f "$SRC_DIR/README.md" "$APP_DIR/" 2>/dev/null || true
if [ -f "$SRC_DIR/hotspot-test.sh" ]; then
  sed 's/\r$//' "$SRC_DIR/hotspot-test.sh" > "$APP_DIR/hotspot-test.sh"
  chmod +x "$APP_DIR/hotspot-test.sh"
fi
# Strip CR if the files came across from Windows.
find "$APP_DIR/nightshoot" -type f \( -name '*.py' -o -name '*.html' \) \
  -exec sed -i 's/\r$//' {} +

# Example scripts are only seeded, never overwritten, so your edits survive
# a re-run of this installer.
if [ -d "$SRC_DIR/examples/scripts" ]; then
  for f in "$SRC_DIR"/examples/scripts/*.yaml; do
    [ -e "$f" ] || continue
    target="$STATE_DIR/scripts/$(basename "$f")"
    if [ -e "$target" ]; then
      echo "    keeping existing $(basename "$f")"
    else
      sed 's/\r$//' "$f" > "$target"
      echo "    seeded $(basename "$f")"
    fi
  done
fi

python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip wheel
# 'gphoto2' is the PyPI name for the python-gphoto2 bindings. A prebuilt wheel
# is used when available; otherwise it compiles against libgphoto2-dev.
"$APP_DIR/venv/bin/pip" install "flask>=3.0" "gphoto2>=2.5" "PyYAML>=6.0"

say "Installing the systemd service"
sed 's/\r$//' "$SRC_DIR/systemd/nightshoot.service" > /etc/systemd/system/nightshoot.service
systemctl daemon-reload
systemctl enable nightshoot.service
systemctl restart nightshoot.service

say "Configuring the '$AP_SSID' Wi-Fi hotspot"
WIFI_DEV="$(nmcli -t -f DEVICE,TYPE device | awk -F: '$2=="wifi"{print $1; exit}')"
if [ -z "$WIFI_DEV" ]; then
  warn "no Wi-Fi device found - skipping hotspot setup"
else
  rfkill unblock wifi 2>/dev/null || true

  # AP mode is blocked outright until the regulatory domain is known. A fresh
  # Lite image often has it unset, which makes the hotspot fail with no useful
  # message.
  if command -v raspi-config >/dev/null 2>&1; then
    CUR_COUNTRY="$(iw reg get 2>/dev/null | awk -F': ' '/country/{print $1}' | awk '{print $2}' | head -1)"
    if [ -z "$CUR_COUNTRY" ] || [ "$CUR_COUNTRY" = "00" ]; then
      if [ -n "${WIFI_COUNTRY:-}" ]; then
        raspi-config nonint do_wifi_country "$WIFI_COUNTRY" && \
          echo "    set Wi-Fi country to $WIFI_COUNTRY"
      else
        warn "Wi-Fi country is not set - AP mode may refuse to start."
        warn "Fix with:  sudo raspi-config nonint do_wifi_country BE"
      fi
    fi
  fi

  nmcli connection delete nightshoot-ap 2>/dev/null || true
  # pairwise/proto are set explicitly: NetworkManager on Trixie is unreliable
  # in AP mode without them.
  nmcli connection add \
    con-name nightshoot-ap type wifi ifname "$WIFI_DEV" ssid "$AP_SSID" \
    802-11-wireless.mode ap 802-11-wireless.band bg \
    wifi-sec.key-mgmt wpa-psk wifi-sec.proto rsn wifi-sec.pairwise ccmp \
    wifi-sec.psk "$AP_PASS" \
    ipv4.method shared ipv4.address "$AP_ADDR" \
    ipv6.method disabled \
    connection.autoconnect yes connection.autoconnect-priority 0

  # Any Wi-Fi network you have already joined wins, so the Pi still reaches the
  # internet at home for updates and only falls back to the hotspot in the field.
  while IFS=: read -r name type; do
    [ "$type" = "802-11-wireless" ] || continue
    [ "$name" = "nightshoot-ap" ] && continue
    nmcli connection modify "$name" connection.autoconnect-priority 10 2>/dev/null || true
  done < <(nmcli -t -f NAME,TYPE connection show)
fi

say "Checking for the camera"
if gphoto2 --auto-detect | tail -n +3 | grep -q .; then
  gphoto2 --auto-detect | tail -n +3
else
  warn "No camera detected. Plug the Z50 in, switch it ON, and run: gphoto2 --auto-detect"
  warn "If it still fails, the packaged libgphoto2 may predate your body; see README."
fi

IP="${AP_ADDR%%/*}"
say "Done"
cat <<EOF

  Web UI (at home)  :  http://$(hostname).local:8080
  Web UI (in field) :  join Wi-Fi "$AP_SSID"  (password: $AP_PASS)
                       then open  http://$IP:8080

  Scripts folder    :  $STATE_DIR/scripts   (drop .yaml files here)
  Test the hotspot  :  sudo $APP_DIR/hotspot-test.sh check
  Logs              :  journalctl -u nightshoot -f
  Restart           :  sudo systemctl restart nightshoot
EOF
[ -n "${REBOOT_NEEDED:-}" ] && warn "Reboot to apply the USB autosuspend change: sudo reboot"
exit 0
