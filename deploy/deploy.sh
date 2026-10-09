#!/usr/bin/env bash
# Jal-Drishti - copy the latest code + AI model + dashboard from the laptop to the Pi
# and restart it. Run from Git Bash on the laptop (from the project folder):
#     bash deploy/deploy.sh            (uses the ssh alias "jal-pi")
#     bash deploy/deploy.sh other-host
# First time only, also run:  ssh -t jal-pi "bash ~/jaldrishti/deploy/pi_install.sh"
set -euo pipefail

HOST="${1:-jal-pi}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> building the dashboard"
(cd dashboard && npm run build --silent)

echo "==> copying to $HOST:~/jaldrishti (no training data, no local database)"
tar -czf - \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
  ml/jaldrishti_ml ml/models ml/pyproject.toml ml/requirements.txt \
  server/jaldrishti_server server/requirements.txt \
  dashboard/dist deploy \
  | ssh "$HOST" 'mkdir -p ~/jaldrishti && tar -xzf - -C ~/jaldrishti'

echo "==> updating and restarting on the Pi"
ssh "$HOST" 'bash ~/jaldrishti/deploy/pi_update.sh'
