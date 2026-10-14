import json
import os
import re
import threading
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


ZOOM_ACCOUNT_ID = env("ZOOM_ACCOUNT_ID")
ZOOM_CLIENT_ID = env("ZOOM_CLIENT_ID")
ZOOM_CLIENT_SECRET = env("ZOOM_CLIENT_SECRET")
ZOOM_WEBHOOK_SECRET_TOKEN = env("ZOOM_WEBHOOK_SECRET_TOKEN")

BOX_CLIENT_ID = env("BOX_CLIENT_ID")
BOX_CLIENT_SECRET = env("BOX_CLIENT_SECRET")
BOX_ENTERPRISE_ID = env("BOX_ENTERPRISE_ID")
BOX_REDIRECT_URI = env("BOX_REDIRECT_URI", "http://localhost:8787/box/callback")
BOX_PARENT_FOLDER_ID = env("BOX_PARENT_FOLDER_ID", "0") or "0"
BOX_ROOT_FOLDER_NAME = env("BOX_ROOT_FOLDER_NAME", "Zoom") or "Zoom"

APP_HOST = env("APP_HOST", "0.0.0.0") or "0.0.0.0"
APP_PORT = int(env("APP_PORT", "8787") or "8787")
PUBLIC_BASE_URL = env("PUBLIC_BASE_URL", f"http://localhost:{APP_PORT}").rstrip("/")

DATA_DIR = BASE_DIR / "data"
PUBLIC_URL_FILE = "public_base_url.txt"

# kind -> Box 上のフォルダ名
FOLDER_NAMES = {
    "recordings": "録音",
    "transcripts": "文字起こし",
    "summaries": "議事録",
    "fax": "FAX",
}

def normalize_public_base_url(value: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError("ngrok の URL を入力してください。")
    if "://" not in text:
        text = "https://" + text
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("https:// から始まる ngrok の URL を入力してください。")
    path = parsed.path.rstrip("/")
    if path.endswith("/zoom/webhook"):
        path = path[: -len("/zoom/webhook")]
    if path not in {"", "/"}:
        raise ValueError("ngrok のオリジンだけを入力してください。末尾の /zoom/webhook は自動で外れます。")
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def read_public_base_url(data_dir: Path) -> str:
    path = Path(data_dir) / PUBLIC_URL_FILE
    if path.exists():
        saved = path.read_text(encoding="utf-8").strip()
        if saved:
            return saved.rstrip("/")
    return PUBLIC_BASE_URL


def write_public_base_url(data_dir: Path, value: str) -> str:
    normalized = normalize_public_base_url(value)
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / PUBLIC_URL_FILE).write_text(normalized + "\n", encoding="utf-8")
    return normalized


BOX_SETTINGS_FILE = "box_settings.json"
WEBHOOK_SECRET_FILE = "zoom_webhook_secret.txt"


def read_box_settings(data_dir: Path) -> dict:
    defaults = {
        "client_id": BOX_CLIENT_ID,
        "client_secret": BOX_CLIENT_SECRET,
        "enterprise_id": BOX_ENTERPRISE_ID,
        "redirect_uri": BOX_REDIRECT_URI,
        "parent_folder_id": BOX_PARENT_FOLDER_ID,
    }
    path = Path(data_dir) / BOX_SETTINGS_FILE
    if not path.exists():
        return defaults
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return defaults
    if not isinstance(saved, dict):
        return defaults
    parent = str(saved.get("parent_folder_id") or "").strip() or "0"
    return {
        "client_id": str(saved.get("client_id") or "").strip(),
        "client_secret": str(saved.get("client_secret") or "").strip(),
        "enterprise_id": str(saved.get("enterprise_id") or "").strip(),
        "redirect_uri": str(saved.get("redirect_uri") or "").strip() or BOX_REDIRECT_URI,
        "parent_folder_id": parent,
    }


def write_box_settings(data_dir: Path, client_id: str, client_secret: str, parent_folder_id: str, enterprise_id: str) -> dict:
    client_id = client_id.strip()
    parent_folder_id = parent_folder_id.strip() or "0"
    enterprise_id = enterprise_id.strip()
    if not client_id:
        raise ValueError("Box の Client ID を入力してください。")
    current = read_box_settings(data_dir)
    secret = client_secret.strip() or current["client_secret"]
    if not secret:
        raise ValueError("Box の Client Secret を入力してください。")
    if not enterprise_id.isdigit():
        raise ValueError("Enterprise ID は数字で入力してください。Developer Console 右上のアイコンからコピーできます。")
    if not parent_folder_id.isdigit():
        raise ValueError("保存先フォルダ ID は数字で入力してください。ルートは 0 です。")
    saved = {
        "client_id": client_id,
        "client_secret": secret,
        "enterprise_id": enterprise_id,
        "redirect_uri": current["redirect_uri"],
        "parent_folder_id": parent_folder_id,
    }
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / BOX_SETTINGS_FILE).write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
    return saved


def read_webhook_secret(data_dir: Path) -> str:
    path = Path(data_dir) / WEBHOOK_SECRET_FILE
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ZOOM_WEBHOOK_SECRET_TOKEN


def write_webhook_secret(data_dir: Path, value: str) -> str:
    secret = value.strip()
    if not secret:
        secret = read_webhook_secret(data_dir)
    if not secret:
        raise ValueError("Zoom の Webhook Secret Token を入力してください。")
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / WEBHOOK_SECRET_FILE).write_text(secret + "\n", encoding="utf-8")
    return secret


FAX_POLL_FILE = "fax_poll.json"
DEFAULT_FAX_POLL_SECONDS = 30
_FAX_POLL_LOCK = threading.Lock()


def read_fax_poll(data_dir: Path) -> dict:
    path = Path(data_dir) / FAX_POLL_FILE
    saved = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                saved = loaded
        except json.JSONDecodeError:
            saved = {}
    try:
        interval = int(saved.get("interval") or DEFAULT_FAX_POLL_SECONDS)
    except (TypeError, ValueError):
        interval = DEFAULT_FAX_POLL_SECONDS
    if interval < 10 or interval > 3600:
        interval = DEFAULT_FAX_POLL_SECONDS
    return {
        "interval": interval,
        "last_at": str(saved.get("last_at") or ""),
        "last_message": str(saved.get("last_message") or ""),
    }


def write_fax_poll_interval(data_dir: Path, value: str) -> int:
    text = str(value or "").strip()
    if not text.isdigit():
        raise ValueError("転送間隔は秒数で入力してください。")
    seconds = int(text)
    if seconds < 10 or seconds > 3600:
        raise ValueError("転送間隔は 10 秒から 3600 秒の間で入力してください。")
    with _FAX_POLL_LOCK:
        current = read_fax_poll(data_dir)
        current["interval"] = seconds
        _write_fax_poll(data_dir, current)
    return seconds


def write_fax_poll_result(data_dir: Path, when: str, message: str) -> None:
    with _FAX_POLL_LOCK:
        current = read_fax_poll(data_dir)
        current["last_at"] = when
        current["last_message"] = message
        _write_fax_poll(data_dir, current)


def _write_fax_poll(data_dir: Path, payload: dict) -> None:
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / FAX_POLL_FILE).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


SHARE_FILE = "box_share.json"
SHARE_ROLES = {"viewer": "閲覧者", "editor": "編集者"}
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def read_box_share(data_dir: Path) -> dict:
    path = Path(data_dir) / SHARE_FILE
    if not path.exists():
        return {"emails": [], "role": "viewer"}
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"emails": [], "role": "viewer"}
    role = saved.get("role") if saved.get("role") in SHARE_ROLES else "viewer"
    emails = [str(item).strip().lower() for item in saved.get("emails") or [] if str(item).strip()]
    return {"emails": emails, "role": role}


def write_box_share(data_dir: Path, emails_text: str, role: str) -> dict:
    emails = []
    for item in re.split(r"[\s,;、]+", emails_text or ""):
        item = item.strip().lower()
        if not item:
            continue
        if not EMAIL_PATTERN.match(item):
            raise ValueError(f"メールアドレスの形式を確認してください: {item}")
        if item not in emails:
            emails.append(item)
    if not emails:
        raise ValueError("Box にログインするメールアドレスを 1 件以上入力してください。")
    if role not in SHARE_ROLES:
        role = "viewer"
    saved = {"emails": emails, "role": role}
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / SHARE_FILE).write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
    return saved


SUBSCRIBE_EVENTS = [
    "phone.recording_completed",
    "phone.recording_transcript_completed",
    "phone.ai_call_summary_changed",
    "phone.fax_received",
    "meeting.summary_completed",
    "meeting.summary_updated",
]

ZOOM_SCOPES = [
    "phone:read:call_recording:admin",
    "phone:read:recording_transcript:admin",
    "phone:read:ai_call_summary:admin",
    "phone:read:fax_log:admin",
    "meeting:read:summary:admin",
]
