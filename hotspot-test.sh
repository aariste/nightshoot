#!/usr/bin/env bash
# Test and troubleshoot the NightShoot field hotspot without locking yourself out.
#
#   sudo ./hotspot-test.sh check      config + radio, changes nothing
#   sudo ./hotspot-test.sh diagnose   why the hotspot will not start
#   sudo ./hotspot-test.sh start      hotspot on, auto-reverts after 10 minutes
#   sudo ./hotspot-test.sh start 300  ... after 5 minutes
#   sudo ./hotspot-test.sh stop       hotspot off, rejoin Wi-Fi now
#   sudo ./hotspot-test.sh status     what is active right now
#
# One Wi-Fi radio cannot be a client and an access point at once, so starting the
# hotspot WILL drop an SSH session that runs over Wi-Fi. That is expected. The
# timed auto-revert is the safety net for when the hotspot does not come up.
set -uo pipefail

AP_CON="${AP_CON:-nightshoot-ap}"
REVERT_UNIT="nightshoot-ap-revert"
LOG=/tmp/nightshoot-ap.log

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  + %s\033[0m\n' "$*"; }
bad()  { printf '\033[1;31m  - %s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

wifi_dev() { nmcli -t -f DEVICE,TYPE device 2>/dev/null | awk -F: '$2=="wifi"{print $1; exit}'; }
ap_addr()  { nmcli -t -f ipv4.address connection show "$AP_CON" 2>/dev/null | cut -d: -f2- | cut -d/ -f1; }
ap_ssid()  { nmcli -g 802-11-wireless.ssid connection show "$AP_CON" 2>/dev/null; }
ap_active(){ nmcli -t -f NAME connection show --active 2>/dev/null | grep -qx "$AP_CON"; }
active_wifi_con() {
  nmcli -t -f NAME,TYPE,DEVICE connection show --active 2>/dev/null \
    | awk -F: -v d="$(wifi_dev)" '$2=="802-11-wireless" && $3==d {print $1; exit}'
}

# ---------------------------------------------------------------- prerequisites

# Returns 0 if everything the hotspot needs is present. Prints what is missing.
preflight() {
  local problems=0 dev
  dev="$(wifi_dev)"

  if [ -z "$dev" ]; then bad "no Wi-Fi device found"; return 1; fi
  ok "Wi-Fi device: $dev"

  # The usual culprit. NetworkManager runs dnsmasq to hand out DHCP leases on a
  # shared connection; without it activation fails immediately.
  if [ -x /usr/sbin/dnsmasq ] || dpkg -s dnsmasq-base >/dev/null 2>&1; then
    ok "dnsmasq-base present (needed for ipv4.method shared)"
  else
    bad "dnsmasq-base is MISSING - this alone stops the hotspot"
    echo "        fix:  sudo apt install -y dnsmasq-base"
    problems=1
  fi

  if command -v iptables >/dev/null 2>&1 || command -v nft >/dev/null 2>&1; then
    ok "packet filter available for address sharing"
  else
    bad "neither iptables nor nftables found"
    echo "        fix:  sudo apt install -y iptables"
    problems=1
  fi

  # AP mode stays disabled until the radio knows its regulatory domain.
  local reg
  reg="$(iw reg get 2>/dev/null | awk -F': ' '/country/{print $1}' | awk '{print $2}' | head -1)"
  if [ -z "$reg" ] || [ "$reg" = "00" ]; then
    bad "Wi-Fi country is not set (regulatory domain ${reg:-unknown})"
    echo "        fix:  sudo raspi-config nonint do_wifi_country BE   # your ISO code"
    problems=1
  else
    ok "Wi-Fi country: $reg"
  fi

  if command -v rfkill >/dev/null 2>&1; then
    if rfkill list wifi 2>/dev/null | grep -q "yes"; then
      bad "the Wi-Fi radio is rfkill-blocked"
      rfkill list wifi | sed 's/^/        /'
      echo "        fix:  sudo rfkill unblock wifi"
      problems=1
    else
      ok "radio is not rfkill-blocked"
    fi
  else
    warn "rfkill not installed - cannot check for a blocked radio"
  fi

  if command -v iw >/dev/null 2>&1; then
    if iw list 2>/dev/null | sed -n '/Supported interface modes/,/Band /p' | grep -qw AP; then
      ok "radio supports AP mode"
    else
      bad "this radio does not advertise AP mode"
      problems=1
    fi
  else
    warn "iw not installed - cannot verify AP capability"
  fi

  if nmcli -t -f NAME connection show 2>/dev/null | grep -qx "$AP_CON"; then
    ok "hotspot profile '$AP_CON' exists"
  else
    bad "no '$AP_CON' profile - re-run install.sh"
    problems=1
  fi

  return $problems
}

# -------------------------------------------------------------------- commands

cmd_check() {
  say "Prerequisites"
  local pre_ok=0
  preflight || pre_ok=1

  if nmcli -t -f NAME connection show 2>/dev/null | grep -qx "$AP_CON"; then
    say "Hotspot profile"
    echo "      SSID          : $(ap_ssid)"
    echo "      password      : $(nmcli -s -g 802-11-wireless-security.psk connection show "$AP_CON" 2>/dev/null)"
    echo "      address       : $(ap_addr)"
    echo "      mode          : $(nmcli -g 802-11-wireless.mode connection show "$AP_CON" 2>/dev/null)"
    echo "      ipv4 method   : $(nmcli -g ipv4.method connection show "$AP_CON" 2>/dev/null)"
    echo "      autoconnect   : $(nmcli -g connection.autoconnect connection show "$AP_CON" 2>/dev/null)" \
         "(priority $(nmcli -g connection.autoconnect-priority connection show "$AP_CON" 2>/dev/null))"
    echo "      web UI        : http://$(ap_addr):8080"
  fi

  say "Saved Wi-Fi networks (these win over the hotspot when in range)"
  local found=0 name type
  while IFS=: read -r name type; do
    [ "$type" = "802-11-wireless" ] || continue
    [ "$name" = "$AP_CON" ] && continue
    found=1
    printf '      %-28s priority %s\n' "$name" \
      "$(nmcli -g connection.autoconnect-priority connection show "$name" 2>/dev/null)"
  done < <(nmcli -t -f NAME,TYPE connection show 2>/dev/null)
  [ "$found" = 1 ] || echo "      (none)"

  say "Verdict"
  if [ "$pre_ok" = 0 ]; then
    echo "  Everything the hotspot needs is in place."
    echo "  In the field, with no saved network in range, '$AP_CON' comes up by itself."
    echo "  To prove it now:  sudo $0 start"
  else
    bad "fix the items marked above, then run:  sudo $0 check"
  fi
}

cmd_diagnose() {
  say "Prerequisites"
  preflight || true

  local dev; dev="$(wifi_dev)"
  say "Device state"
  nmcli -f GENERAL.STATE,GENERAL.REASON,GENERAL.CONNECTION device show "$dev" 2>/dev/null | sed 's/^/      /'

  say "Last activation attempt"
  if [ -s "$LOG" ]; then sed 's/^/      /' "$LOG"; else echo "      (no $LOG yet - run 'start' first)"; fi

  say "Trying the hotspot in the foreground (5s)"
  echo "  Any error below is the real reason it will not start."
  local out rc
  out="$(timeout 20 nmcli connection up "$AP_CON" 2>&1)"; rc=$?
  echo "$out" | sed 's/^/      /'
  if [ "$rc" = 0 ]; then
    ok "it activated - bringing it straight back down"
    nmcli connection down "$AP_CON" >/dev/null 2>&1
    nmcli device connect "$dev" >/dev/null 2>&1 || true
  else
    bad "activation failed (exit $rc)"
  fi

  say "NetworkManager log, last 2 minutes"
  journalctl -u NetworkManager --since "2 min ago" --no-pager 2>/dev/null \
    | grep -iE "$AP_CON|dnsmasq|ap mode|sharing|hostapd|supplicant|error|fail" \
    | tail -25 | sed 's/^/      /' || echo "      (nothing)"
}

cmd_status() {
  say "Active connections"
  nmcli -t -f NAME,TYPE,DEVICE connection show --active 2>/dev/null | sed 's/^/      /'
  local dev ip; dev="$(wifi_dev)"
  ip="$(nmcli -t -f IP4.ADDRESS device show "$dev" 2>/dev/null | cut -d: -f2- | head -1)"
  echo
  echo "      $dev address: ${ip:-none}"
  if systemctl list-timers --all 2>/dev/null | grep -q "$REVERT_UNIT"; then
    warn "an automatic revert is still scheduled"
    systemctl list-timers --all 2>/dev/null | grep "$REVERT_UNIT" | sed 's/^/      /'
  fi
  if ap_active; then
    echo; ok "hotspot is UP - join '$(ap_ssid)'"
    echo "      then browse to http://$(ap_addr):8080"
  fi
}

cmd_start() {
  local seconds="${1:-600}"
  local previous user ssid addr dev
  dev="$(wifi_dev)"
  previous="$(active_wifi_con)"
  user="${SUDO_USER:-${USER:-pi}}"
  ssid="$(ap_ssid)"
  addr="$(ap_addr)"

  say "Prerequisites"
  if ! preflight; then
    bad "not starting - fix the above first, or run:  sudo $0 diagnose"
    exit 1
  fi

  say "Starting the hotspot for ${seconds}s"
  [ -n "$previous" ] && [ "$previous" != "$AP_CON" ] && \
    echo "  Currently joined to '$previous'. That will drop."
  cat <<EOF

  If you are connected over Wi-Fi, THIS SSH SESSION WILL FREEZE. That is normal.

  To get back in:
    1. join Wi-Fi "$ssid" from your phone or laptop
    2. open  http://$addr:8080     (the NightShoot UI)
       or    ssh $user@$addr

  If the hotspot fails to appear, the Pi rejoins your normal Wi-Fi
  automatically after ${seconds}s. Just wait it out - nothing is broken.

EOF
  read -r -p "  Continue? [y/N] " reply
  case "$reply" in [yY]*) ;; *) echo "  aborted"; exit 0;; esac

  # Safety net first, so a lost session can never strand the Pi on the hotspot.
  systemctl stop "${REVERT_UNIT}.timer" 2>/dev/null || true
  systemctl reset-failed "$REVERT_UNIT" 2>/dev/null || true
  systemd-run --quiet --on-active="$seconds" --unit="$REVERT_UNIT" \
    /usr/bin/nmcli connection up "${previous:-$dev}" 2>/dev/null \
    || systemd-run --quiet --on-active="$seconds" --unit="$REVERT_UNIT" \
       /usr/bin/nmcli connection down "$AP_CON"
  ok "auto-revert armed for ${seconds}s from now"

  # Detached, because this very session dies the moment the AP takes the radio.
  : > "$LOG"
  setsid nmcli connection up "$AP_CON" >"$LOG" 2>&1 &
  local pid=$!

  # Poll instead of a blind sleep: if activation fails fast we can actually say
  # so, rather than printing "done" over a hotspot that never came up.
  local i
  for i in $(seq 1 25); do
    sleep 1
    if ap_active; then
      ok "hotspot activated after ${i}s"
      cmd_status
      return 0
    fi
    if ! kill -0 "$pid" 2>/dev/null && [ -s "$LOG" ]; then
      break
    fi
  done

  echo
  bad "the hotspot did not come up"
  [ -s "$LOG" ] && { echo "  nmcli said:"; sed 's/^/      /' "$LOG"; }
  echo
  echo "  Cancelling the revert timer and restoring Wi-Fi."
  systemctl stop "${REVERT_UNIT}.timer" 2>/dev/null || true
  systemctl reset-failed "$REVERT_UNIT" 2>/dev/null || true
  nmcli connection down "$AP_CON" >/dev/null 2>&1 || true
  nmcli device connect "$dev" >/dev/null 2>&1 || true
  echo
  echo "  Next step:  sudo $0 diagnose"
  exit 1
}

cmd_stop() {
  say "Stopping the hotspot"
  systemctl stop "${REVERT_UNIT}.timer" 2>/dev/null || true
  systemctl reset-failed "$REVERT_UNIT" 2>/dev/null || true
  nmcli connection down "$AP_CON" 2>/dev/null || true
  sleep 3
  # NetworkManager picks the highest-priority saved network itself; nudge it in
  # case nothing is in range yet.
  nmcli device connect "$(wifi_dev)" >/dev/null 2>&1 || true
  sleep 3
  cmd_status
}

case "${1:-check}" in
  check)    cmd_check ;;
  diagnose) cmd_diagnose ;;
  start)    cmd_start "${2:-600}" ;;
  stop)     cmd_stop ;;
  status)   cmd_status ;;
  *) echo "usage: sudo $0 {check|diagnose|start [seconds]|stop|status}"; exit 1 ;;
esac
