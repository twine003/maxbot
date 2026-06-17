"""BotContext — the shared service container handed to plugins, handlers and runners.

A plugin's `register(app, ctx)` receives an instance of this. Use the attributes
to access the connector, sessions, tasks store, worker manager, etc. without
re-wiring globals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import BotConfig
    from .connectors.telegram.connector import TelegramConnector
    from .core.pending_work import PendingWorkStore
    from .core.sessions import SessionStore
    from .core.tasks import TaskStore
    from .runners.base import RunnerRegistry
    from .workers.manager import WorkerManager


@dataclass
class BotContext:
    config: "BotConfig"
    connector: "TelegramConnector"
    sessions: "SessionStore"
    tasks_store: "TaskStore"
    pending_work: "PendingWorkStore"
    runners: "RunnerRegistry"
    workers: "WorkerManager"
    # Free-form bucket for plugins that need to share state with each other
    shared: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.shared is None:
            self.shared = {}

    # ----- common helpers exposed to plugins -----
    def is_allowed(self, update) -> bool:
        from .core.auth import is_allowed
        return is_allowed(update, self.config.allowed_chat_ids)
