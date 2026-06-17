"""/capabilities (alias /help) — surface MAXBOT.md + each plugin's CONTEXT.md.

Usage:
    /capabilities             → short summary + list of enabled plugins
    /capabilities <plugin>    → full CONTEXT.md of that plugin
    /capabilities framework   → full MAXBOT.md (framework manifest)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

if TYPE_CHECKING:
    from ..context import BotContext

log = logging.getLogger(__name__)


# Find MAXBOT.md by walking up from the maxbot package directory.
def _find_maxbot_md() -> Path | None:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "MAXBOT.md"
        if candidate.exists():
            return candidate
    return None


def _build_summary(ctx: "BotContext") -> str:
    caps = ctx.shared.get("capabilities", {})
    runners = ctx.runners.all_names()
    active_runner = ctx.config.default_model

    lines = [
        "*maxbot capabilities*",
        "",
        f"Workspace: `{ctx.config.workspace}`",
        f"Runners disponibles: {', '.join(runners)} (default: {active_runner})",
        "",
        "*Comandos base*",
        "/start — ayuda",
        "/new_session — nueva conversación",
        "/change_model <name> — cambiar runner",
        "/status — qué hace el worker",
        "/cancelar — matar el worker",
        "/capabilities [plugin] — esta vista",
        "/help — alias de /capabilities",
        "",
    ]
    if caps:
        lines.append("*Plugins activos*")
        for name, info in caps.items():
            desc = info.get("description") or "(sin descripción)"
            lines.append(f"- *{name}* — {desc}")
        lines.append("")
        lines.append("Detalle de un plugin: `/capabilities <name>`")
        lines.append("Manifest del framework: `/capabilities framework`")
    else:
        lines.append("(No hay plugins activos.)")
    return "\n".join(lines)


def _plugin_detail(ctx: "BotContext", name: str) -> str | None:
    caps = ctx.shared.get("capabilities", {})
    info = caps.get(name)
    if not info:
        return None
    md = info.get("context_md")
    if md:
        return md
    desc = info.get("description") or "(sin descripción)"
    return f"# {name}\n\n{desc}\n\n_(No CONTEXT.md available for this plugin.)_"


def register(ctx: "BotContext") -> None:
    app = ctx.connector.app
    maxbot_md_path = _find_maxbot_md()

    async def cmd_capabilities(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not ctx.is_allowed(update):
            return
        arg = (context.args[0].strip().lower() if context.args else "").replace("/", "")
        chat_id = update.effective_chat.id

        if not arg:
            text = _build_summary(ctx)
        elif arg == "framework":
            if maxbot_md_path and maxbot_md_path.exists():
                text = maxbot_md_path.read_text(encoding="utf-8")
            else:
                text = "_(MAXBOT.md no encontrado en el repo.)_"
        else:
            detail = _plugin_detail(ctx, arg)
            if detail is None:
                caps = ctx.shared.get("capabilities", {})
                available = ", ".join(caps.keys()) or "(ninguno)"
                text = f"No conozco el plugin `{arg}`. Activos: {available}."
            else:
                text = detail

        # Use the connector's long-message helper so MarkdownV2 + chunking work.
        await ctx.connector.send_long(chat_id, text)

    app.add_handler(CommandHandler("capabilities", cmd_capabilities))
    app.add_handler(CommandHandler("help", cmd_capabilities))
