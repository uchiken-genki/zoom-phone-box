"""Zoom のイベントを資料種別に分けて Box へ保存する。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import config
from store import Store
from zoom_api import ZoomError

TOKYO = ZoneInfo("Asia/Tokyo")
UTC = ZoneInfo("UTC")
INVALID = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')

MIME = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".mp4": "video/mp4",
    ".pdf": "application/pdf",
    ".vtt": "text/vtt",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json",
    ".md": "text/markdown; charset=utf-8",
}


def handle_event(body: dict, zoom, box, store: Store) -> dict:
    event = body.get("event") or ""
    payload = body.get("payload") or {}
    account_id = payload.get("account_id")
    if account_id and config.ZOOM_ACCOUNT_ID and account_id != config.ZOOM_ACCOUNT_ID:
        return _result(event, "skipped", "別アカウントのイベントのため保存しませんでした")

    if event in {"phone.recording_completed", "phone.recording_completed_for_access_member"}:
        return _save_recordings(event, payload, zoom, box, store)
    if event in {"phone.recording_transcript_completed", "phone.recording_transcript_completed_for_access_member"}:
        return _save_transcripts(event, payload, zoom, box, store)
    if event == "phone.ai_call_summary_changed":
        return _save_phone_summary(event, payload, zoom, box, store)
    if event == "phone.fax_received":
        return _save_fax(event, payload, zoom, box, store)
    if event in {"meeting.summary_completed", "meeting.summary_updated"}:
        return _save_meeting_summary(event, payload, zoom, box, store)
    return _result(event, "skipped", "このアプリが保存対象にしていないイベントです")


def _save_recordings(event, payload, zoom, box, store: Store) -> dict:
    items = _recordings(payload)
    if not items:
        return _result(event, "skipped", "録音データがありません")
    messages = []
    for item in items:
        recording_id = str(item.get("id") or "")
        key = f"recording:{recording_id}"
        if not store.claim(key):
            messages.append(f"録音 {recording_id} は保存済みです")
            continue
        try:
            content, content_type = zoom.download(item.get("download_url") or "")
            ext = sniff_audio_ext(content, content_type)
            filename = clip_filename(_call_filename(item, "録音", ext))
            uploaded = box.upload("recordings", filename, content, MIME.get(ext, "application/octet-stream"))
            store.finish(key)
            messages.append(f"録音/{filename} (file {uploaded.get('id')})")
        except Exception:
            store.release(key)
            raise
    return _batch_result(event, messages)


def _save_transcripts(event, payload, zoom, box, store: Store) -> dict:
    items = _recordings(payload)
    if not items:
        return _result(event, "skipped", "文字起こしデータがありません")
    messages = []
    for item in items:
        recording_id = str(item.get("id") or "")
        key = f"transcript:{recording_id}"
        if not store.claim(key):
            messages.append(f"文字起こし {recording_id} は保存済みです")
            continue
        try:
            content, content_type = zoom.download(item.get("transcript_download_url") or "")
            ext = sniff_text_ext(content, content_type)
            filename = clip_filename(_call_filename(item, "文字起こし", ext))
            uploaded = box.upload("transcripts", filename, content, MIME.get(ext, "text/plain; charset=utf-8"))
            store.finish(key)
            messages.append(f"文字起こし/{filename} (file {uploaded.get('id')})")
        except Exception:
            store.release(key)
            raise
    return _batch_result(event, messages)


def _save_phone_summary(event, payload, zoom, box, store: Store) -> dict:
    obj = payload.get("object") or {}
    summary_id = str(obj.get("ai_call_summary_id") or "")
    user_id = str(obj.get("user_id") or "")
    modified = obj.get("modified_time") or obj.get("created_time") or ""
    key = f"phone-summary:{summary_id}:{modified}:{'deleted' if obj.get('deleted') else 'active'}"
    if not store.claim(key):
        return _result(event, "skipped", "この電話の要約は保存済みです")
    try:
        if obj.get("deleted"):
            store.finish(key)
            return _result(event, "skipped", "要約の削除通知のため、Box には保存しませんでした")
        if not summary_id or not user_id:
            raise RuntimeError("AI 要約 ID またはユーザー ID がありません")
        try:
            fetched = zoom.ai_call_summary(user_id, summary_id)
        except ZoomError as exc:
            if exc.status in {401, 403}:
                raise RuntimeError(
                    "Zoom が電話の AI 要約本文の取得を拒否しました。"
                    " phone:read:ai_call_summary:admin を付けていても、"
                    "通話の所有者以外は要約を読めない制限がある場合があります。"
                    f" ({exc})"
                ) from exc
            raise
        merged = _merge(obj, fetched)
        text = render_phone_summary(merged)
        filename = clip_filename(
            f"{stamp_from(merged.get('created_time') or modified)}_電話_{safe_part(merged.get('call_id'), 24)}.md"
        )
        uploaded = box.upload("summaries", filename, text.encode("utf-8"), MIME[".md"])
        store.finish(key)
        return _result(event, "saved", f"議事録/{filename} (file {uploaded.get('id')})")
    except Exception:
        store.release(key)
        raise


def _save_meeting_summary(event, payload, zoom, box, store: Store) -> dict:
    obj = payload.get("object") or {}
    meeting_uuid = str(obj.get("meeting_uuid") or "")
    modified = obj.get("summary_last_modified_time") or obj.get("summary_created_time") or ""
    key = f"meeting-summary:{meeting_uuid}:{modified}"
    if not store.claim(key):
        return _result(event, "skipped", "この会議の議事録は保存済みです")
    try:
        fetched = {}
        if not _has_summary_body(obj) and meeting_uuid:
            try:
                fetched = zoom.meeting_summary(meeting_uuid)
            except ZoomError as exc:
                if exc.status in {401, 403}:
                    raise RuntimeError(
                        "会議の AI 議事録を取得できませんでした。"
                        " meeting:read:summary:admin と、ホスト側の AI Companion 議事録が有効かを確認してください。"
                        f" ({exc})"
                    ) from exc
                raise
        merged = _merge(obj, fetched)
        text = render_meeting_summary(merged)
        filename = clip_filename(
            "{stamp}_会議_{meeting}_{topic}.md".format(
                stamp=stamp_from(merged.get("meeting_start_time") or merged.get("summary_created_time")),
                meeting=safe_part(merged.get("meeting_id"), 20),
                topic=safe_part(merged.get("meeting_topic") or merged.get("summary_title"), 40),
            )
        )
        uploaded = box.upload("summaries", filename, text.encode("utf-8"), MIME[".md"])
        store.finish(key)
        return _result(event, "saved", f"議事録/{filename} (file {uploaded.get('id')})")
    except Exception:
        store.release(key)
        raise


def _save_fax(event, payload, zoom, box, store: Store) -> dict:
    obj = payload.get("object") or {}
    fax_log_id = str(obj.get("fax_log_id") or "")
    file_id = str(obj.get("file_id") or "")
    key = f"fax:{fax_log_id}:{file_id}"
    if not store.claim(key):
        return _result(event, "skipped", "この FAX は保存済みです")
    try:
        status = (obj.get("status") or "").lower()
        if status in {"failed", "canceled"}:
            store.finish(key)
            return _result(event, "skipped", f"FAX は {status} のため保存しませんでした")
        if not fax_log_id or not file_id:
            raise RuntimeError("FAX の log ID または file ID がありません")
        content, content_type = zoom.fax_file(fax_log_id, file_id)
        ext = ".pdf" if "pdf" in content_type.lower() or content.startswith(b"%PDF") else sniff_audio_ext(content, content_type)
        if ext not in {".pdf", ".tif", ".tiff", ".png", ".jpg"}:
            ext = ".pdf"
        when = obj.get("created_at") or obj.get("create_time")
        filename = clip_filename(
            "{stamp}_{sender}_to_{receiver}_{log}{ext}".format(
                stamp=stamp_from(when),
                sender=safe_part(obj.get("sender_number") or obj.get("sender_name"), 24),
                receiver=safe_part(obj.get("receiver_number") or obj.get("receiver_name"), 24),
                log=safe_part(fax_log_id, 16),
                ext=ext,
            )
        )
        uploaded = box.upload("fax", filename, content, "application/pdf" if ext == ".pdf" else "application/octet-stream")
        store.finish(key)
        return _result(event, "saved", f"FAX/{filename} (file {uploaded.get('id')})")
    except Exception:
        store.release(key)
        raise


def render_meeting_summary(obj: dict) -> str:
    title = obj.get("summary_title") or obj.get("meeting_topic") or "議事録"
    lines = [
        f"# {title}",
        "",
        f"- 会議: {obj.get('meeting_topic') or ''}",
        f"- 会議ID: {obj.get('meeting_id') or ''}",
        f"- ホスト: {obj.get('meeting_host_email') or ''}",
        f"- 開始: {obj.get('meeting_start_time') or ''}",
        f"- 終了: {obj.get('meeting_end_time') or ''}",
    ]
    if obj.get("summary_doc_url"):
        lines.append(f"- Zoom Docs: {obj.get('summary_doc_url')}")
    lines.append("")
    content = obj.get("summary_content")
    if isinstance(content, str) and content.strip():
        lines.append(content.strip())
        lines.append("")
        return "\n".join(lines)
    overview = obj.get("summary_overview")
    if overview:
        lines.extend(["## 概要", "", str(overview).strip(), ""])
    for item in obj.get("summary_details") or []:
        if isinstance(item, dict):
            lines.extend([f"## {item.get('label') or '詳細'}", "", str(item.get("summary") or "").strip(), ""])
        elif item:
            lines.extend([f"- {item}", ""])
    steps = obj.get("next_steps") or []
    if steps:
        lines.extend(["## 次のステップ", ""])
        for step in steps:
            lines.append(f"- {step}")
        lines.append("")
    if not _has_summary_body(obj):
        lines.extend(["## 取得データ", "", "```json", json.dumps(obj, ensure_ascii=False, indent=2), "```", ""])
    return "\n".join(lines)


def render_phone_summary(obj: dict) -> str:
    summary = _first_text(obj, ("summary_content", "detailed_summary", "call_summary", "summary", "summary_overview"))
    steps = _find_key(obj, "next_steps") or []
    lines = [
        "# Zoom Phone AI 要約",
        "",
        f"- 通話ID: {obj.get('call_id') or ''}",
        f"- ユーザーID: {obj.get('user_id') or ''}",
        f"- 作成: {obj.get('created_time') or ''}",
        f"- 更新: {obj.get('modified_time') or ''}",
        "",
    ]
    if summary:
        lines.extend([summary.strip(), ""])
    if isinstance(steps, list) and steps:
        lines.extend(["## 次のステップ", ""])
        for step in steps:
            lines.append(f"- {step}")
        lines.append("")
    elif isinstance(steps, str) and steps.strip():
        lines.extend(["## 次のステップ", "", steps.strip(), ""])
    if not summary and not steps:
        lines.extend(["## 取得データ", "", "```json", json.dumps(obj, ensure_ascii=False, indent=2), "```", ""])
    return "\n".join(lines)


def sniff_audio_ext(data: bytes, content_type: str) -> str:
    mapped = _ext_from_type(content_type)
    if mapped:
        return mapped
    if data.startswith(b"ID3") or data[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}:
        return ".mp3"
    if data.startswith(b"RIFF"):
        return ".wav"
    if len(data) > 8 and data[4:8] == b"ftyp":
        return ".m4a"
    if data.startswith(b"%PDF"):
        return ".pdf"
    return ".mp3"


def sniff_text_ext(data: bytes, content_type: str) -> str:
    mapped = _ext_from_type(content_type)
    if mapped in {".vtt", ".txt", ".json"}:
        return mapped
    head = data[:300].lstrip()
    if head.startswith(b"WEBVTT"):
        return ".vtt"
    if head.startswith(b"{") or head.startswith(b"["):
        return ".json"
    return ".txt"


def stamp_from(value) -> str:
    parsed = parse_time(value) or datetime.now(TOKYO)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(TOKYO).strftime("%Y%m%d_%H%M%S")


def parse_time(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, UTC)
    text = str(value).strip()
    if text.isdigit():
        return parse_time(int(text))
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def safe_part(value, limit: int = 60) -> str:
    text = INVALID.sub("_", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip(" ._")
    return (text or "unknown")[:limit]


def clip_filename(name: str) -> str:
    if len(name) <= 240:
        return name
    stem, dot, ext = name.rpartition(".")
    extension = f".{ext}" if dot else ""
    return stem[: 240 - len(extension)] + extension


def _call_filename(item: dict, label: str, ext: str) -> str:
    direction = {"inbound": "着信", "outbound": "発信"}.get(item.get("direction") or "", safe_part(item.get("direction"), 12))
    return "{stamp}_{direction}_{caller}_{callee}_{label}_{recording}{ext}".format(
        stamp=stamp_from(item.get("date_time") or item.get("end_time")),
        direction=direction,
        caller=safe_part(item.get("caller_name") or item.get("caller_number"), 24),
        callee=safe_part(item.get("callee_name") or item.get("callee_number"), 24),
        label=label,
        recording=safe_part(item.get("id"), 16),
        ext=ext,
    )


def _recordings(payload: dict) -> list:
    obj = payload.get("object") or {}
    recordings = obj.get("recordings")
    if isinstance(recordings, list):
        return recordings
    if obj.get("download_url") or obj.get("transcript_download_url"):
        return [obj]
    return []


def _has_summary_body(obj: dict) -> bool:
    if isinstance(obj.get("summary_content"), str) and obj["summary_content"].strip():
        return True
    if isinstance(obj.get("summary_overview"), str) and obj["summary_overview"].strip():
        return True
    if obj.get("summary_details") or obj.get("next_steps"):
        return True
    return False


def _merge(base: dict, extra: dict) -> dict:
    merged = dict(base or {})
    for key, value in (extra or {}).items():
        if value not in (None, "", [], {}):
            merged[key] = value
    return merged


def _first_text(obj, keys: tuple[str, ...]) -> str:
    for key in keys:
        found = _find_key(obj, key)
        if isinstance(found, str) and found.strip():
            return found
        if isinstance(found, dict):
            nested = _first_text(found, keys)
            if nested:
                return nested
    return ""


def _find_key(obj, key: str):
    if isinstance(obj, dict):
        if key in obj and obj[key] not in (None, "", []):
            return obj[key]
        for value in obj.values():
            found = _find_key(value, key)
            if found not in (None, "", []):
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_key(item, key)
            if found not in (None, "", []):
                return found
    return None


def _ext_from_type(content_type: str) -> str:
    media = (content_type or "").split(";")[0].strip().lower()
    return {
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/mp4": ".m4a",
        "video/mp4": ".mp4",
        "application/pdf": ".pdf",
        "text/vtt": ".vtt",
        "text/plain": ".txt",
        "application/json": ".json",
    }.get(media, "")


def _batch_result(event: str, messages: list[str]) -> dict:
    if messages and all("保存済み" in message for message in messages):
        return _result(event, "skipped", " / ".join(messages))
    return _result(event, "saved", " / ".join(messages))


def _result(event: str, status: str, detail: str) -> dict:
    return {"event": event, "status": status, "detail": detail}
