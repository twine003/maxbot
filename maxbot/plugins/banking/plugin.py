"""Banking / external-script plugin — an EXAMPLE plugin you can adapt.

This plugin demonstrates three reusable patterns:

  1. A command that spawns a worker which runs an external script and returns
     its output (`/informe_balance`).
  2. A command that asks the runner to follow a documented procedure stored in
     a file/skill (`/servidores`).
  3. A *text pre-processor* that intercepts plain chat text BEFORE the worker
     runs — here used to capture a one-time code (e.g. 2FA) and drop it into a
     shared JSON file that a waiting sub-agent can read (`enable_2fa_intake`).

None of the referenced scripts ship with maxbot — they are placeholders. Point
the options below at your own scripts, or disable this plugin entirely. It is
OFF by default in the example deployment.

Options (from `[plugins.banking]`):
    report_script     = "./scripts/generate_report.py"   # script run by /informe_balance
    procedure_doc     = "./scripts/server_status.md"      # doc followed by /servidores
    code_intake_file  = "./data/code_pending.json"        # where text codes are written
    enable_2fa_intake = true                               # register the text pre-processor
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from ..base import Plugin

if TYPE_CHECKING:
    from ...context import BotContext


class BankingPlugin(Plugin):
    name = "banking"
    description = "/informe_balance, /servidores + one-time code intake (example plugin)"
    telegram_commands = [
        ("informe_balance", "Run the configured report script"),
        ("servidores", "Check server status via the configured procedure"),
    ]

    def register(self, ctx: "BotContext") -> None:
        app = ctx.connector.app
        opts = self.options
        workspace = ctx.config.workspace

        def _resolve(value: str) -> str:
            p = Path(value)
            return str(p if p.is_absolute() else (workspace / p))

        report_script = _resolve(opts.get("report_script", "./scripts/generate_report.py"))
        procedure_doc = _resolve(opts.get("procedure_doc", "./scripts/server_status.md"))
        code_file = Path(_resolve(opts.get("code_intake_file", "./data/code_pending.json")))
        enable_2fa = opts.get("enable_2fa_intake", True)

        async def cmd_informe_balance(update: Update, _: ContextTypes.DEFAULT_TYPE):
            if not ctx.is_allowed(update):
                return
            chat_id = update.effective_chat.id
            prompt = (
                f"Quiero el informe completo. Para generarlo corré "
                f"python {report_script} en la shell. "
                "Cuando termine: pegame el stdout TAL CUAL (sin reformatear) y agregá al final una línea "
                "[ADJUNTO:<path del .html>] usando el path que sale en stderr como __REPORT_PATH__=<path>. "
                "Si el exit code es distinto de 0, reportame el stderr."
            )
            ctx.workers.spawn(chat_id, prompt)
            await update.message.reply_text("📊 Generando informe...")

        async def cmd_servidores(update: Update, _: ContextTypes.DEFAULT_TYPE):
            if not ctx.is_allowed(update):
                return
            chat_id = update.effective_chat.id
            prompt = (
                f"Necesito el estado de los servidores. El procedimiento exacto está documentado en "
                f"{procedure_doc}. Leelo primero con cat o type, después seguí los pasos que indica "
                "y presentame el informe en el formato que especifica el documento."
            )
            ctx.workers.spawn(chat_id, prompt)
            await update.message.reply_text("Verificando servidores...")

        app.add_handler(CommandHandler("informe_balance", cmd_informe_balance))
        app.add_handler(CommandHandler("servidores", cmd_servidores))

        if enable_2fa:
            code_re = re.compile(r'^2fa\s+\S+', re.IGNORECASE)

            async def code_preprocessor(update: Update, _context, _ctx) -> bool:
                """If the user sent '2fa <code>', drop the code into the shared file."""
                text = (update.message.text or "").strip()
                if not code_re.match(text):
                    return False
                code = text.split(None, 1)[1]
                try:
                    code_file.parent.mkdir(parents=True, exist_ok=True)
                    code_file.write_text(json.dumps({"code": code}), encoding="utf-8")
                    await update.message.reply_text(f"Código recibido: {code}")
                except Exception as e:
                    await update.message.reply_text(f"Error guardando código: {e}")
                return True

            ctx.shared.setdefault("text_preprocessors", []).append(code_preprocessor)
