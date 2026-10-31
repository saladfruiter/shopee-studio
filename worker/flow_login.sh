#!/usr/bin/env bash
# Open the worker's Flow browser for a human: login, 2FA, CAPTCHA or checking the project.
# Shows it in the browser of pc1 through noVNC over a reverse SSH tunnel (nothing listens on the LAN).
# Usage: flow_login.sh [ssh-host-of-your-pc]   (default pc1-lan). Ctrl+C closes everything and restarts the worker.
set -euo pipefail
pc="${1:-pc1-lan}"
base="$HOME/.local/share/flow-vm-test"
profile="$(python3 -c 'import json,os; p=os.path.expanduser("~/.config/shopee-studio-worker/config.json"); print(json.load(open(p)).get("flow_profile","")) if os.path.exists(p) else print("")')"
profile="${profile:-$base/profile}"
display=":96"
chrome="$HOME/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome"
pids=()
cleanup() {
  for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
  systemctl --user start shopee-studio-worker.service || true
  echo "Navegador e visualizador encerrados; worker reiniciado."
}
trap cleanup EXIT INT TERM
echo "Parando o worker (o perfil do Flow só pode ser usado por um navegador)..."
systemctl --user stop shopee-studio-worker.service || true
Xvfb "$display" -screen 0 1440x900x24 -nolisten tcp & pids+=($!)
sleep 1
DISPLAY="$display" "$chrome" --no-sandbox --user-data-dir="$profile" --no-first-run --window-size=1440,900 \
  --start-maximized https://flow.google.com/ >/dev/null 2>&1 & pids+=($!)
LD_LIBRARY_PATH="$base/runtime/usr/lib/x86_64-linux-gnu" "$base/runtime/usr/bin/x11vnc" -display "$display" \
  -localhost -rfbport 5906 -forever -shared -nopw -quiet >/dev/null 2>&1 & pids+=($!)
"$base/web-venv/bin/websockify" --web="$base/runtime/usr/share/novnc" 127.0.0.1:6086 127.0.0.1:5906 >/dev/null 2>&1 & pids+=($!)
ssh -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -N -R 127.0.0.1:6086:127.0.0.1:6086 "$pc" & pids+=($!)
sleep 3
echo
echo "No navegador do PC, abra: http://127.0.0.1:6086/vnc.html  → Conectar"
echo "Faça o login ou a verificação no Google Flow. Depois volte aqui e tecle Ctrl+C."
wait
