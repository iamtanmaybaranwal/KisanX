#!/usr/bin/env bash
# Jal-Drishti - update Python packages, check the AI model and restart the service.
# Runs on the Pi without a password (called by deploy.sh after copying new code).
set -euo pipefail

APP="$HOME/jaldrishti"

echo "==> Python environment (same versions as the laptop)"
[ -x "$APP/.venv/bin/python" ] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/python" -m pip install -q --upgrade pip
"$APP/.venv/bin/python" -m pip install -q -e "$APP/ml" \
  "fastapi==0.118.0" "uvicorn[standard]==0.54.0" "paho-mqtt==2.1.0"

echo "==> AI model check"
"$APP/.venv/bin/python" - <<'EOF'
import time
from jaldrishti_ml.predictor import MoisturePredictor
t = time.time(); p = MoisturePredictor()
print(f"   model OK: horizons {p.horizons}, loaded in {time.time() - t:.2f}s")
EOF

if systemctl list-unit-files jaldrishti.service >/dev/null 2>&1 && \
   systemctl list-unit-files jaldrishti.service | grep -q jaldrishti; then
  echo "==> restarting the service"
  sudo -n /usr/bin/systemctl restart jaldrishti
  for i in $(seq 1 40); do
    if curl -fs -m 2 http://localhost/api/state >/dev/null; then
      echo "   running: http://$(hostname -I | awk '{print $1}')/"
      exit 0
    fi
    sleep 1
  done
  echo "   the server did not answer - last log lines:"
  sudo -n /usr/bin/journalctl -u jaldrishti -n 30 --no-pager
  exit 1
else
  echo "==> service not installed yet - run once:  ssh -t jal-pi \"bash ~/jaldrishti/deploy/pi_install.sh\""
fi
