"""Persistent onboarding state for the in-Telegram setup wizard.

Lives at `<workspace>/setup_state.json`. Tracks how far first-time setup has
progressed so the bot can resume the wizard across restarts. Created
automatically; safe to delete to re-run onboarding from scratch.

Stages:
    pairing       – waiting for the user to send the pairing code shown in the
                    server console (claims ALLOWED_CHAT_IDS = that chat).
    agent_select  – ask which AI agent CLI to use (claude / codex).
    agent_install – install the chosen agent's CLI (npm).
    agent_auth    – authenticate the agent (API key paste / browser login).
    alma          – guided wizard that writes the bot's "ALMA" (.md persona).
    done          – fully configured; normal operation.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

STAGES = ("pairing", "agent_select", "agent_install", "agent_auth", "alma", "done")


def _gen_pairing_code() -> str:
    """A short, human-typeable code, e.g. 'K7P-Q2M'. Avoids ambiguous chars."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(6))
    return f"{raw[:3]}-{raw[3:]}"


class SetupState:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._d: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        return {"stage": "pairing"}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._d, indent=2, ensure_ascii=False), encoding="utf-8")

    # ----- generic access -----
    def get(self, key: str, default: Any = None) -> Any:
        return self._d.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._d[key] = value
        self.save()

    # ----- stage -----
    @property
    def stage(self) -> str:
        return self._d.get("stage", "pairing")

    @stage.setter
    def stage(self, value: str) -> None:
        if value not in STAGES:
            raise ValueError(f"Unknown setup stage: {value}")
        self._d["stage"] = value
        self.save()

    @property
    def is_done(self) -> bool:
        return self.stage == "done"

    # ----- pairing -----
    @property
    def pairing_code(self) -> str:
        code = self._d.get("pairing_code")
        if not code:
            code = _gen_pairing_code()
            self._d["pairing_code"] = code
            self.save()
        return code

    @property
    def paired_chat_id(self) -> int | None:
        v = self._d.get("paired_chat_id")
        return int(v) if v is not None else None

    @paired_chat_id.setter
    def paired_chat_id(self, value: int) -> None:
        self._d["paired_chat_id"] = int(value)
        self.save()

    # ----- agent -----
    @property
    def agent(self) -> str | None:
        return self._d.get("agent")

    @agent.setter
    def agent(self, value: str) -> None:
        self._d["agent"] = value
        self.save()

    # ----- alma answers -----
    @property
    def alma(self) -> dict[str, Any]:
        return self._d.setdefault("alma", {"step": 0, "answers": {}})

    def save_alma(self, alma: dict[str, Any]) -> None:
        self._d["alma"] = alma
        self.save()
