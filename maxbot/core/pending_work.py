"""Persist 'pending work' across bot restarts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class PendingWorkStore:
    def __init__(self, path: Path):
        self.path = path

    def save(self, chat_id: int, prompt: str, session_id: str | None,
             resume: bool, model: str) -> None:
        data = {
            "chat_id": chat_id,
            "prompt": prompt,
            "session_id": session_id,
            "resume": resume,
            "model": model,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def consume(self) -> dict | None:
        """Returns the pending work payload and deletes it (one-shot)."""
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self.path.unlink(missing_ok=True)
            return None
        self.path.unlink(missing_ok=True)
        return data
