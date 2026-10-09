#!/usr/bin/env bash
# Jal-Drishti - ONE-TIME install of the system parts on the Raspberry Pi.
# Needs your sudo password once. Run from the laptop:
#     ssh -t jal-pi "bash ~/jaldrishti/deploy/pi_install.sh"
#   * Mosquitto MQTT broker reachable by the ESP32 (port 1883)
#   * systemd service: starts on boot, restarts on crash, dashboard on port 80
#   * a narrow sudo rule so later deploys can restart ONLY this service and switch the
#     hotspot without a password
set -euo pipefail

APP="$HOME/jaldrishti"
U="$(id -un)"

echo "==> system packages (mosquitto, python venv)"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq mosquitto python3-venv curl >/dev/null

echo "==> MQTT broker: listen on the local network for the ESP32"
sudo tee /etc/mosquitto/conf.d/jaldrishti.conf >/dev/null <<'EOF'
# Jal-Drishti: the ESP32 connects over the farm Wi-Fi (private hotspot).
listener 1883 0.0.0.0
allow_anonymous true
EOF
sudo systemctl enable mosquitto >/dev/null 2>&1
sudo systemctl restart mosquitto

echo "==> service: auto-start on boot, dashboard on port 80"
sudo tee /etc/systemd/system/jaldrishti.service >/dev/null <<EOF
[Unit]
Description=Jal-Drishti smart irrigation server
After=network-online.target mosquitto.service
Wants=network-online.target

[Service]
User=$U
WorkingDirectory=$APP/server
Environment=JD_PROFILE=demo
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP/.venv/bin/python -m jaldrishti_server --mqtt localhost --port 80
AmbientCapabilities=CAP_NET_BIND_SERVICE
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

echo "==> permission: restart this service + hotspot without a password (nothing else)"
RULE=/etc/sudoers.d/jaldrishti
sudo tee "$RULE.tmp" >/dev/null <<EOF
$U ALL=(root) NOPASSWD: /usr/bin/systemctl restart jaldrishti, /usr/bin/systemctl stop jaldrishti, /usr/bin/systemctl start jaldrishti, /usr/bin/systemctl status jaldrishti, /usr/bin/journalctl -u jaldrishti *, /usr/bin/nmcli *
EOF
sudo chmod 440 "$RULE.tmp"
sudo visudo -cf "$RULE.tmp" >/dev/null && sudo mv "$RULE.tmp" "$RULE"

echo "==> stopping the temporary test server (if running)"
pkill -f "jaldrishti_server --sim" 2>/dev/null || true

sudo systemctl daemon-reload
sudo systemctl enable jaldrishti >/dev/null 2>&1
echo "==> install done - now starting the app"
bash "$APP/deploy/pi_update.sh"
