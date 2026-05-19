from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


def utcish_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class HistoryStore:
    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self.sessions_dir = self.base_dir / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.sessions_dir / "index.json"
        self.active_session_id = ""
        self.index = self._load_index()

    def _load_index(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return {"version": 1, "active_session_id": "", "sessions": {}}
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("version", 1)
                data.setdefault("active_session_id", "")
                data.setdefault("sessions", {})
                self.active_session_id = str(data.get("active_session_id") or "")
                return data
        except Exception:
            pass
        return {"version": 1, "active_session_id": "", "sessions": {}}

    def _save_index(self) -> None:
        temp = self.index_path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(self.index, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, self.index_path)

    def session_path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{session_id}.jsonl"

    def ensure_session(self, session_mode: str = "persistent_thread") -> str:
        if self.active_session_id and self.session_path(self.active_session_id).exists():
            return self.active_session_id
        return self.new_session(session_mode=session_mode)

    def new_session(self, session_mode: str = "persistent_thread") -> str:
        session_id = str(uuid.uuid4())
        self.active_session_id = session_id
        self.index["active_session_id"] = session_id
        self.index.setdefault("sessions", {})[session_id] = {
            "id": session_id,
            "name": "New Gemini Agent Session",
            "created_at": utcish_now(),
            "updated_at": utcish_now(),
            "runtime_mode": "gemini_first",
            "session_mode": session_mode,
            "gemini_url": "",
            "canvas_active": False,
            "tool_contract_seeded": False,
            "tool_contract_seed_in_progress": False,
        }
        self.session_path(session_id).write_text("", encoding="utf-8")
        self._save_index()
        return session_id

    def metadata(self, session_id: str | None = None) -> dict[str, Any]:
        sid = session_id or self.active_session_id
        return dict(self.index.setdefault("sessions", {}).get(sid, {}))

    def update_metadata(self, session_id: str | None = None, **updates: Any) -> None:
        sid = session_id or self.active_session_id
        if not sid:
            return
        sessions = self.index.setdefault("sessions", {})
        item = sessions.setdefault(sid, {"id": sid, "created_at": utcish_now()})
        item.update(updates)
        item["updated_at"] = utcish_now()
        self._save_index()

    def add_event(self, kind: str, payload: dict[str, Any], session_id: str | None = None) -> None:
        sid = session_id or self.active_session_id
        if not sid:
            sid = self.new_session()
        event = {
            "ts": utcish_now(),
            "session_id": sid,
            "kind": kind,
            "payload": payload,
        }
        with self.session_path(sid).open("a", encoding="utf-8", errors="replace") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.update_metadata(sid)

    def events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        sid = session_id or self.active_session_id
        path = self.session_path(sid)
        if not path.exists():
            return []
        items: list[dict[str, Any]] = []
        # Bolt Optimization: Read line-by-line to prevent loading large JSONL logs
        # into memory as a single string and duplicating them into a massive list.
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if isinstance(item, dict):
                    items.append(item)
        return items
