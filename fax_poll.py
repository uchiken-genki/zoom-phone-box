"""受信済み FAX を Zoom から定期的に取得して Box へ保存する。"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import config
from handlers import handle_event

LOGGER = logging.getLogger("zoom_phone_box")
TOKYO = ZoneInfo("Asia/Tokyo")
_LOCK = threading.Lock()


def transfer_received_faxes(zoom, box, store) -> dict:
    if not _LOCK.acquire(blocking=False):
        return {"saved": 0, "errors": 0, "level": "warn", "message": "転送処理が実行中です"}
    try:
        when = datetime.now(TOKYO).strftime("%Y-%m-%d %H:%M:%S")
        try:
            logs = zoom.list_fax_logs(page_size=100)
        except Exception as exc:
            message = f"FAX 一覧の取得に失敗しました: {_redact(str(exc))}"
            config.write_fax_poll_result(store.root, when, message)
            LOGGER.exception("fax poll list failed")
            return {"saved": 0, "errors": 1, "level": "error", "message": message}

        saved = 0
        errors = []
        for item in logs:
            if not _receivable(item):
                continue
            key = "fax:{log}:{file}".format(log=item.get("fax_log_id"), file=item.get("file_id"))
            if store.is_done(key):
                continue
            try:
                result = handle_event(
                    {"event": "phone.fax_received", "payload": {"object": item}},
                    zoom,
                    box,
                    store,
                )
            except Exception as exc:
                errors.append(_redact(str(exc)))
                LOGGER.exception("fax poll save failed")
                continue
            if result.get("status") == "saved":
                saved += 1
                store.log(
                    {
                        "time": when,
                        "event": "phone.fax_received",
                        "status": "saved",
                        "detail": result.get("detail") or "",
                    }
                )

        if errors and saved:
            message = f"{saved} 件を Box へ転送しました。{len(errors)} 件は失敗しました: {errors[0]}"
            level = "warn"
        elif errors:
            message = f"転送に失敗しました: {errors[0]}"
            level = "error"
        elif saved:
            message = f"{saved} 件を Box へ転送しました"
            level = "ok"
        else:
            message = "受信済みの未転送 FAX はありません"
            level = "ok"
        config.write_fax_poll_result(store.root, when, message)
        LOGGER.info("fax poll: %s", message)
        return {"saved": saved, "errors": len(errors), "level": level, "message": message}
    finally:
        _LOCK.release()


def watch_received_faxes(zoom, box, store) -> None:
    last = 0.0
    while True:
        interval = config.read_fax_poll(store.root)["interval"]
        if time.monotonic() - last >= interval:
            last = time.monotonic()
            transfer_received_faxes(zoom, box, store)
        time.sleep(1)


def _receivable(item: dict) -> bool:
    status = str(item.get("status") or "").lower()
    return (
        item.get("direction") == "inbound"
        and status == "received"
        and bool(item.get("fax_log_id"))
        and bool(item.get("file_id"))
    )


def _redact(text: str) -> str:
    text = re.sub(r"access_token=[^&\s\"']+", "access_token=REDACTED", text)
    text = re.sub(r"Bearer\s+\S+", "Bearer REDACTED", text)
    return text[:1000]
