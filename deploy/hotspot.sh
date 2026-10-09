#!/usr/bin/env bash
# Jal-Drishti - turn the Pi's Wi-Fi into the farm hotspot (for the demo), or back.
#   bash ~/jaldrishti/deploy/hotspot.sh on     -> Wi-Fi "JAL-DRISHTI", Pi = 192.168.4.1
#   bash ~/jaldrishti/deploy/hotspot.sh off    -> back to the normal Wi-Fi
#
# WARNING: the Pi 5 has ONE Wi-Fi radio. "on" disconnects it from your home/college
# Wi-Fi, so an SSH session over Wi-Fi drops. To come back: join the JAL-DRISHTI Wi-Fi
# from the laptop and run  ssh iamtanmaybaranwal@192.168.4.1 "bash ~/jaldrishti/deploy/hotspot.sh off"
# For internet (live weather) while in hotspot mode use a LAN cable or USB phone tethering.
set -euo pipefail
SSID="JAL-DRISHTI"
PASS="jaldrishti123"
CON="jaldrishti-hotspot"

case "${1:-}" in
  on)
    if ! nmcli -t -f NAME con show | grep -qx "$CON"; then
      sudo nmcli con add type wifi ifname wlan0 con-name "$CON" autoconnect yes ssid "$SSID" \
        802-11-wireless.mode ap 802-11-wireless.band bg \
        ipv4.method shared ipv4.addresses 192.168.4.1/24 ipv6.method disabled \
        wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PASS" >/dev/null
    fi
    sudo nmcli con modify "$CON" connection.autoconnect yes connection.autoconnect-priority 100
    sudo nmcli con up "$CON"
    echo "Hotspot ON: Wi-Fi $SSID / $PASS - dashboard http://192.168.4.1/"
    ;;
  off)
    sudo nmcli con modify "$CON" connection.autoconnect no 2>/dev/null || true
    sudo nmcli con down "$CON" 2>/dev/null || true
    echo "Hotspot OFF - reconnecting to the normal Wi-Fi"
    sudo nmcli dev wifi rescan 2>/dev/null || true
    ;;
  *)
    echo "usage: $0 on|off"; exit 1 ;;
esac
