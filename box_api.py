"""Box OAuth とフォルダ作成、ファイルアップロード。"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from pathlib import Path

import requests

import config

SIMPLE_UPLOAD_LIMIT = 20 * 1024 * 1024


class BoxError(RuntimeError):
    pass


class NameConflict(BoxError):
    def __init__(self, file_id: str):
        super().__init__(f"同名ファイルが既にあります: {file_id}")
        self.file_id = file_id


def conflict_id(body: dict) -> str | None:
    conflicts = (body.get("context_info") or {}).get("conflicts")
    if isinstance(conflicts, list):
        conflicts = conflicts[0] if conflicts else None
    if isinstance(conflicts, dict) and conflicts.get("id"):
        return str(conflicts["id"])
    return None


class BoxClient:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.token_path = self.data_dir / "box_token.json"
        self.folder_path = self.data_dir / "folders.json"
        self.state_path = self.data_dir / "oauth_state.txt"
        self._access = None
        self._refresh = None
        self._expires = 0.0
        self._user = None
        self._lock = threading.Lock()
        self._load()

    def _credentials(self) -> dict:
        return config.read_box_settings(self.data_dir)

    def configured(self) -> bool:
        settings = self._credentials()
        return bool(settings["client_id"] and settings["client_secret"] and settings.get("enterprise_id"))

    def connected(self) -> bool:
        with self._lock:
            self._load()
            return bool(self._refresh or self._access)

    def user(self) -> dict | None:
        with self._lock:
            self._load()
            return self._user

    def connect(self) -> dict:
        if not self.configured():
            raise BoxError("Box の Client ID、Client Secret、Enterprise ID を設定画面で入力してください。")
        self.access_token(force=True)
        me = self.current_user()
        user = {"id": me.get("id"), "name": me.get("name"), "login": me.get("login")}
        with self._lock:
            self._user = user
            self._persist()
        return user

    def authorization_url(self, state: str) -> str:
        settings = self._credentials()
        if not self.configured():
            raise BoxError("Box の Client ID、Client Secret、Enterprise ID を設定画面で入力してください。")
        self.state_path.write_text(state, encoding="utf-8")
        return (
            "https://account.box.com/api/oauth2/authorize"
            f"?response_type=code&client_id={settings['client_id']}"
            f"&redirect_uri={requests.utils.quote(settings['redirect_uri'], safe='')}"
            f"&state={requests.utils.quote(state, safe='')}"
        )

    def consume_state(self, state: str) -> None:
        saved = self.state_path.read_text(encoding="utf-8").strip() if self.state_path.exists() else ""
        self.state_path.unlink(missing_ok=True)
        if not saved or saved != state:
            raise BoxError("Box の認可 state が一致しません。接続をやり直してください。")

    def exchange_code(self, code: str) -> dict:
        settings = self._credentials()
        data = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": settings["client_id"],
                "client_secret": settings["client_secret"],
                "redirect_uri": settings["redirect_uri"],
            }
        )
        self._store_token(data)
        me = self.current_user()
        with self._lock:
            self._user = {"id": me.get("id"), "name": me.get("name"), "login": me.get("login")}
            self._persist()
        return self._user

    def disconnect(self) -> None:
        with self._lock:
            self._access = None
            self._refresh = None
            self._expires = 0
            self._user = None
            self.token_path.unlink(missing_ok=True)
        self.folder_path.unlink(missing_ok=True)

    def current_user(self) -> dict:
        response = self._request("GET", "https://api.box.com/2.0/users/me", params={"fields": "id,name,login"})
        self._raise(response, "Box ユーザー情報の取得")
        return response.json()

    def ensure_folders(self) -> dict:
        cached = self._read_folders()
        if not cached:
            settings = self._credentials()
            root_id = self._find_or_create(settings["parent_folder_id"], config.BOX_ROOT_FOLDER_NAME)
            cached = {"parent": settings["parent_folder_id"], "root": root_id}
            for kind, name in config.FOLDER_NAMES.items():
                cached[kind] = self._find_or_create(root_id, name)
        cached["shared_url"] = self.share_with_company(cached["root"])
        self.folder_path.write_text(json.dumps(cached, ensure_ascii=False, indent=2), encoding="utf-8")
        return cached

    def share_with_company(self, folder_id: str) -> str:
        response = self._request(
            "PUT",
            f"https://api.box.com/2.0/folders/{folder_id}",
            params={"fields": "shared_link"},
            json={
                "shared_link": {
                    "access": "company",
                    "permissions": {"can_download": True, "can_preview": True},
                }
            },
        )
        self._raise(response, "社内共有の設定")
        link = (response.json().get("shared_link") or {}).get("url") or ""
        if not link:
            raise BoxError("社内共有リンクを取得できませんでした。")
        return link

    def collaborators(self, folder_id: str) -> list[dict]:
        response = self._request("GET", f"https://api.box.com/2.0/folders/{folder_id}/collaborations")
        self._raise(response, "共有ユーザーの取得")
        result = []
        for item in response.json().get("entries") or []:
            who = item.get("accessible_by") or {}
            result.append(
                {
                    "id": str(item.get("id") or ""),
                    "login": str(who.get("login") or "").lower(),
                    "name": who.get("name") or "",
                    "role": item.get("role") or "",
                    "status": item.get("status") or "",
                }
            )
        return result

    def invite(self, folder_id: str, emails: list[str], role: str) -> list[dict]:
        existing = {item["login"]: item for item in self.collaborators(folder_id)}
        results = []
        for email in emails:
            current = existing.get(email)
            if current and current["role"] == role:
                results.append({"login": email, "status": current["status"] or "accepted", "result": "共有済み"})
                continue
            if current:
                response = self._request(
                    "PUT",
                    f"https://api.box.com/2.0/collaborations/{current['id']}",
                    json={"role": role},
                )
                self._raise(response, f"{email} の権限変更")
                results.append({"login": email, "status": response.json().get("status") or "", "result": "権限を変更"})
                continue
            response = self._request(
                "POST",
                "https://api.box.com/2.0/collaborations",
                params={"notify": "true"},
                json={
                    "item": {"type": "folder", "id": folder_id},
                    "accessible_by": {"type": "user", "login": email},
                    "role": role,
                },
            )
            self._raise(response, f"{email} の招待")
            results.append({"login": email, "status": response.json().get("status") or "", "result": "招待"})
        return results

    def upload(self, kind: str, filename: str, content: bytes, mime: str) -> dict:
        folders = self.ensure_folders()
        folder_id = folders[kind]
        try:
            return self._upload(folder_id, None, filename, content, mime)
        except NameConflict as conflict:
            return self._upload(folder_id, conflict.file_id, filename, content, mime)

    def _upload(self, folder_id: str, file_id: str | None, filename: str, content: bytes, mime: str) -> dict:
        if len(content) >= SIMPLE_UPLOAD_LIMIT:
            return self._chunked_upload(folder_id, file_id, filename, content)
        return self._simple_upload(folder_id, file_id, filename, content, mime)

    def _simple_upload(self, folder_id: str, file_id: str | None, filename: str, content: bytes, mime: str) -> dict:
        if file_id:
            url = f"https://upload.box.com/api/2.0/files/{file_id}/content"
            attributes = {"name": filename}
        else:
            url = "https://upload.box.com/api/2.0/files/content"
            attributes = {"name": filename, "parent": {"id": folder_id}}
        response = self._request(
            "POST",
            url,
            files={
                "attributes": (None, json.dumps(attributes), "application/json"),
                "file": (filename, content, mime or "application/octet-stream"),
            },
            timeout=300,
        )
        if response.status_code == 409 and not file_id:
            found = conflict_id(_json(response))
            if found:
                raise NameConflict(found)
        self._raise(response, "Box へのアップロード")
        return (response.json().get("entries") or [{}])[0]

    def _chunked_upload(self, folder_id: str, file_id: str | None, filename: str, content: bytes) -> dict:
        if file_id:
            url = f"https://upload.box.com/api/2.0/files/{file_id}/upload_sessions"
            body = {"file_size": len(content)}
        else:
            url = "https://upload.box.com/api/2.0/files/upload_sessions"
            body = {"folder_id": folder_id, "file_size": len(content), "file_name": filename}
        response = self._request("POST", url, json=body, timeout=60)
        if response.status_code == 409 and not file_id:
            found = conflict_id(_json(response))
            if found:
                raise NameConflict(found)
        self._raise(response, "Box アップロードセッションの作成")
        session = response.json()
        part_size = int(session.get("part_size") or len(content) or 1)
        endpoints = session.get("session_endpoints") or {}
        upload_url = endpoints.get("upload_part")
        commit_url = endpoints.get("commit")
        parts = []
        offset = 0
        total = len(content)
        while offset < total:
            end = min(offset + part_size, total)
            chunk = content[offset:end]
            part_response = self._request(
                "PUT",
                upload_url,
                data=chunk,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Digest": _sha1_digest(chunk),
                    "Content-Range": f"bytes {offset}-{end - 1}/{total}",
                },
                timeout=300,
            )
            self._raise(part_response, "Box への分割アップロード")
            part = part_response.json().get("part") or {}
            parts.append({"part_id": part.get("part_id"), "offset": offset, "size": end - offset})
            offset = end
        commit = self._request(
            "POST",
            commit_url,
            json={"parts": parts},
            headers={"Digest": _sha1_digest(content), "Content-Type": "application/json"},
            timeout=120,
        )
        if commit.status_code == 202:
            time.sleep(int(commit.headers.get("Retry-After") or 2))
            commit = self._request(
                "POST",
                commit_url,
                json={"parts": parts},
                headers={"Digest": _sha1_digest(content), "Content-Type": "application/json"},
                timeout=120,
            )
        self._raise(commit, "Box アップロードの確定")
        return (commit.json().get("entries") or [{}])[0]

    def _find_or_create(self, parent_id: str, name: str) -> str:
        for entry in self._children(parent_id):
            if entry.get("type") == "folder" and entry.get("name") == name:
                return str(entry["id"])
        response = self._request(
            "POST",
            "https://api.box.com/2.0/folders",
            json={"name": name, "parent": {"id": parent_id}},
        )
        if response.status_code == 409:
            found = conflict_id(_json(response))
            if found:
                return found
        self._raise(response, f"フォルダ「{name}」の作成")
        return str(response.json()["id"])

    def _children(self, folder_id: str):
        offset = 0
        while True:
            response = self._request(
                "GET",
                f"https://api.box.com/2.0/folders/{folder_id}/items",
                params={"limit": 1000, "offset": offset, "fields": "id,name,type"},
            )
            self._raise(response, "フォルダ一覧の取得")
            data = response.json()
            entries = data.get("entries") or []
            yield from entries
            offset += len(entries)
            if not entries or offset >= int(data.get("total_count") or 0):
                break

    def _read_folders(self) -> dict | None:
        if not self.folder_path.exists():
            return None
        data = json.loads(self.folder_path.read_text(encoding="utf-8"))
        if data.get("parent") != self._credentials()["parent_folder_id"]:
            return None
        if not all(kind in data for kind in config.FOLDER_NAMES):
            return None
        return data

    def _token_request(self, form: dict) -> dict:
        response = requests.post("https://api.box.com/oauth2/token", data=form, timeout=30)
        data = _json(response)
        if response.status_code >= 400 or "access_token" not in data:
            message = data.get("error_description") or data.get("error") or response.text[:300]
            raise BoxError(f"Box のトークン取得に失敗しました ({response.status_code}: {message})")
        return data

    def _store_token(self, data: dict) -> None:
        with self._lock:
            self._access = data["access_token"]
            self._refresh = data.get("refresh_token") or self._refresh
            self._expires = time.time() + int(data.get("expires_in") or 3600)
            self._persist()

    def access_token(self, force: bool = False) -> str:
        with self._lock:
            self._load()
            if not force and self._access and time.time() < self._expires - 60:
                return self._access
            settings = self._credentials()
            refresh = self._refresh
        if settings.get("enterprise_id"):
            data = self._token_request(
                {
                    "grant_type": "client_credentials",
                    "client_id": settings["client_id"],
                    "client_secret": settings["client_secret"],
                    "box_subject_type": "enterprise",
                    "box_subject_id": settings["enterprise_id"],
                }
            )
            self._store_token(data)
            return data["access_token"]
        if not refresh:
            raise BoxError("Box に未接続です。設定画面で Enterprise ID を入力し、「サービスアカウントで接続」を押してください。")
        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": settings["client_id"],
            "client_secret": settings["client_secret"],
        }
        data = self._token_request(form)
        self._store_token(data)
        return data["access_token"]

    def _request(self, method: str, url: str, **kwargs):
        timeout = kwargs.pop("timeout", 120)
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {self.access_token()}"
        response = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
        if response.status_code == 401:
            headers["Authorization"] = f"Bearer {self.access_token(force=True)}"
            response = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
        return response

    def _raise(self, response: requests.Response, label: str) -> None:
        if response.status_code < 400:
            return
        data = _json(response)
        message = data.get("message") or data.get("error_description") or response.text[:300]
        raise BoxError(f"{label}に失敗しました ({response.status_code}: {message})")

    def _load(self) -> None:
        if self._access or not self.token_path.exists():
            return
        data = json.loads(self.token_path.read_text(encoding="utf-8"))
        self._access = data.get("access_token")
        self._refresh = data.get("refresh_token")
        self._expires = float(data.get("expires_at") or 0)
        self._user = data.get("user")

    def _persist(self) -> None:
        payload = {
            "access_token": self._access,
            "refresh_token": self._refresh,
            "expires_at": self._expires,
            "user": self._user,
        }
        self.token_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _json(response: requests.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _sha1_digest(data: bytes) -> str:
    return "sha=" + base64.b64encode(hashlib.sha1(data).digest()).decode("ascii")
