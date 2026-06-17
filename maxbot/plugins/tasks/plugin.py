"""Recurring + one-time task CRUD via Telegram commands.

Storage is `<workspace>/tasks.json` via `ctx.tasks_store`. The heartbeat plugin
is what actually executes them.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from ...core.tasks import parse_interval
from ..base import Plugin

if TYPE_CHECKING:
    from ...context import BotContext


class TasksPlugin(Plugin):
    name = "tasks"
    description = "/tarea, /tarea_once, /tareas, /borrar_tarea, /pausar_tarea"
    telegram_commands = [
        ("tarea", "Crear tarea recurrente (cron)"),
        ("tarea_once", "Crear tarea de una sola vez"),
        ("tareas", "Listar todas las tareas"),
        ("borrar_tarea", "Eliminar tarea por ID"),
        ("pausar_tarea", "Pausar/reanudar tarea"),
    ]

    def register(self, ctx: "BotContext") -> None:
        app = ctx.connector.app
        store = ctx.tasks_store

        async def cmd_tarea(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not ctx.is_allowed(update):
                return
            text = " ".join(context.args) if context.args else ""
            if not text:
                await update.message.reply_text(
                    "Uso: /tarea <descripción>\n"
                    "Ejemplo: /tarea Revisá el estado de los servidores cada 6h\n\n"
                    "Frecuencias: 'cada 30min', 'cada 2h', 'cada 6 horas', 'diario'"
                )
                return
            interval = parse_interval(text)
            note = ""
            if interval is None:
                interval = 30
                note = " (frecuencia no detectada, usando 30 min por defecto)"
            prompt = re.sub(
                r"cada\s+\d+\s*(?:h(?:oras?)?|min(?:utos?)?)|diario|cada\s+(?:dia|día|hora)",
                "", text, flags=re.IGNORECASE,
            ).strip()
            if not prompt:
                prompt = text
            task_id = str(uuid.uuid4())[:8]
            tasks = store.load()
            tasks["recurring"].append({
                "id": task_id, "name": prompt[:50], "prompt": prompt,
                "interval_minutes": interval, "notify": "always",
                "enabled": True, "last_run": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            store.save(tasks)
            h, m = divmod(interval, 60)
            freq = f"{h}h {m}min" if h else f"{m} min"
            await update.message.reply_text(
                f"Tarea recurrente creada{note}\n\nID: {task_id}\nTarea: {prompt[:80]}\nFrecuencia: cada {freq}"
            )

        async def cmd_tarea_once(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not ctx.is_allowed(update):
                return
            text = " ".join(context.args) if context.args else ""
            if not text:
                await update.message.reply_text(
                    "Uso: /tarea_once <descripción>\nSe ejecuta en el próximo heartbeat (máx 30 min)."
                )
                return
            task_id = str(uuid.uuid4())[:8]
            tasks = store.load()
            tasks["one_time"].append({
                "id": task_id, "prompt": text,
                "run_at": datetime.now(timezone.utc).isoformat(),
                "notify": "always",
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            store.save(tasks)
            await update.message.reply_text(f"Tarea programada (una vez)\nID: {task_id}\nTarea: {text[:80]}")

        async def cmd_tareas(update: Update, _: ContextTypes.DEFAULT_TYPE):
            if not ctx.is_allowed(update):
                return
            tasks = store.load()
            lines: list[str] = []
            if tasks["recurring"]:
                lines.append("RECURRENTES:")
                for t in tasks["recurring"]:
                    h, m = divmod(t["interval_minutes"], 60)
                    freq = f"{h}h{m:02d}m" if h else f"{m}min"
                    status = "ON" if t.get("enabled", True) else "PAUSADA"
                    last = t.get("last_run", "nunca")
                    if last and last != "nunca":
                        last = last[:16].replace("T", " ")
                    lines.append(f"  [{t['id']}] {status} | cada {freq} | {t.get('name', t['prompt'][:40])}")
                    lines.append(f"    Última: {last}")
            else:
                lines.append("No hay tareas recurrentes.")
            if tasks["one_time"]:
                lines.append("\nPROGRAMADAS (una vez):")
                for t in tasks["one_time"]:
                    run_at = t["run_at"][:16].replace("T", " ")
                    lines.append(f"  [{t['id']}] {run_at} | {t['prompt'][:50]}")
            else:
                lines.append("\nNo hay tareas programadas.")
            await update.message.reply_text("\n".join(lines))

        async def cmd_borrar_tarea(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not ctx.is_allowed(update):
                return
            task_id = context.args[0] if context.args else ""
            if not task_id:
                await update.message.reply_text("Uso: /borrar_tarea <id>")
                return
            tasks = store.load()
            before_rec = len(tasks["recurring"])
            tasks["recurring"] = [t for t in tasks["recurring"] if not t["id"].startswith(task_id)]
            removed_rec = before_rec - len(tasks["recurring"])
            before_one = len(tasks["one_time"])
            tasks["one_time"] = [t for t in tasks["one_time"] if not t["id"].startswith(task_id)]
            removed_one = before_one - len(tasks["one_time"])
            if removed_rec or removed_one:
                store.save(tasks)
                await update.message.reply_text(f"Tarea {task_id} eliminada.")
            else:
                await update.message.reply_text(f"No se encontró tarea con ID: {task_id}")

        async def cmd_pausar_tarea(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not ctx.is_allowed(update):
                return
            task_id = context.args[0] if context.args else ""
            if not task_id:
                await update.message.reply_text("Uso: /pausar_tarea <id>")
                return
            tasks = store.load()
            for t in tasks["recurring"]:
                if t["id"].startswith(task_id):
                    t["enabled"] = not t.get("enabled", True)
                    state = "activada" if t["enabled"] else "pausada"
                    store.save(tasks)
                    await update.message.reply_text(f"Tarea {t['id']} {state}.")
                    return
            await update.message.reply_text(f"No se encontró tarea con ID: {task_id}")

        app.add_handler(CommandHandler("tarea", cmd_tarea))
        app.add_handler(CommandHandler("tarea_once", cmd_tarea_once))
        app.add_handler(CommandHandler("tareas", cmd_tareas))
        app.add_handler(CommandHandler("borrar_tarea", cmd_borrar_tarea))
        app.add_handler(CommandHandler("pausar_tarea", cmd_pausar_tarea))
