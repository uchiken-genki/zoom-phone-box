"""Zoom Phone / Meeting の資料を Box に保存する受信アプリ。"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, abort, jsonify, redirect, render_template, request

import config
import ngrok_public
from box_api import BoxClient
from fax_poll import transfer_received_faxes, watch_received_faxes
from handlers import handle_event
from store import Store
from zoom_api import ZoomClient
from zoom_webhook import validation_response, verify_signature

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger("zoom_phone_box")
EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="zoom-box")
TOKYO = ZoneInfo("Asia/Tokyo")


def create_app(store: Store | None = None, zoom: ZoomClient | None = None, box: BoxClient | None = None, sync: bool = False) -> Flask:
    application = Flask(__name__, template_folder=str(config.BASE_DIR / "templates"))
    application.config["STORE"] = store or Store(config.DATA_DIR)
    application.config["ZOOM"] = zoom or ZoomClient()
    application.config["BOX"] = box or BoxClient(config.DATA_DIR)
    application.config["SYNC"] = sync

    @application.get("/health")
    def health():
        return jsonify({"ok": True})

    @application.get("/")
    def index():
        store_ = _store()
        box_ = _box()
        user = box_.user() if box_.connected() else None
        box_settings = config.read_box_settings(store_.root)
        public = config.read_public_base_url(store_.root)
        ngrok_tunnel = ngrok_public.read_tunnel(store_.root) or {}
        local = public.startswith("http://localhost") or public.startswith("http://127.0.0.1")
        folders = {}
        folder_file = store_.root / "folders.json"
        if folder_file.exists():
            folders = json.loads(folder_file.read_text(encoding="utf-8"))
        return render_template(
            "index.html",
            zoom_ready=_zoom().configured(),
            webhook_secret_ready=bool(config.read_webhook_secret(store_.root)),
            box_configured=box_.configured(),
            box_connected=box_.connected(),
            box_user=user,
            public_base_url=public,
            ngrok_local_port=ngrok_tunnel.get("local_port"),
            webhook_url=f"{public}/zoom/webhook",
            redirect_uri=box_settings["redirect_uri"],
            local_webhook=local,
            folders=folders,
            folder_names=config.FOLDER_NAMES,
            root_name=config.BOX_ROOT_FOLDER_NAME,
            events=config.SUBSCRIBE_EVENTS,
            scopes=config.ZOOM_SCOPES,
            activity=store_.activity(),
            token=_form_token(store_),
            flash=_pop_flash(store_),
        )

    @application.post("/settings/public-url")
    def update_public_url():
        _check_form()
        try:
            saved = config.write_public_base_url(_store().root, request.form.get("public_base_url", ""))
        except ValueError as exc:
            _flash(str(exc), "error")
            return redirect("/")
        _flash(f"公開 URL を更新しました。Zoom には {saved}/zoom/webhook を登録してください。", "ok")
        return redirect("/")

    @application.get("/settings")
    def settings():
        store_ = _store()
        box_settings = config.read_box_settings(store_.root)
        share = config.read_box_share(store_.root)
        collaborators = []
        share_error = ""
        folder_file = store_.root / "folders.json"
        if _box().connected() and folder_file.exists():
            try:
                root_id = json.loads(folder_file.read_text(encoding="utf-8")).get("root")
                if root_id:
                    collaborators = _box().collaborators(str(root_id))
            except Exception as exc:
                LOGGER.exception("collaborator list failed")
                share_error = redact(str(exc))
        return render_template(
            "settings.html",
            share=share,
            share_roles=config.SHARE_ROLES,
            collaborators=collaborators,
            share_error=share_error,
            root_name=config.BOX_ROOT_FOLDER_NAME,
            box=box_settings,
            secret_set=bool(box_settings["client_secret"]),
            webhook_secret_set=bool(config.read_webhook_secret(store_.root)),
            box_configured=_box().configured(),
            box_connected=_box().connected(),
            token=_form_token(store_),
            flash=_pop_flash(store_),
        )

    @application.post("/settings/box")
    def save_box_settings():
        _check_form()
        store_ = _store()
        before = config.read_box_settings(store_.root)
        try:
            saved = config.write_box_settings(
                store_.root,
                request.form.get("client_id", ""),
                request.form.get("client_secret", ""),
                request.form.get("parent_folder_id", ""),
                request.form.get("enterprise_id", ""),
            )
        except ValueError as exc:
            _flash(str(exc), "error")
            return redirect("/settings")
        if (
            saved["client_id"] != before["client_id"]
            or saved["client_secret"] != before["client_secret"]
            or saved["enterprise_id"] != before.get("enterprise_id")
        ):
            _box().disconnect()
        _flash("Box の設定を保存しました。「サービスアカウントで接続」を押してください。", "ok")
        return redirect("/settings")

    @application.post("/settings/box-share")
    def save_box_share():
        _check_form()
        store_ = _store()
        try:
            saved = config.write_box_share(
                store_.root,
                request.form.get("emails", ""),
                request.form.get("role", "viewer"),
            )
            folders = _box().ensure_folders()
            results = _box().invite(folders["root"], saved["emails"], saved["role"])
        except Exception as exc:
            LOGGER.exception("box share failed")
            _flash(redact(str(exc)), "error")
            return redirect("/settings")
        summary = "、".join(f"{item['login']}（{item['result']}）" for item in results)
        _flash(f"「{config.BOX_ROOT_FOLDER_NAME}」フォルダを共有しました: {summary}。各ユーザーの「すべてのファイル」に表示されます。", "ok")
        return redirect("/settings")

    @application.post("/settings/zoom-webhook")
    def save_zoom_webhook():
        _check_form()
        try:
            config.write_webhook_secret(_store().root, request.form.get("webhook_secret", ""))
        except ValueError as exc:
            _flash(str(exc), "error")
            return redirect("/settings")
        _flash("Zoom の Webhook Secret Token を保存しました。", "ok")
        return redirect("/settings")

    @application.post("/box/login")
    def box_login():
        _check_form()
        try:
            user = _box().connect()
        except Exception as exc:
            LOGGER.exception("Box connect failed")
            _flash(redact(str(exc)), "error")
            return redirect("/settings")
        _flash(f"Box のサービスアカウントに接続しました: {user.get('name') or user.get('login')}", "ok")
        return redirect("/")

    @application.get("/box/callback")
    def box_callback():
        error = request.args.get("error_description") or request.args.get("error")
        if error:
            _flash(f"Box の認可が拒否されました: {error}", "error")
            return redirect("/")
        try:
            _box().consume_state(request.args.get("state", ""))
            user = _box().exchange_code(request.args.get("code", ""))
        except Exception as exc:
            LOGGER.exception("Box OAuth failed")
            _flash(redact(str(exc)), "error")
            return redirect("/")
        _flash(f"Box に接続しました: {user.get('name') or user.get('login')}", "ok")
        return redirect("/")

    @application.post("/box/disconnect")
    def box_disconnect():
        _check_form()
        _box().disconnect()
        _flash("Box の接続を解除しました", "ok")
        return redirect("/")

    @application.post("/box/folders")
    def create_folders():
        _check_form()
        try:
            folders = _box().ensure_folders()
        except Exception as exc:
            LOGGER.exception("folder create failed")
            _flash(redact(str(exc)), "error")
            return redirect("/")
        _flash(f"フォルダを確認しました。社内のユーザーは {folders.get('shared_url')} から開けます。", "ok")
        return redirect("/")

    @application.post("/zoom/check")
    def zoom_check():
        _check_form()
        try:
            _zoom().access_token(force=True)
        except Exception as exc:
            LOGGER.exception("zoom check failed")
            _flash(redact(str(exc)), "error")
            return redirect("/")
        _flash("Zoom のトークンを取得できました", "ok")
        return redirect("/")

    @application.post("/box/check")
    def box_check():
        _check_form()
        try:
            user = _box().current_user()
        except Exception as exc:
            LOGGER.exception("box check failed")
            _flash(redact(str(exc)), "error")
            return redirect("/")
        _flash(f"Box に接続できています: {user.get('name')} ({user.get('login')})", "ok")
        return redirect("/")

    @application.post("/retry")
    def retry():
        _check_form()
        files = list(_store().inbox.glob("*.json"))
        for path in files:
            path.with_name(path.name + ".error").unlink(missing_ok=True)
            _enqueue_existing(path)
        _flash(f"未処理 {len(files)} 件を再実行しました" if files else "再実行するイベントはありません", "ok")
        return redirect("/")

    @application.post("/zoom/webhook")
    def webhook():
        raw = request.get_data() or b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            return jsonify({"message": "invalid json"}), 400
        if not isinstance(body, dict):
            return jsonify({"message": "invalid json"}), 400

        secret = config.read_webhook_secret(_store().root)
        if not secret:
            return jsonify({"message": "ZOOM_WEBHOOK_SECRET_TOKEN が未設定です"}), 500

        timestamp = request.headers.get("x-zm-request-timestamp", "")
        signature = request.headers.get("x-zm-signature", "")
        if not verify_signature(raw, timestamp, signature, secret):
            _note_receipt("署名不一致", "Zoom からの通知を拒否しました")
            return jsonify({"message": "invalid signature"}), 401

        if body.get("event") == "endpoint.url_validation":
            _note_receipt("endpoint.url_validation", "URL の検証に応答しました")
            plain = ((body.get("payload") or {}).get("plainToken")) or ""
            return jsonify(validation_response(plain, secret))

        _note_receipt(str(body.get("event") or ""), _receipt_detail(body))
        _accept(raw, body)
        return "", 200

    @application.get("/check")
    def check_page():
        store_ = _store()
        public = config.read_public_base_url(store_.root)
        error = ""
        try:
            logs = _zoom().list_fax_logs()
        except Exception as exc:
            LOGGER.exception("fax log check failed")
            logs = []
            error = redact(str(exc))
        rows = []
        for item in sorted(logs, key=lambda row: str(row.get("created_at") or ""), reverse=True):
            log_id = str(item.get("fax_log_id") or "")
            file_id = str(item.get("file_id") or "")
            direction = str(item.get("direction") or "")
            status = str(item.get("status") or "")
            rows.append(
                {
                    "time": _tokyo(item.get("created_at")),
                    "direction": "受信" if direction == "inbound" else ("送信" if direction == "outbound" else direction),
                    "status": status,
                    "sender": item.get("sender_number") or item.get("sender_name") or "",
                    "receiver": item.get("receiver_number") or item.get("receiver_name") or "",
                    "fax_log_id": log_id,
                    "saved": store_.is_done(f"fax:{log_id}:{file_id}"),
                    "can_save": direction == "inbound" and status == "received" and bool(log_id and file_id),
                }
            )
        return render_template(
            "check.html",
            webhook_url=f"{public}/zoom/webhook",
            rows=rows,
            receipts=store_.receipts(),
            poll=config.read_fax_poll(store_.root),
            error=error,
            token=_form_token(store_),
            flash=_pop_flash(store_),
        )

    @application.post("/check/refresh")
    def check_refresh():
        _check_form()
        result = transfer_received_faxes(_zoom(), _box(), _store())
        _flash(result["message"], result["level"])
        return redirect("/check")

    @application.post("/check/interval")
    def check_interval():
        _check_form()
        try:
            seconds = config.write_fax_poll_interval(_store().root, request.form.get("interval", ""))
        except ValueError as exc:
            _flash(str(exc), "error")
            return redirect("/check")
        _flash(f"転送間隔を {seconds} 秒に変更しました。次の確認から反映されます。", "ok")
        return redirect("/check")

    @application.post("/check/save")
    def check_save():
        _check_form()
        fax_log_id = (request.form.get("fax_log_id") or "").strip()
        try:
            logs = _zoom().list_fax_logs()
            item = next((row for row in logs if str(row.get("fax_log_id") or "") == fax_log_id), None)
            if not item:
                raise RuntimeError("指定した FAX が Zoom の一覧にありません")
            result = handle_event(
                {"event": "phone.fax_received", "payload": {"object": item}},
                _zoom(),
                _box(),
                _store(),
            )
        except Exception as exc:
            LOGGER.exception("fax save from check failed")
            _flash(redact(str(exc)), "error")
            return redirect("/check")
        level = "ok" if result.get("status") == "saved" else "warn"
        _flash(result.get("detail") or "FAX を確認しました", level)
        return redirect("/check")

    return application


def _note_receipt(event: str, detail: str) -> None:
    _store().note_receipt(
        {
            "time": datetime.now(TOKYO).strftime("%Y-%m-%d %H:%M:%S"),
            "event": event,
            "detail": detail,
        }
    )


def _receipt_detail(body: dict) -> str:
    obj = ((body.get("payload") or {}).get("object") or {}) if isinstance(body.get("payload"), dict) else {}
    if not isinstance(obj, dict):
        return ""
    fax_log_id = obj.get("fax_log_id") or ""
    if fax_log_id:
        return f"fax_log_id={fax_log_id}"
    return ""


def _tokyo(value) -> str:
    text = str(value or "")
    if not text:
        return ""
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    return moment.astimezone(TOKYO).strftime("%Y-%m-%d %H:%M")


def _accept(raw: bytes, body: dict) -> None:
    store = _store()
    digest = hashlib.sha256(raw).hexdigest()[:20]
    path = store.inbox / f"{int(time.time() * 1000)}_{digest}.json"
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    _enqueue_existing(path)


def _enqueue_existing(path: Path) -> None:
    application = current_app()
    store = application.config["STORE"]
    zoom = application.config["ZOOM"]
    box = application.config["BOX"]
    if application.config.get("SYNC"):
        _process(path, store, zoom, box)
        return
    EXECUTOR.submit(_process, path, store, zoom, box)


def _process(path: Path, store: Store, zoom, box) -> None:
    event_name = ""
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
        event_name = body.get("event") or ""
        result = handle_event(body, zoom, box, store)
        path.unlink(missing_ok=True)
        path.with_name(path.name + ".error").unlink(missing_ok=True)
        _log(store, result)
        LOGGER.info("%s %s", result.get("status"), result.get("detail"))
    except Exception as exc:
        message = redact(str(exc))
        LOGGER.exception("event processing failed")
        path.with_name(path.name + ".error").write_text(message, encoding="utf-8")
        _log(store, {"event": event_name, "status": "error", "detail": message})


def _log(store: Store, result: dict) -> None:
    store.log(
        {
            "time": datetime.now(TOKYO).strftime("%Y-%m-%d %H:%M:%S"),
            "event": result.get("event") or "",
            "status": result.get("status") or "",
            "detail": result.get("detail") or "",
        }
    )


def redact(text: str) -> str:
    text = re.sub(r"access_token=[^&\s\"']+", "access_token=REDACTED", text)
    text = re.sub(r"Bearer\s+\S+", "Bearer REDACTED", text)
    return text[:1000]


def _form_token(store: Store) -> str:
    path = store.root / "ui_token"
    if not path.exists():
        path.write_text(secrets.token_urlsafe(24), encoding="utf-8")
    return path.read_text(encoding="utf-8").strip()


def _check_form() -> None:
    supplied = request.form.get("token", "")
    expected = _form_token(_store())
    if not supplied or not hmac.compare_digest(supplied, expected):
        abort(400)


def _flash(message: str, level: str) -> None:
    (_store().root / "flash.json").write_text(
        json.dumps({"message": message, "level": level}, ensure_ascii=False),
        encoding="utf-8",
    )


def _pop_flash(store: Store):
    path = store.root / "flash.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    path.unlink(missing_ok=True)
    return data


def current_app() -> Flask:
    from flask import current_app as flask_current

    return flask_current


def _store() -> Store:
    return current_app().config["STORE"]


def _zoom() -> ZoomClient:
    return current_app().config["ZOOM"]


def _box() -> BoxClient:
    return current_app().config["BOX"]


if __name__ == "__main__":
    store = Store(config.DATA_DIR)
    store.clear_inflight()
    zoom = ZoomClient()
    box = BoxClient(config.DATA_DIR)
    application = create_app(store=store, zoom=zoom, box=box)
    for path in sorted(store.inbox.glob("*.json")):
        if not path.with_name(path.name + ".error").exists():
            EXECUTOR.submit(_process, path, store, zoom, box)
    adopted = ngrok_public.sync_public_url(store.root, config.APP_PORT)
    if adopted:
        LOGGER.info("ngrok の公開 URL を反映しました: %s （転送先 localhost:%s）", adopted, config.APP_PORT)
    threading.Thread(
        target=ngrok_public.watch_public_url,
        args=(store.root, config.APP_PORT),
        name="ngrok-sync",
        daemon=True,
    ).start()
    threading.Thread(
        target=watch_received_faxes,
        args=(zoom, box, store),
        name="fax-poll",
        daemon=True,
    ).start()
    LOGGER.info("listening on http://%s:%s", config.APP_HOST, config.APP_PORT)
    LOGGER.info("webhook %s/zoom/webhook", config.read_public_base_url(store.root))
    application.run(host=config.APP_HOST, port=config.APP_PORT, debug=False, use_reloader=False)
