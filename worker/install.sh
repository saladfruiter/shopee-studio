#!/usr/bin/env bash
# Install or update the worker as a systemd user service on the hermes VM.
# Usage: worker/install.sh [PANEL_URL]   (token is read from stdin when the config does not exist yet)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
target="$HOME/.local/share/shopee-studio-worker"
config_dir="$HOME/.config/shopee-studio-worker"
mkdir -p "$target" "$HOME/.config/systemd/user" "$config_dir"
chmod 700 "$config_dir"
install -m 0644 "$here/studio_worker.py" "$target/studio_worker.py"
install -m 0644 "$here/shopee-studio-worker.service" "$HOME/.config/systemd/user/shopee-studio-worker.service"
if [ ! -f "$config_dir/config.json" ]; then
  url="${1:-http://192.168.1.90:8787}"
  read -r -s -p "Token do worker (Preferências → Worker da VM → Gerar novo token): " token; echo
  umask 077
  WORKER_TOKEN="$token" python3 -c 'import json,os,sys; json.dump({"panel_url": sys.argv[1], "token": os.environ["WORKER_TOKEN"]}, open(sys.argv[2], "w"))' \
    "$url" "$config_dir/config.json"
fi
chmod 600 "$config_dir/config.json"
systemctl --user daemon-reload
systemctl --user enable --now shopee-studio-worker.service
systemctl --user restart shopee-studio-worker.service
systemctl --user --no-pager status shopee-studio-worker.service | head -5
