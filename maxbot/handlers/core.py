"""Built-in command handlers: /start, /new_session, /change_model, /status, /cancelar."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

if TYPE_CHECKING:
    from ..context import BotContext


def register(ctx: "BotContext") -> None:
    app = ctx.connector.app

    async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE):
        if not ctx.is_allowed(update):
            return
        model = ctx.sessions.get_active_model(update.effective_chat.id)
        # The list of available plugin commands is appended by the loader if
        # plugins choose to publish a 'help_line'. We keep a minimal core help here.
        await update.message.reply_text(
            f"Bot conectado. Modelo activo: {ctx.runners.label(model)}.\n"
            "Envía texto, imágenes o notas de voz.\n\n"
            "Comandos base:\n"
            "/new_session — nueva conversación\n"
            "/change_model — cambiar entre runners\n"
            "/status — qué está haciendo el worker\n"
            "/cancelar — cancelar tarea en curso"
        )

    async def cmd_new_session(update: Update, _: ContextTypes.DEFAULT_TYPE):
        if not ctx.is_allowed(update):
            return
        chat_id = update.effective_chat.id
        model = ctx.sessions.get_active_model(chat_id)
        ctx.workers.cancel_all(chat_id)
        sid = ctx.sessions.new_session(chat_id, model)
        if sid:
            await update.message.reply_text(
                f"Nueva sesión de {ctx.runners.label(model)} creada.\nID: `{sid}`",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                f"Nueva sesión de {ctx.runners.label(model)} creada.\n"
                "El ID se asignará con tu próximo mensaje."
            )

    async def cmd_change_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not ctx.is_allowed(update):
            return
        chat_id = update.effective_chat.id
        current_model = ctx.sessions.get_active_model(chat_id)
        if ctx.workers.list_active(chat_id):
            await update.message.reply_text(
                "Hay una tarea en curso. Usa /cancelar o espera antes de cambiar de modelo."
            )
            return
        requested = context.args[0].strip().lower() if context.args else ""
        supported = ctx.runners.all_names()
        if not requested:
            opts = "  o  ".join(f"/change_model {m}" for m in supported)
            await update.message.reply_text(
                f"Modelo activo: {ctx.runners.label(current_model)}.\nUso: {opts}"
            )
            return
        if requested not in supported:
            await update.message.reply_text(f"Modelo no soportado. Opciones: {', '.join(supported)}.")
            return
        if requested == current_model:
            await update.message.reply_text(f"Ya estás conversando con {ctx.runners.label(current_model)}.")
            return
        ctx.sessions.set_active_model(chat_id, requested)
        session_id, resume = ctx.sessions.get_session(chat_id, requested)
        if session_id and resume:
            session_text = f"Sesión activa: `{session_id}`"
            parse_mode = "Markdown"
        elif session_id:
            session_text = f"Sesión preparada: `{session_id}`"
            parse_mode = "Markdown"
        else:
            session_text = "Sesión nueva — se inicializará con tu próximo mensaje."
            parse_mode = None
        await update.message.reply_text(
            f"Modelo cambiado a {ctx.runners.label(requested)}.\n{session_text}",
            parse_mode=parse_mode,
        )

    async def cmd_status(update: Update, _: ContextTypes.DEFAULT_TYPE):
        if not ctx.is_allowed(update):
            return
        chat_id = update.effective_chat.id
        workers = ctx.workers.list_active(chat_id)
        if not workers:
            model = ctx.sessions.get_active_model(chat_id)
            session_id, resume = ctx.sessions.get_session(chat_id, model)
            session_text = session_id if session_id else "pendiente"
            state = "reutilizable" if resume else "nueva"
            await update.message.reply_text(
                f"Libre, no hay tarea en curso.\n"
                f"Modelo activo: {ctx.runners.label(model)}\n"
                f"Sesión: {session_text} ({state})"
            )
            return
        lines = []
        for i, worker in enumerate(workers, 1):
            elapsed = time.monotonic() - worker.start_time
            mins = int(elapsed) // 60
            secs = int(elapsed) % 60
            time_str = f"{mins}m {secs:02d}s" if mins else f"{secs}s"
            prefix = f"[{i}/{len(workers)}] " if len(workers) > 1 else ""
            lines.append(
                f"{prefix}{worker.last_activity} ({time_str}) — {ctx.runners.label(worker.model)}"
            )
        await update.message.reply_text("\n".join(lines))

    async def cmd_cancelar(update: Update, _: ContextTypes.DEFAULT_TYPE):
        if not ctx.is_allowed(update):
            return
        count = ctx.workers.cancel_all(update.effective_chat.id)
        if count == 0:
            await update.message.reply_text("No hay tarea en curso.")
        else:
            await update.message.reply_text(f"Cancelando {count} tarea(s)...")

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new_session", cmd_new_session))
    app.add_handler(CommandHandler("nueva_session", cmd_new_session))
    app.add_handler(CommandHandler("change_model", cmd_change_model))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cancelar", cmd_cancelar))
