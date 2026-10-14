import hashlib
import hmac
import json
import tempfile
import time
import unittest
from pathlib import Path

from box_api import conflict_id
from handlers import (
    handle_event,
    render_meeting_summary,
    render_phone_summary,
    sniff_text_ext,
    stamp_from,
)
from store import Store
from zoom_webhook import validation_response, verify_signature


class FakeZoom:
    def __init__(self):
        self.downloads = {}
        self.summary = {"summary": "来週訪問する。", "next_steps": ["見積を送る"], "call_id": "call-1"}

    def configured(self):
        return True

    def download(self, url):
        return self.downloads[url]

    def ai_call_summary(self, user_id, summary_id):
        return self.summary

    def meeting_summary(self, meeting_uuid):
        raise AssertionError("本文があるときは API を呼ばない")

    def fax_file(self, fax_log_id, file_id):
        return b"%PDF-1.4 fax", "application/pdf"

    def list_fax_logs(self, page_size=30):
        return []


class FakeBox:
    def __init__(self):
        self.uploads = []

    def configured(self):
        return False

    def connected(self):
        return False

    def user(self):
        return None

    def upload(self, kind, filename, content, mime):
        self.uploads.append({"kind": kind, "filename": filename, "content": content, "mime": mime})
        return {"id": "box-file-1", "name": filename}


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.zoom = FakeZoom()
        self.box = FakeBox()

    def tearDown(self):
        self.tmp.cleanup()

    def test_validation_token_matches_zoom_hmac(self):
        secret = "webhook-secret"
        body = validation_response("plain-token", secret)
        self.assertEqual(body["plainToken"], "plain-token")
        expected = hmac.new(secret.encode(), b"plain-token", hashlib.sha256).hexdigest()
        self.assertEqual(body["encryptedToken"], expected)

    def test_signature_rejects_expired_timestamp(self):
        secret = "webhook-secret"
        raw = b'{"event":"phone.recording_completed"}'
        timestamp = "1000"
        signature = "v0=" + hmac.new(secret.encode(), f"v0:{timestamp}:".encode() + raw, hashlib.sha256).hexdigest()
        self.assertFalse(verify_signature(raw, timestamp, signature, secret, now=2000))
        self.assertTrue(verify_signature(raw, timestamp, signature, secret, now=1100))

    def test_recording_is_saved_under_recordings(self):
        self.zoom.downloads["https://zoom.example/rec"] = (b"ID3audio", "audio/mpeg")
        result = handle_event(
            {
                "event": "phone.recording_completed",
                "payload": {
                    "object": {
                        "recordings": [
                            {
                                "id": "rec1",
                                "caller_name": "山田",
                                "callee_number": "+81300000000",
                                "direction": "inbound",
                                "date_time": "2026-10-05T01:02:03Z",
                                "download_url": "https://zoom.example/rec",
                            }
                        ]
                    }
                },
            },
            self.zoom,
            self.box,
            self.store,
        )
        self.assertEqual(result["status"], "saved")
        saved = self.box.uploads[0]
        self.assertEqual(saved["kind"], "recordings")
        self.assertTrue(saved["filename"].endswith("_録音_rec1.mp3"))
        self.assertIn("着信", saved["filename"])
        again = handle_event(
            {
                "event": "phone.recording_completed",
                "payload": {"object": {"recordings": [{"id": "rec1", "download_url": "https://zoom.example/rec"}]}},
            },
            self.zoom,
            self.box,
            self.store,
        )
        self.assertEqual(again["status"], "skipped")
        self.assertEqual(len(self.box.uploads), 1)

    def test_transcript_detects_vtt(self):
        self.zoom.downloads["https://zoom.example/vtt"] = (b"WEBVTT\n\n00:00.000 --> 00:01.000\nhello", "application/octet-stream")
        result = handle_event(
            {
                "event": "phone.recording_transcript_completed",
                "payload": {
                    "object": {
                        "recordings": [
                            {
                                "id": "rec2",
                                "date_time": "2026-10-05T01:02:03Z",
                                "transcript_download_url": "https://zoom.example/vtt",
                            }
                        ]
                    }
                },
            },
            self.zoom,
            self.box,
            self.store,
        )
        self.assertEqual(result["status"], "saved")
        self.assertEqual(self.box.uploads[0]["kind"], "transcripts")
        self.assertTrue(self.box.uploads[0]["filename"].endswith(".vtt"))
        self.assertEqual(sniff_text_ext(b"{}\n", "application/octet-stream"), ".json")

    def test_phone_summary_and_deleted_notice(self):
        result = handle_event(
            {
                "event": "phone.ai_call_summary_changed",
                "payload": {
                    "object": {
                        "ai_call_summary_id": "sum1",
                        "user_id": "user1",
                        "call_id": "call-1",
                        "created_time": "2026-10-05T01:02:03Z",
                        "modified_time": "2026-10-05T01:05:00Z",
                    }
                },
            },
            self.zoom,
            self.box,
            self.store,
        )
        self.assertEqual(self.box.uploads[0]["kind"], "summaries")
        text = self.box.uploads[0]["content"].decode("utf-8")
        self.assertIn("来週訪問する。", text)
        self.assertIn("見積を送る", text)
        self.assertIn("電話", self.box.uploads[0]["filename"])

        deleted = handle_event(
            {
                "event": "phone.ai_call_summary_changed",
                "payload": {"object": {"ai_call_summary_id": "sum1", "user_id": "user1", "deleted": True, "modified_time": "2026-10-05T02:00:00Z"}},
            },
            self.zoom,
            self.box,
            self.store,
        )
        self.assertEqual(deleted["status"], "skipped")
        self.assertEqual(len(self.box.uploads), 1)
        self.assertEqual(result["status"], "saved")

    def test_meeting_summary_uses_webhook_body(self):
        result = handle_event(
            {
                "event": "meeting.summary_completed",
                "payload": {
                    "object": {
                        "meeting_uuid": "uuid-1",
                        "meeting_id": 123,
                        "meeting_topic": "週次会議",
                        "meeting_host_email": "host@example.com",
                        "meeting_start_time": "2026-10-05T01:00:00Z",
                        "summary_title": "週次の決定",
                        "summary_created_time": "2026-10-05T02:00:00Z",
                        "summary_content": "## 決定\n\n来月リリースする。",
                        "summary_doc_url": "https://docs.zoom.us/doc/1",
                    }
                },
            },
            self.zoom,
            self.box,
            self.store,
        )
        self.assertEqual(result["status"], "saved")
        text = self.box.uploads[0]["content"].decode("utf-8")
        self.assertIn("来月リリースする。", text)
        self.assertIn("https://docs.zoom.us/doc/1", text)
        self.assertIn("週次会議", self.box.uploads[0]["filename"])

    def test_fax_pdf_goes_to_fax_folder(self):
        result = handle_event(
            {
                "event": "phone.fax_received",
                "payload": {
                    "object": {
                        "fax_log_id": "faxlog1",
                        "file_id": "file1",
                        "status": "received",
                        "sender_number": "+81311112222",
                        "receiver_number": "+81333334444",
                        "created_at": "2026-10-05T03:04:05Z",
                    }
                },
            },
            self.zoom,
            self.box,
            self.store,
        )
        self.assertEqual(result["status"], "saved")
        self.assertEqual(self.box.uploads[0]["kind"], "fax")
        self.assertTrue(self.box.uploads[0]["filename"].endswith(".pdf"))
        self.assertTrue(self.box.uploads[0]["content"].startswith(b"%PDF"))

    def test_failed_fax_is_not_uploaded(self):
        result = handle_event(
            {
                "event": "phone.fax_received",
                "payload": {"object": {"fax_log_id": "faxlog2", "file_id": "file2", "status": "failed"}},
            },
            self.zoom,
            self.box,
            self.store,
        )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(self.box.uploads, [])

    def test_stamp_uses_japan_time(self):
        self.assertEqual(stamp_from("2026-10-05T01:02:03Z"), "20261005_100203")

    def test_conflict_id_accepts_object_and_list(self):
        self.assertEqual(conflict_id({"context_info": {"conflicts": {"id": "10"}}}), "10")
        self.assertEqual(conflict_id({"context_info": {"conflicts": [{"id": "11"}]}}), "11")

    def test_phone_summary_renderer_reads_nested_summary(self):
        text = render_phone_summary({"call_id": "c", "call_summary": {"summary": "本文", "next_steps": ["次"]}})
        self.assertIn("本文", text)
        self.assertIn("次", text)
        meeting = render_meeting_summary(
            {"meeting_topic": "企画", "summary_overview": "概要", "summary_details": [{"label": "論点", "summary": "詳細"}], "next_steps": ["確認"]}
        )
        self.assertIn("概要", meeting)
        self.assertIn("詳細", meeting)
        self.assertIn("確認", meeting)

    def test_public_url_change_updates_webhook_without_restart(self):
        from config import normalize_public_base_url
        from app import create_app

        self.assertEqual(
            normalize_public_base_url("https://abc.ngrok-free.app/zoom/webhook"),
            "https://abc.ngrok-free.app",
        )
        self.assertEqual(normalize_public_base_url("xyz.ngrok-free.app"), "https://xyz.ngrok-free.app")
        with self.assertRaises(ValueError):
            normalize_public_base_url("https://abc.ngrok-free.app/other")

        app = create_app(store=self.store, zoom=self.zoom, box=self.box, sync=True)
        client = app.test_client()
        home = client.get("/")
        token = home.data.split(b'name="token" value="', 1)[1].split(b'"', 1)[0].decode()
        updated = client.post(
            "/settings/public-url",
            data={"token": token, "public_base_url": "https://new.ngrok-free.app/"},
            follow_redirects=True,
        )
        self.assertEqual(updated.status_code, 200)
        self.assertIn(b"https://new.ngrok-free.app/zoom/webhook", updated.data)
        self.assertIn("公開 URL を更新しました".encode(), updated.data)

    def test_ngrok_tunnel_for_app_port_is_saved(self):
        from ngrok_public import apply_tunnel, choose_tunnel

        tunnels = [
            {"proto": "https", "public_url": "https://wrong.ngrok-free.app", "config": {"addr": "http://localhost:5000"}},
            {"proto": "http", "public_url": "http://abc.ngrok-free.app", "config": {"addr": "http://localhost:8787"}},
            {"proto": "https", "public_url": "https://abc.ngrok-free.app", "config": {"addr": "http://localhost:8787"}},
        ]
        chosen = choose_tunnel(tunnels, 8787)
        self.assertEqual(chosen["public_url"], "https://abc.ngrok-free.app")
        self.assertIsNone(choose_tunnel(tunnels, 9))

        saved = apply_tunnel(self.store.root, chosen, 8787)
        self.assertEqual(saved, "https://abc.ngrok-free.app")
        self.assertEqual((self.store.root / "public_base_url.txt").read_text(encoding="utf-8").strip(), saved)
        meta = json.loads((self.store.root / "ngrok.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["local_port"], 8787)
        self.assertEqual(meta["public_port"], 443)
        self.assertEqual(meta["public_url"], saved)

    def test_settings_page_saves_box_without_showing_secret(self):
        import config
        from app import create_app
        from box_api import BoxClient

        box = BoxClient(self.store.root)
        config.write_box_settings(
            self.store.root,
            "box-client",
            "super-secret-value",
            "0",
            "998877",
        )
        config.write_webhook_secret(self.store.root, "zoom-webhook-secret-value")
        self.assertTrue(box.configured())

        app = create_app(store=self.store, zoom=self.zoom, box=box, sync=True)
        client = app.test_client()
        page = client.get("/settings")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Client Secret".encode(), page.data)
        self.assertIn(b"box-client", page.data)
        self.assertIn(b"998877", page.data)
        self.assertNotIn(b"super-secret-value", page.data)
        self.assertNotIn(b"zoom-webhook-secret-value", page.data)
        self.assertIn("Box の共有ユーザー".encode(), page.data)

        token = page.data.split(b'name="token" value="', 1)[1].split(b'"', 1)[0].decode()
        saved = client.post(
            "/settings/box",
            data={
                "token": token,
                "client_id": "box-client-2",
                "client_secret": "",
                "enterprise_id": "998877",
                "parent_folder_id": "0",
            },
            follow_redirects=True,
        )
        self.assertEqual(saved.status_code, 200)
        self.assertIn("Box の設定を保存しました".encode(), saved.data)
        stored = config.read_box_settings(self.store.root)
        self.assertEqual(stored["client_id"], "box-client-2")
        self.assertEqual(stored["client_secret"], "super-secret-value")
        self.assertNotIn(b"super-secret-value", saved.data)

    def test_box_share_invites_new_users_and_keeps_existing(self):
        import config
        from box_api import BoxClient

        saved = config.write_box_share(self.store.root, "Taro@Example.co.jp\n hanako@example.co.jp, taro@example.co.jp", "viewer")
        self.assertEqual(saved["emails"], ["taro@example.co.jp", "hanako@example.co.jp"])
        with self.assertRaises(ValueError):
            config.write_box_share(self.store.root, "not-an-email", "viewer")

        calls = []

        class Response:
            def __init__(self, status, body):
                self.status_code = status
                self._body = body
                self.text = json.dumps(body)

            def json(self):
                return self._body

        def fake_request(method, url, **kwargs):
            calls.append((method, url, kwargs.get("json")))
            if method == "GET":
                return Response(200, {"entries": [{"id": "c1", "role": "viewer", "status": "accepted", "accessible_by": {"login": "taro@example.co.jp", "name": "Taro"}}]})
            return Response(201, {"status": "accepted"})

        box = BoxClient(self.store.root)
        box._request = fake_request
        results = box.invite("100", saved["emails"], "viewer")
        self.assertEqual([item["result"] for item in results], ["共有済み", "招待"])
        posts = [call for call in calls if call[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2]["accessible_by"], {"type": "user", "login": "hanako@example.co.jp"})
        self.assertEqual(posts[0][2]["item"], {"type": "folder", "id": "100"})

    def test_fax_poll_saves_inbound_once_and_keeps_interval(self):
        import config
        from fax_poll import transfer_received_faxes

        self.assertEqual(config.read_fax_poll(self.store.root)["interval"], 30)
        self.assertEqual(config.write_fax_poll_interval(self.store.root, "45"), 45)
        with self.assertRaises(ValueError):
            config.write_fax_poll_interval(self.store.root, "5")

        self.zoom.list_fax_logs = lambda page_size=30: [
            {"direction": "outbound", "status": "sent", "fax_log_id": "out", "file_id": "f-out", "created_at": "2026-10-12T07:52:04Z"},
            {
                "direction": "inbound",
                "status": "received",
                "fax_log_id": "in",
                "file_id": "f-in",
                "created_at": "2026-10-12T07:52:22Z",
                "sender_number": "1",
                "receiver_number": "2",
            },
        ]
        first = transfer_received_faxes(self.zoom, self.box, self.store)
        self.assertEqual(first["saved"], 1)
        self.assertEqual(len(self.box.uploads), 1)
        self.assertEqual(self.box.uploads[0]["kind"], "fax")
        second = transfer_received_faxes(self.zoom, self.box, self.store)
        self.assertEqual(second["saved"], 0)
        self.assertEqual(len(self.box.uploads), 1)
        self.assertEqual(config.read_fax_poll(self.store.root)["interval"], 45)
        self.assertIn("未転送 FAX はありません", config.read_fax_poll(self.store.root)["last_message"])

    def test_home_and_webhook_validation(self):
        import config
        from app import create_app

        original = config.ZOOM_WEBHOOK_SECRET_TOKEN
        config.ZOOM_WEBHOOK_SECRET_TOKEN = "webhook-secret"
        try:
            app = create_app(store=self.store, zoom=self.zoom, box=self.box, sync=True)
            client = app.test_client()
            home = client.get("/")
            self.assertEqual(home.status_code, 200)
            self.assertIn("Zoom Phone".encode(), home.data)
            self.assertIn("録音".encode(), home.data)
            self.assertIn("Zoom 側で行う設定".encode(), home.data)
            self.assertIn("Box 側で行う設定".encode(), home.data)
            self.assertIn(b"phone:read:fax_log:admin", home.data)
            self.assertIn(b"phone.fax_received", home.data)

            check = client.get("/check")
            self.assertEqual(check.status_code, 200)
            self.assertIn("FAX の確認".encode(), check.data)
            self.assertIn("更新".encode(), check.data)
            self.assertIn("Webhook を受け取っていません".encode(), check.data)

            raw = json.dumps({"event": "endpoint.url_validation", "payload": {"plainToken": "abc"}}).encode()
            timestamp = str(int(time.time()))
            signature = "v0=" + hmac.new(
                b"webhook-secret",
                f"v0:{timestamp}:".encode() + raw,
                hashlib.sha256,
            ).hexdigest()
            response = client.post(
                "/zoom/webhook",
                data=raw,
                headers={
                    "Content-Type": "application/json",
                    "x-zm-request-timestamp": timestamp,
                    "x-zm-signature": signature,
                },
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["plainToken"], "abc")
            self.assertEqual(response.get_json()["encryptedToken"], validation_response("abc", "webhook-secret")["encryptedToken"])

            saved = client.post(
                "/zoom/webhook",
                data=raw,
                headers={"Content-Type": "application/json", "x-zm-request-timestamp": timestamp, "x-zm-signature": "v0=bad"},
            )
            self.assertEqual(saved.status_code, 401)
        finally:
            config.ZOOM_WEBHOOK_SECRET_TOKEN = original


if __name__ == "__main__":
    unittest.main()
