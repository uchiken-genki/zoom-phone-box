"""ngrok ローカル API から公開 URL と転送先ポートを読み、アプリの設定へ反映する。"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import config

LOGGER = logging.getLogger("zoom_phone_box")
NGROK_FILE = "ngrok.json"
DEFAULT_API = "http://127.0.0.1:4040/api/tunnels"


def addr_port(addr: str) -> int | None:
    text = (addr or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if "://" not in text:
        text = "http://" + text
    parsed = urlparse(text)
    return parsed.port


def choose_tunnel(tunnels: list[dict], local_port: int) -> dict | None:
    for tunnel in tunnels:
        if tunnel.get("proto") != "https":
            continue
        addr = str((tunnel.get("config") or {}).get("addr") or "")
        if addr_port(addr) == local_port and tunnel.get("public_url"):
            return tunnel
    return None


def public_port_of(public_url: str) -> int:
    parsed = urlparse(public_url)
    if parsed.port:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


def apply_tunnel(data_dir: Path, tunnel: dict, local_port: int) -> str:
    public_url = config.write_public_base_url(data_dir, str(tunnel["public_url"]))
    payload = {
        "public_url": public_url,
        "public_port": public_port_of(public_url),
        "local_port": local_port,
    }
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / NGROK_FILE).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return public_url


def read_tunnel(data_dir: Path) -> dict | None:
    path = Path(data_dir) / NGROK_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def api_urls() -> list[str]:
    raw = os.environ.get("NGROK_API_URL", "").strip()
    urls: list[str] = []
    if raw:
        urls.append(raw if raw.rstrip("/").endswith("/api/tunnels") else raw.rstrip("/") + "/api/tunnels")
    if DEFAULT_API not in urls:
        urls.append(DEFAULT_API)
    return urls


def fetch_tunnels(api_url: str, timeout: float = 1.5) -> list[dict]:
    request = urllib.request.Request(api_url, headers={"User-Agent": "zoom-phone-box"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    tunnels = payload.get("tunnels") if isinstance(payload, dict) else None
    return [item for item in tunnels or [] if isinstance(item, dict)]


def sync_public_url(data_dir: Path, local_port: int, timeout: float = 1.5) -> str | None:
    for api_url in api_urls():
        try:
            tunnels = fetch_tunnels(api_url, timeout=timeout)
        except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
            continue
        chosen = choose_tunnel(tunnels, local_port)
        if chosen:
            return apply_tunnel(data_dir, chosen, local_port)
    return None


def watch_public_url(data_dir: Path, local_port: int, interval: float = 15) -> None:
    last = config.read_public_base_url(data_dir)
    while True:
        found = sync_public_url(data_dir, local_port)
        if found and found != last:
            LOGGER.info("ngrok の公開 URL を反映しました: %s （転送先 localhost:%s）", found, local_port)
            last = found
        time.sleep(interval)
