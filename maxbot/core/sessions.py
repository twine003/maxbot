"""Per-chat session state, namespaced by model (claude / codex / …).

Persists to <workspace>/sessions.json. File layout:

    {
        "<chat_id>": {
            "active_model": "claude",
            "models": {
                "claude": {"session_id": "...", "created_at": "...", "initialized": true},
                "codex":  {"session_id": null,  "created_at": "...", "initialized": false}
            }
        }
    }
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


class SessionStore:
    def __init__(self, path: Path, default_model: str, supported_models: Iterable[str]):
        self.path = path
        self.default_model = default_model
        self.supported_models = set(supported_models)

    # ------- low-level IO -------
    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        normalized: dict[str, dict] = {}
        changed = False
        for key, entry in raw.items():
            norm = self._normalize(str(key), entry)
            normalized[str(key)] = norm
            if norm != entry:
                changed = True
        if changed:
            self._save(normalized)
        return normalized

    def _save(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _new_model_session(self, model: str) -> dict:
        session_id = str(uuid.uuid4()) if model == "claude" else None
        return {
            "session_id": session_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "initialized": False,
        }

    def _normalize(self, key: str, entry: dict | None) -> dict:
        if not isinstance(entry, dict):
            entry = {}
        if "models" in entry:
            entry.setdefault("active_model", self.default_model)
            return entry
        if "session_id" in entry:  # legacy single-model layout
            return {
                "active_model": self.default_model,
                "models": {
                    "claude": {
                        "session_id": entry.get("session_id"),
                        "created_at": entry.get("created_at", datetime.now(timezone.utc).isoformat()),
                        "initialized": entry.get("initialized", False),
                    }
                },
            }
        return {"active_model": self.default_model, "models": {}}

    def _ensure_owner(self, data: dict, owner_key: str) -> dict:
        if owner_key not in data:
            data[owner_key] = self._normalize(owner_key, {})
        else:
            data[owner_key] = self._normalize(owner_key, data[owner_key])
        return data[owner_key]

    # ------- public API -------
    def get_active_model(self, chat_id: int) -> str:
        sessions = self._load()
        entry = self._ensure_owner(sessions, str(chat_id))
        model = entry.get("active_model", self.default_model)
        if model not in self.supported_models:
            model = self.default_model
            entry["active_model"] = model
            self._save(sessions)
        return model

    def set_active_model(self, chat_id: int, model: str) -> None:
        sessions = self._load()
        entry = self._ensure_owner(sessions, str(chat_id))
        entry["active_model"] = model
        self._save(sessions)

    def get_session(self, owner: int | str, model: str) -> tuple[str | None, bool]:
        sessions = self._load()
        entry = self._ensure_owner(sessions, str(owner))
        model_entry = entry["models"].get(model)
        if not model_entry:
            model_entry = self._new_model_session(model)
            entry["models"][model] = model_entry
            self._save(sessions)
        resume = bool(model_entry.get("initialized") and model_entry.get("session_id"))
        return model_entry.get("session_id"), resume

    def mark_initialized(self, owner: int | str, model: str, session_id: str | None = None) -> None:
        sessions = self._load()
        entry = self._ensure_owner(sessions, str(owner))
        model_entry = entry["models"].setdefault(model, self._new_model_session(model))
        if session_id:
            model_entry["session_id"] = session_id
        model_entry["initialized"] = True
        self._save(sessions)

    def new_session(self, chat_id: int, model: str | None = None) -> str | None:
        model = model or self.get_active_model(chat_id)
        sessions = self._load()
        entry = self._ensure_owner(sessions, str(chat_id))
        model_entry = self._new_model_session(model)
        entry["models"][model] = model_entry
        self._save(sessions)
        return model_entry.get("session_id")
