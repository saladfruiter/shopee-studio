#!/usr/bin/env bash
# Install or update the worker as a systemd user service on the hermes VM.
# Usage: worker/install.sh [PANEL_URL]   (token is read from stdin when the config does not exist yet)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
target="$HOME/.local/share/shopee-studio-worker"
config_dir="$HOME/.config/shopee-studio-worker"
venv="$target/venv"
mkdir -p "$target" "$HOME/.config/systemd/user" "$config_dir"
chmod 700 "$config_dir"
for file in studio_worker.py flow.py subtitles.py; do
  install -m 0644 "$here/$file" "$target/$file"
done
install -m 0755 "$here/flow_login.sh" "$target/flow_login.sh"
install -m 0644 "$here/shopee-studio-worker.service" "$HOME/.config/systemd/user/shopee-studio-worker.service"
# Video tasks: Playwright must match the Chromium build in ~/.cache/ms-playwright (chromium-1234 = 1.62.0).
# Versions pinned to wheels already in the uv cache: hardlinked, almost no extra disk.
if [ ! -x "$venv/bin/python" ]; then
  uv venv -q --python /usr/bin/python3.12 "$venv"
fi
uv pip install -q --python "$venv/bin/python" playwright==1.62.0 faster-whisper==1.2.1 ctranslate2==4.8.1 \
  av==18.1.0 onnxruntime==1.29.0 numpy==1.26.4 tokenizers==0.23.1
if [ ! -f "$config_dir/config.json" ]; then
  url="${1:-http://192.168.1.90:8787}"
  read -r -s -p "Token do worker (Preferências → Worker da VM → Gerar novo token): " token; echo
  umask 077
  WORKER_TOKEN="$token" python3 -c 'import json,os,sys; json.dump({"panel_url": sys.argv[1], "token": os.environ["WORKER_TOKEN"]}, open(sys.argv[2], "w"))' \
    "$url" "$config_dir/config.json"
fi
chmod 600 "$config_dir/config.json"
model="$(python3 -c 'import json,os; print(json.load(open(os.path.expanduser("~/.config/shopee-studio-worker/config.json"))).get("whisper_model","small"))')"
# Download the transcription model now (~460 MB for small), so the first video does not wait for it.
"$venv/bin/python" -c "from faster_whisper import WhisperModel; WhisperModel('$model', device='cpu', compute_type='int8')" \
  && echo "Modelo de transcrição '$model' disponível."
systemctl --user daemon-reload
systemctl --user enable --now shopee-studio-worker.service
systemctl --user restart shopee-studio-worker.service
systemctl --user --no-pager status shopee-studio-worker.service | head -5
