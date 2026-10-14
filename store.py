"""処理済みキー、活動ログ、未処理 Webhook の保存。"""

from __future__ import annotations

import json
import threading
from pathlib import Path


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "state.json"
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if not self.state_path.exists():
            return {"done": [], "inflight": [], "activity": []}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _save(self, state: dict) -> None:
        self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def claim(self, key: str) -> bool:
        with self._lock:
            state = self._load()
            done = set(state.get("done", []))
            inflight = set(state.get("inflight", []))
            if key in done or key in inflight:
                return False
            inflight.add(key)
            state["inflight"] = list(inflight)
            self._save(state)
            return True

    def finish(self, key: str) -> None:
        with self._lock:
            state = self._load()
            inflight = [item for item in state.get("inflight", []) if item != key]
            done = state.get("done", [])
            if key not in done:
                done.append(key)
            state["inflight"] = inflight
            state["done"] = done[-5000:]
            self._save(state)

    def release(self, key: str) -> None:
        with self._lock:
            state = self._load()
            state["inflight"] = [item for item in state.get("inflight", []) if item != key]
            self._save(state)

    def clear_inflight(self) -> None:
        with self._lock:
            state = self._load()
            state["inflight"] = []
            self._save(state)

    def is_done(self, key: str) -> bool:
        with self._lock:
            return key in set(self._load().get("done", []))

    def note_receipt(self, entry: dict) -> None:
        with self._lock:
            state = self._load()
            items = state.setdefault("receipts", [])
            items.insert(0, entry)
            state["receipts"] = items[:50]
            self._save(state)

    def receipts(self) -> list:
        with self._lock:
            return list(self._load().get("receipts", []))

    def log(self, entry: dict) -> None:
        with self._lock:
            state = self._load()
            activity = state.setdefault("activity", [])
            activity.insert(0, entry)
            state["activity"] = activity[:200]
            self._save(state)

    def activity(self) -> list:
        with self._lock:
            return list(self._load().get("activity", []))
