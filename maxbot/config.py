"""TOML-based configuration loader for maxbot deployments."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib  # type: ignore[import-not-found]
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore[import-not-found,no-redef]


@dataclass
class RunnerConfig:
    enabled: bool = True
    cli: str = "auto"
    system_instruction: str | None = None
    # Optional specific model to pass to the CLI (e.g. "claude-opus-4-8",
    # "claude-sonnet-4-6"). None = let the CLI use its default.
    model: str | None = None
    # Codex only: "app-server" (persistent JSON-RPC, lower latency) or "exec"
    # (one-shot `codex exec` per turn). Ignored by other runners.
    mode: str = "app-server"
    # Codex only: sandbox policy for the app-server ("danger-full-access" =
    # no sandbox, the default; "workspace-write"; "read-only").
    sandbox: str | None = None


@dataclass
class PluginsConfig:
    enabled: list[str] = field(default_factory=list)
    search_paths: list[str] = field(default_factory=list)
    options: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class BotConfig:
    # Where the runner is launched (its cwd). Point this at the project the bot
    # works on, so that project's own agent files (CLAUDE.md, AGENTS.md, ...) load.
    workspace: Path
    # Where this deployment keeps its own files (sessions, tasks, media, ...).
    # Defaults to `workspace`, so existing deployments keep working unchanged.
    state_dir: Path
    config_path: Path
    token: str
    allowed_chat_ids: set[int]
    default_model: str = "claude"
    timeout_seconds: int = 300
    max_retries: int = 2
    retry_delay: int = 3
    media_max_age: int = 3600
    progress_min_cooldown: float = 2.0
    progress_stale_interval: int = 60
    telegram_msg_max: int = 4000
    system_instruction: str = ""
    include_capabilities_in_system_instruction: bool = False
    runners: dict[str, RunnerConfig] = field(default_factory=dict)
    plugins: PluginsConfig = field(default_factory=PluginsConfig)
    allowed_tools: list[str] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def sessions_file(self) -> Path:
        return self.state_dir / "sessions.json"

    @property
    def tasks_file(self) -> Path:
        return self.state_dir / "tasks.json"

    @property
    def pending_work_file(self) -> Path:
        return self.state_dir / "pending_work.json"

    @property
    def media_dir(self) -> Path:
        return self.state_dir / "media"

    @property
    def restart_marker(self) -> Path:
        return self.state_dir / "restart_marker"


def _load_env_file(env_file: Path) -> None:
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _read_text_field(value: str | None, base_dir: Path) -> str:
    """Resolve a field that can be inline string OR a path reference '@file.md'."""
    if not value:
        return ""
    if value.startswith("@"):
        ref = value[1:].strip()
        path = Path(ref)
        if not path.is_absolute():
            path = base_dir / path
        return path.read_text(encoding="utf-8")
    return value


def load_config(config_path: Path | str) -> BotConfig:
    """Load and validate a deployment config from a TOML file.

    Resolution order for the workspace:
      1. [bot].workspace (absolute, or relative to the config file dir)
      2. The config file's parent directory (default)

    [bot].state_dir is resolved the same way and defaults to the workspace. Set
    it when the deployment folder is NOT inside the project the bot works on.
    """
    config_path = Path(config_path).resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    config_dir = config_path.parent

    bot_section = raw.get("bot", {}) or {}
    workspace_raw = bot_section.get("workspace")
    if workspace_raw:
        workspace = Path(workspace_raw)
        if not workspace.is_absolute():
            workspace = (config_dir / workspace).resolve()
    else:
        workspace = config_dir
    workspace.mkdir(parents=True, exist_ok=True)

    # state_dir lets a deployment live outside the project it works on: the
    # runner still runs inside that project (workspace), but sessions, tasks and
    # media stay in the deployment folder. Defaults to workspace.
    state_raw = bot_section.get("state_dir")
    if state_raw:
        state_dir = Path(state_raw)
        if not state_dir.is_absolute():
            state_dir = (config_dir / state_dir).resolve()
    else:
        state_dir = workspace
    state_dir.mkdir(parents=True, exist_ok=True)

    env_file = bot_section.get("env_file")
    if env_file:
        env_path = Path(env_file)
        if not env_path.is_absolute():
            env_path = config_dir / env_path
        _load_env_file(env_path)
    else:
        _load_env_file(workspace / ".env")
        _load_env_file(config_dir / ".env")

    token = bot_section.get("token") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN. Set it in .env, in env vars, or in [bot].token."
        )

    raw_ids = bot_section.get("allowed_chat_ids")
    if raw_ids is None:
        env_ids = os.environ.get("ALLOWED_CHAT_IDS", "")
        raw_ids = [int(x) for x in env_ids.split(",") if x.strip()]
    allowed = {int(x) for x in raw_ids if str(x).strip()}

    default_model = (bot_section.get("default_model") or "claude").lower()

    runners_cfg: dict[str, RunnerConfig] = {}
    for name, body in (raw.get("runners") or {}).items():
        body = body or {}
        runners_cfg[name] = RunnerConfig(
            enabled=bool(body.get("enabled", True)),
            cli=body.get("cli", "auto"),
            system_instruction=_read_text_field(body.get("system_instruction"), config_dir)
            if "system_instruction" in body
            else None,
            model=body.get("model"),
            mode=body.get("mode", "app-server"),
            sandbox=body.get("sandbox"),
        )
    runners_cfg.setdefault("claude", RunnerConfig())
    runners_cfg.setdefault("codex", RunnerConfig(system_instruction=""))

    plugins_section = raw.get("plugins") or {}
    plugins_cfg = PluginsConfig(
        enabled=list(plugins_section.get("enabled") or []),
        search_paths=list(plugins_section.get("search_paths") or []),
        options={
            name: body for name, body in plugins_section.items()
            if isinstance(body, dict) and name not in {"enabled", "search_paths"}
        },
    )

    system_instruction = _read_text_field(bot_section.get("system_instruction"), config_dir)

    return BotConfig(
        workspace=workspace,
        state_dir=state_dir,
        config_path=config_path,
        token=token,
        allowed_chat_ids=allowed,
        default_model=default_model,
        timeout_seconds=int(bot_section.get("timeout_seconds", 300)),
        max_retries=int(bot_section.get("max_retries", 2)),
        retry_delay=int(bot_section.get("retry_delay", 3)),
        media_max_age=int(bot_section.get("media_max_age", 3600)),
        progress_min_cooldown=float(bot_section.get("progress_min_cooldown", 2.0)),
        progress_stale_interval=int(bot_section.get("progress_stale_interval", 60)),
        telegram_msg_max=int(bot_section.get("telegram_msg_max", 4000)),
        system_instruction=system_instruction,
        include_capabilities_in_system_instruction=bool(
            bot_section.get("include_capabilities_in_system_instruction", False)
        ),
        runners=runners_cfg,
        plugins=plugins_cfg,
        allowed_tools=list(bot_section.get("allowed_tools") or []),
        extras=raw,
    )
