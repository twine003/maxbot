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
from datetime import datetime, timedelta, timezone
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

    @property
    def owner_user_id(self) -> int | None:
        """Telegram user id of whoever sent the pairing code. Falls back to
        `paired_chat_id` for states written before this field existed (a
        private chat id equals the user id, so the fallback is exact there)."""
        v = self._d.get("owner_user_id")
        if v is None:
            v = self._d.get("paired_chat_id")
        return int(v) if v is not None else None

    @owner_user_id.setter
    def owner_user_id(self, value: int) -> None:
        self._d["owner_user_id"] = int(value)
        self.save()

    @property
    def allowed_chat_ids(self) -> set[int]:
        raw = self._d.get("allowed_chat_ids", [])
        return {int(v) for v in raw}

    def add_allowed_chat_id(self, value: int) -> None:
        ids = self.allowed_chat_ids
        ids.add(int(value))
        self._d["allowed_chat_ids"] = sorted(ids)
        self.save()

    # ----- invitación de un solo uso -----
    # El `pairing_code` es permanente y solo lo acepta el dueño, así que no
    # sirve para dar acceso a otra persona (ella lo enviaría desde SU chat y
    # sería rechazada). La invitación es lo contrario: la genera el dueño con
    # /invitar, la usa alguien más, vale una sola vez y vence.
    @property
    def invite(self) -> dict[str, Any] | None:
        inv = self._d.get("invite")
        return inv if isinstance(inv, dict) else None

    def new_invite(self, ttl_minutes: int = 60, created_by: int | None = None) -> dict[str, Any]:
        expires = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
        inv = {
            "code": _gen_pairing_code(),
            "expires_at": expires.isoformat(),
            "created_by": int(created_by) if created_by is not None else None,
        }
        self._d["invite"] = inv
        self.save()
        return inv

    def clear_invite(self) -> None:
        if "invite" in self._d:
            del self._d["invite"]
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
