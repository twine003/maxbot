"""Runner contract — the abstraction for AI engines (Claude, Codex, future Gemini, …).

A Runner is responsible for:
- Locating the CLI on disk (`find_executable`)
- Building the subprocess command for a turn (`build_command`)
- Running a turn and streaming progress into the live Telegram message (`run_turn`)

The Runner does NOT own Telegram or chat state — it receives a Connector + WorkerState
and updates them via well-defined helpers in `maxbot.connectors.telegram.ui`.

How to add a new runner
-----------------------
See `docs/AUTHORING_RUNNERS.md`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import BotConfig, RunnerConfig
    from ..workers.state import WorkerState
    from ..connectors.telegram.connector import TelegramConnector
    from ..core.sessions import SessionStore


@dataclass
class RunnerResult:
    text: str
    session_id: str | None = None
    error: bool = False


class Runner(ABC):
    """A subprocess-driven AI engine adapter (Claude CLI, Codex CLI, …)."""

    name: str = ""
    label: str = ""

    def __init__(self, config: "BotConfig", runner_config: "RunnerConfig", sessions: "SessionStore"):
        self.config = config
        self.runner_config = runner_config
        self.sessions = sessions
        self.executable = self._resolve_executable()

    @abstractmethod
    def find_executable(self) -> str:
        """Locate the CLI binary on disk. Should return a path that exists, or the
        plain CLI name as a last-resort fallback (will fail at exec time)."""

    def _resolve_executable(self) -> str:
        if self.runner_config.cli and self.runner_config.cli != "auto":
            return self.runner_config.cli
        return self.find_executable()

    @abstractmethod
    def build_command(self, prompt: str, session_id: str | None, resume: bool) -> list[str]:
        """Build the argv for a single turn (subprocess.exec)."""

    @abstractmethod
    async def run_turn(
        self,
        connector: "TelegramConnector",
        chat_id: int,
        prompt: str,
        session_id: str | None,
        resume: bool,
        worker: "WorkerState",
    ) -> RunnerResult:
        """Run a single turn end-to-end. Streams progress into worker.stream_msg_id."""

    # ------- shared helpers -------
    def get_system_instruction(self) -> str:
        """Per-runner system instruction. Falls back to the bot-wide one."""
        if self.runner_config.system_instruction is not None:
            return self.runner_config.system_instruction
        return self.config.system_instruction


class RunnerRegistry:
    """Holds the active runners keyed by name. Built once at bootstrap."""

    def __init__(self):
        self._runners: dict[str, Runner] = {}

    def register(self, runner: Runner) -> None:
        if runner.name in self._runners:
            raise ValueError(f"Runner already registered: {runner.name}")
        self._runners[runner.name] = runner

    def get(self, name: str) -> Runner:
        if name not in self._runners:
            raise KeyError(f"Runner not registered: {name}")
        return self._runners[name]

    def all_names(self) -> list[str]:
        return list(self._runners)

    def label(self, name: str) -> str:
        if name in self._runners:
            return self._runners[name].label or name
        return name
