"""Zoom Server-to-Server OAuth とファイル取得。"""

from __future__ import annotations

import base64
import threading
import time
from urllib.parse import quote

import requests

import config


class ZoomError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class ZoomClient:
    def __init__(self):
        self._token = None
        self._expires = 0.0
        self._lock = threading.Lock()

    def configured(self) -> bool:
        return bool(config.ZOOM_ACCOUNT_ID and config.ZOOM_CLIENT_ID and config.ZOOM_CLIENT_SECRET)

    def access_token(self, force: bool = False) -> str:
        if not self.configured():
            raise ZoomError("Zoom の Account ID / Client ID / Client Secret が未設定です。")
        with self._lock:
            if not force and self._token and time.time() < self._expires - 60:
                return self._token
            basic = base64.b64encode(
                f"{config.ZOOM_CLIENT_ID}:{config.ZOOM_CLIENT_SECRET}".encode("utf-8")
            ).decode("ascii")
            response = requests.post(
                "https://zoom.us/oauth/token",
                params={
                    "grant_type": "account_credentials",
                    "account_id": config.ZOOM_ACCOUNT_ID,
                },
                headers={"Authorization": f"Basic {basic}"},
                timeout=30,
            )
            data = _json(response)
            if response.status_code >= 400 or "access_token" not in data:
                reason = data.get("reason") or data.get("error") or response.text[:300]
                raise ZoomError(f"Zoom のトークン取得に失敗しました ({response.status_code}: {reason})", response.status_code)
            self._token = data["access_token"]
            self._expires = time.time() + int(data.get("expires_in") or 3600)
            return self._token

    def download(self, url: str) -> tuple[bytes, str]:
        if not url:
            raise ZoomError("ダウンロード URL が空です。")
        token = self.access_token()
        separator = "&" if "?" in url else "?"
        authed = f"{url}{separator}access_token={quote(token, safe='')}"
        response = requests.get(authed, allow_redirects=True, timeout=180)
        if response.status_code == 401:
            token = self.access_token(force=True)
            response = requests.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                allow_redirects=True,
                timeout=180,
            )
        if response.status_code >= 400:
            raise ZoomError(f"Zoom からのダウンロードに失敗しました ({response.status_code})", response.status_code)
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type.lower():
            data = _json(response)
            nested = data.get("download_url") or data.get("file_url") or data.get("url")
            if nested and nested != url:
                return self.download(nested)
            message = data.get("message") or data.get("reason") or "JSON が返されました"
            raise ZoomError(f"Zoom のダウンロード結果を読めませんでした ({message})")
        return response.content, content_type

    def get_json(self, path: str) -> dict:
        token = self.access_token()
        response = requests.get(
            f"https://api.zoom.us/v2{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        )
        if response.status_code == 401:
            token = self.access_token(force=True)
            response = requests.get(
                f"https://api.zoom.us/v2{path}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=60,
            )
        data = _json(response)
        if response.status_code >= 400:
            message = data.get("message") or data.get("reason") or response.text[:300]
            raise ZoomError(f"Zoom API エラー ({response.status_code}: {message})", response.status_code)
        return data

    def ai_call_summary(self, user_id: str, summary_id: str) -> dict:
        return self.get_json(
            f"/phone/user/{quote(user_id, safe='')}/ai_call_summary/{quote(summary_id, safe='')}"
        )

    def meeting_summary(self, meeting_uuid: str) -> dict:
        encoded = quote(meeting_uuid, safe="")
        if meeting_uuid.startswith("/") or "//" in meeting_uuid:
            encoded = quote(encoded, safe="")
        return self.get_json(f"/meetings/{encoded}/meeting_summary")

    def list_fax_logs(self, page_size: int = 30) -> list[dict]:
        data = self.get_json(f"/phone/fax/logs?page_size={int(page_size)}")
        logs = data.get("fax_logs") or []
        return [item for item in logs if isinstance(item, dict)]

    def fax_file(self, fax_log_id: str, file_id: str) -> tuple[bytes, str]:
        token = self.access_token()
        url = f"https://api.zoom.us/v2/phone/fax/logs/{quote(fax_log_id, safe='')}/file/{quote(file_id, safe='')}"
        response = requests.get(url, headers={"Authorization": f"Bearer {token}"}, allow_redirects=True, timeout=180)
        if response.status_code == 401:
            token = self.access_token(force=True)
            response = requests.get(url, headers={"Authorization": f"Bearer {token}"}, allow_redirects=True, timeout=180)
        if response.status_code >= 400:
            raise ZoomError(f"FAX ファイルの取得に失敗しました ({response.status_code})", response.status_code)
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type.lower():
            data = _json(response)
            nested = data.get("download_url") or data.get("file_url") or data.get("url")
            if nested:
                return self.download(nested)
            message = data.get("message") or "FAX ファイルの URL がありません"
            raise ZoomError(message)
        return response.content, content_type


def _json(response: requests.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {"value": data}
