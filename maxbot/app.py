"""App bootstrap — assemble core stores, runners, connector, handlers and plugins."""

from __future__ import annotations

import logging
from pathlib import Path

from telegram import BotCommand
from telegram.ext import Application

from .config import BotConfig, load_config
from .connectors.telegram.connector import TelegramConnector
from .context import BotContext
from .core.pending_work import PendingWorkStore
from .core.sessions import SessionStore
from .core.setup_state import SetupState
from .core.tasks import TaskStore
from .handlers import capabilities as capabilities_handler
from .handlers import core as core_handlers
from .handlers import messages as message_handlers
from . import onboarding
from .plugins.loader import load_plugins
from .runners.base import RunnerRegistry
from .runners.claude import ClaudeRunner
from .runners.codex import CodexRunner
from .runners.codex_appserver import CodexAppServerRunner

log = logging.getLogger(__name__)


_RUNNER_CLASSES = {
    "claude": ClaudeRunner,
    "codex": CodexRunner,
}


def _runner_class(name: str, runner_cfg) -> type | None:
    if name == "codex":
        # Default to the persistent app-server backend; "exec" keeps the
        # legacy one-shot `codex exec` runner.
        mode = (getattr(runner_cfg, "mode", "app-server") or "app-server").lower()
        return CodexRunner if mode == "exec" else CodexAppServerRunner
    return _RUNNER_CLASSES.get(name)


def build_runner_registry(config: BotConfig, sessions: SessionStore) -> RunnerRegistry:
    registry = RunnerRegistry()
    for name, runner_cfg in config.runners.items():
        if not runner_cfg.enabled:
            continue
        cls = _runner_class(name, runner_cfg)
        if cls is None:
            log.warning("Unknown runner '%s' — skipping", name)
            continue
        runner = cls(config, runner_cfg, sessions)
        registry.register(runner)
        log.info("Runner registered: %s → %s", name, runner.executable)
    if not registry.all_names():
        raise RuntimeError("No runners enabled — bot has nothing to run.")
    return registry


def build_app(config_path: Path | str) -> tuple[Application, BotContext]:
    config = load_config(config_path)
    config.media_dir.mkdir(parents=True, exist_ok=True)

    sessions = SessionStore(
        path=config.sessions_file,
        default_model=config.default_model,
        supported_models=list(config.runners.keys()),
    )
    tasks_store = TaskStore(config.tasks_file)
    pending = PendingWorkStore(config.pending_work_file)
    runners = build_runner_registry(config, sessions)

    # First-run onboarding. A previously-paired chat is added to the allowlist so
    # auth keeps working across restarts.
    setup = SetupState(config.workspace / "setup_state.json")
    if setup.paired_chat_id is not None:
        config.allowed_chat_ids.add(setup.paired_chat_id)

    connector = TelegramConnector(config)

    # Core commands. Plugins contribute via Plugin.telegram_commands; the loader
    # aggregates them into ctx.shared['capabilities'][<name>]['telegram_commands'].
    core_commands = [
        BotCommand("start", "Iniciar bot / ayuda"),
        BotCommand("new_session", "Nueva sesión del modelo activo"),
        BotCommand("change_model", "Cambiar runner activo"),
        BotCommand("status", "Estado del worker actual"),
        BotCommand("cancelar", "Cancelar tarea en curso"),
        BotCommand("capabilities", "Mostrar capacidades + plugins"),
        BotCommand("help", "Alias de /capabilities"),
    ]

    async def post_init(application: Application):
        # Aggregate core + plugin commands and publish them so they show up
        # in Telegram's slash menu.
        all_cmds = list(core_commands)
        for info in ctx.shared.get("capabilities", {}).values():
            for cmd_name, cmd_desc in info.get("telegram_commands", []):
                all_cmds.append(BotCommand(cmd_name, cmd_desc))
        # Telegram dedupes by name silently, but we dedupe explicitly for clarity.
        seen: set[str] = set()
        deduped = []
        for c in all_cmds:
            if c.command in seen:
                continue
            seen.add(c.command)
            deduped.append(c)
        await application.bot.set_my_commands(deduped)
        log.info("Published %d commands to Telegram", len(deduped))
        # Resume pending work
        work = pending.consume()
        if work:
            chat_id = work["chat_id"]
            session_id = work.get("session_id")
            model = work.get("model", sessions.get_active_model(chat_id))
            log.info("Resuming pending work for chat %d (%s session %s)", chat_id, model, session_id)
            resume_prompt = (
                "Continue from where you left off. "
                "The bot just restarted. Your previous session context is preserved. "
                "Continue with the last task and report your progress to the user."
            )
            ctx.workers.spawn(chat_id, resume_prompt, force_session=session_id, force_model=model)

    app = connector.build(post_init=post_init)

    # Construct the worker manager AFTER connector.build() so it has the app
    from .workers.manager import WorkerManager
    worker_manager = WorkerManager(config, connector, sessions, runners)

    ctx = BotContext(
        config=config,
        connector=connector,
        sessions=sessions,
        tasks_store=tasks_store,
        pending_work=pending,
        runners=runners,
        workers=worker_manager,
        setup=setup,
    )

    # Wire core handlers (commands + media)
    core_handlers.register(ctx)
    message_handlers.register(ctx)

    # Load + register plugins (lego pieces). This populates ctx.shared['capabilities'].
    load_plugins(ctx)

    # Register /capabilities AFTER plugins so it can read the capabilities dict.
    capabilities_handler.register(ctx)

    # Build the capability manifest once and stash it so the ALMA wizard can
    # re-apply it after writing a new system instruction.
    manifest = _build_capabilities_manifest(ctx)
    ctx.shared["capabilities_manifest"] = manifest

    # Optionally append the capability manifest to the bot-wide system instruction.
    # Only takes effect if at least one runner uses config.system_instruction (its
    # own per-runner system_instruction would override).
    if config.include_capabilities_in_system_instruction and manifest:
        config.system_instruction = (config.system_instruction or "") + "\n\n" + manifest
        log.info(
            "Appended capabilities manifest (%d chars) to system_instruction.",
            len(manifest),
        )

    # Onboarding wizard — high-priority handler that drives first-run setup and
    # self-disables once stage == done.
    onboarding.register(ctx)
    if not setup.is_done:
        _announce_pairing(setup)

    return app, ctx


def _announce_pairing(setup: SetupState) -> None:
    """Print the pairing code prominently so the operator can claim the bot."""
    if setup.stage == "pairing":
        code = setup.pairing_code
        banner = (
            "\n"
            "============================================================\n"
            "  maxbot is NOT yet paired.\n"
            f"  Open Telegram, message your bot, and send this code:\n\n"
            f"        PAIRING CODE:  {code}\n\n"
            "  The first chat to send it becomes the bot's owner.\n"
            "============================================================\n"
        )
        print(banner)
        log.warning("Pairing required — code: %s", code)
    else:
        log.info("Onboarding in progress (stage: %s)", setup.stage)


def _build_capabilities_manifest(ctx: BotContext) -> str:
    """Build a single text block summarizing framework + active plugins."""
    from pathlib import Path

    here = Path(__file__).resolve().parent.parent
    maxbot_md = here / "MAXBOT.md"

    parts: list[str] = []
    if maxbot_md.exists():
        parts.append("=== FRAMEWORK MANIFEST (MAXBOT.md) ===")
        parts.append(maxbot_md.read_text(encoding="utf-8"))

    caps = ctx.shared.get("capabilities", {})
    for name, info in caps.items():
        ctx_md = info.get("context_md")
        if ctx_md:
            parts.append(f"=== PLUGIN: {name} (CONTEXT.md) ===")
            parts.append(ctx_md)
    return "\n\n".join(parts)
