#!/usr/bin/env python3
"""Shopee Studio worker for the hermes VM: runs Antigravity CLI (agy) tasks queued by the panel.

Standard library only. Configuration (first match wins):
  env SHOPEE_STUDIO_URL / SHOPEE_STUDIO_WORKER_TOKEN
  ~/.config/shopee-studio-worker/config.json  {"panel_url": "...", "token": "..."}
"""
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

VERSION = "1.0.0"
CONFIG = Path.home() / ".config" / "shopee-studio-worker" / "config.json"
WORK = Path.home() / ".cache" / "shopee-studio-worker"
IMAGE_HOSTS = ("susercontent.com", "shopee.com.br", "shopee.com", "img.shopee")
IMAGE_LIMIT = 6 * 1024 * 1024
IDLE_SECONDS = 5


def log(message):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


def load_config():
    config = {}
    if CONFIG.exists():
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
    url = os.environ.get("SHOPEE_STUDIO_URL") or config.get("panel_url", "")
    token = os.environ.get("SHOPEE_STUDIO_WORKER_TOKEN") or config.get("token", "")
    if not url or not token:
        sys.exit(f"Configure panel_url e token em {CONFIG} (modo 600).")
    return url.rstrip("/"), token


def call(url, token, path, payload, timeout=30):
    request = urllib.request.Request(url + path, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def save_image(image, folder):
    name = "".join(c for c in str(image.get("name", "img")) if c.isalnum())[:12] or "img"
    if image.get("data"):
        data, ext = base64.b64decode(image["data"]), ".png"
    else:
        parsed = urlparse(image.get("url", ""))
        if parsed.scheme != "https" or not any((parsed.hostname or "").endswith(h) for h in IMAGE_HOSTS):
            raise ValueError(f"imagem {name} fora dos domínios da Shopee")
        request = urllib.request.Request(image["url"], headers={"User-Agent": "ShopeeStudioWorker/1.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            kind = response.headers.get_content_type()
            data = response.read(IMAGE_LIMIT + 1)
        if not kind.startswith("image/") or len(data) > IMAGE_LIMIT:
            raise ValueError(f"imagem {name} inválida")
        ext = {"image/png": ".png", "image/webp": ".webp"}.get(kind, ".jpg")
    path = folder / (name + ext)
    path.write_bytes(data)
    return name, path


def run_llm(payload, seconds_left):
    agy = shutil.which("agy") or str(Path.home() / ".local" / "bin" / "agy")
    timeout = max(30, min(int(payload.get("timeout", 180)), seconds_left - 15))
    WORK.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="task-", dir=WORK))
    try:
        mapping = []
        for image in payload.get("images") or []:
            try:
                name, path = save_image(image, folder)
                mapping.append(f"Imagem {name}: {path}")
            except Exception as error:  # A missing candidate image only weakens that comparison.
                mapping.append(f"Imagem {image.get('name')}: indisponível ({error})")
        prompt = payload["prompt"] + ("\n\nARQUIVOS DE IMAGEM (somente leitura):\n" + "\n".join(mapping) if mapping else "")
        schema = folder / "schema.json"
        schema.write_text(json.dumps(payload["schema"]), encoding="utf-8")
        # No --dangerously-skip-permissions: listing titles are untrusted, so terminal tools stay denied.
        command = [agy, "-p", prompt, "--model", payload["model"], "--add-dir", str(folder),
                   "--output-format", "json", "--json-schema", str(schema), "--print-timeout", f"{timeout}s"]
        process = subprocess.run(command, cwd=folder, capture_output=True, text=True, timeout=timeout + 30)
        if process.returncode != 0:
            raise RuntimeError(f"agy saiu com código {process.returncode}: {process.stderr.strip()[-300:]}")
        try:
            result = json.loads(process.stdout)
        except json.JSONDecodeError:
            raise RuntimeError("agy não devolveu JSON: " + (process.stderr.strip()[-300:] or process.stdout[:200])) from None
        if result.get("status") != "SUCCESS":
            raise RuntimeError(f"agy status {result.get('status')}: {process.stderr.strip()[-300:]}")
        output = result.get("structured_output")
        if not isinstance(output, dict):
            raise RuntimeError("agy respondeu sem saída estruturada.")
        return output
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def heartbeat(url, token, worker_id, done):
    """Keep the worker visible to the panel while a long agy call runs."""
    while not done.wait(30):
        try:
            call(url, token, "/api/worker/heartbeat", dict(worker_id=worker_id), timeout=10)
        except Exception:
            pass


def main():
    url, token = load_config()
    worker_id = socket.gethostname()[:64]
    log(f"Worker {VERSION} em {worker_id}; painel {url}")
    while True:
        try:
            task = call(url, token, "/api/worker/claim", dict(worker_id=worker_id, version=VERSION, kinds=["llm"]))["task"]
        except urllib.error.HTTPError as error:
            log(f"Painel recusou ({error.code}); confira o token.")
            time.sleep(60)
            continue
        except (OSError, ValueError) as error:
            log(f"Painel indisponível: {error}")
            time.sleep(30)
            continue
        if not task:
            time.sleep(IDLE_SECONDS)
            continue
        started = time.time()
        log(f"Tarefa {task['id']} ({task['kind']}, {task['payload'].get('model')})")
        busy = threading.Event()
        threading.Thread(target=heartbeat, args=(url, token, worker_id, busy), daemon=True).start()
        try:
            output = run_llm(task["payload"], task["seconds_left"])
            report = dict(worker_id=worker_id, ok=True, output=output)
        except Exception as error:
            report = dict(worker_id=worker_id, ok=False, error=str(error)[:480])
        finally:
            busy.set()
        try:
            call(url, token, f"/api/worker/tasks/{task['id']}/result", report)
            log(f"Tarefa {task['id']}: {'ok' if report['ok'] else report['error']} em {time.time() - started:.0f} s")
        except Exception as error:
            log(f"Tarefa {task['id']}: resultado não aceito ({error})")


if __name__ == "__main__":
    main()
