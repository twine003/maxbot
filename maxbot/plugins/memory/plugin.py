"""Semantic memory commands — an EXAMPLE plugin you can adapt.

Delegates to an external `semantic_memory_script` for vector-store search/write.
The script does NOT ship with maxbot — point the option at your own, or keep this
plugin disabled (it is OFF by default in the example deployment).

Options (from `[plugins.memory]`):
    semantic_memory_script = "./scripts/semantic_memory.py"   # relative paths resolve against workspace
    enable_documentar = true   # requires Claude (Skills/sub-agents); off for codex-only deployments
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from ..base import Plugin

if TYPE_CHECKING:
    from ...context import BotContext


class MemoryPlugin(Plugin):
    name = "memory"
    description = "/memoria, /recordar, /documentar"
    telegram_commands = [
        ("memoria", "Buscar en memoria semántica"),
        ("recordar", "Guardar detalle en memoria"),
        ("documentar", "Documentar y cerrar la sesión"),
    ]

    def register(self, ctx: "BotContext") -> None:
        app = ctx.connector.app
        from pathlib import Path
        raw_script = self.options.get("semantic_memory_script", "./scripts/semantic_memory.py")
        _p = Path(raw_script)
        script = str(_p if _p.is_absolute() else (ctx.config.workspace / _p))
        enable_documentar = self.options.get("enable_documentar", True)

        async def cmd_memoria(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not ctx.is_allowed(update):
                return
            query = " ".join(context.args).strip() if context.args else ""
            if not query:
                await update.message.reply_text(
                    "Uso: /memoria <pregunta>\n"
                    "Ejemplo: /memoria que hicimos con los bancos ayer"
                )
                return
            safe_query = query.replace('"', '\\"')
            prompt = (
                f"Necesito buscar \"{query}\" en mi memoria semántica. "
                f"Para hacerlo, corré en la shell el comando "
                f"python {script} search \"{safe_query}\" -n 5 --json. "
                "Después con los resultados del JSON: "
                "si el top score es >= 0.55 presentame los hallazgos resumidos (archivo, qué dice, fecha) citando la fuente; "
                "si el top está entre 0.45 y 0.55 presentalos pero avisame que la confianza es media; "
                "si el top es < 0.45 decime que no encontraste nada relevante."
            )
            ctx.workers.spawn(update.effective_chat.id, prompt)
            await update.message.reply_text(f"🔍 Buscando en memoria: {query[:60]}...")

        async def cmd_recordar(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not ctx.is_allowed(update):
                return
            text = " ".join(context.args).strip() if context.args else ""
            if not text:
                await update.message.reply_text(
                    "Uso: /recordar <detalle a guardar>\n"
                    "Ejemplo: /recordar prefiero respuestas cortas sin tablas grandes"
                )
                return
            safe_text = text.replace('"', '\\"')
            prompt = (
                f"El usuario quiere que recuerde este detalle PARA SIEMPRE: \"{text}\". "
                "Lo tenés que guardar en la memoria semántica corriendo en la shell el comando "
                f"python {script} add \"{safe_text}\" "
                "--id slug-aca --name desc-aca --type tipo-aca, donde reemplaces: "
                "slug-aca por un identificador único en formato categoría_keywords (ej pref_respuestas_cortas); "
                "desc-aca por una descripción breve del detalle (1 frase, entre comillas si tiene espacios); "
                "tipo-aca por uno de estos valores literales según corresponda: preference, fact, decision, lesson, config, o relationship. "
                "Después de ejecutar, confirmame que quedó guardado citando el id que usaste."
            )
            ctx.workers.spawn(update.effective_chat.id, prompt)
            await update.message.reply_text("💾 Guardando en memoria...")

        async def cmd_documentar(update: Update, _: ContextTypes.DEFAULT_TYPE):
            if not ctx.is_allowed(update):
                return
            chat_id = update.effective_chat.id
            if ctx.sessions.get_active_model(chat_id) == "codex":
                await update.message.reply_text(
                    "📝 /documentar requiere el sub-agente session-documenter, que solo está "
                    "disponible con Claude. Cambiá con /change_model claude y volvé a probar."
                )
                return
            prompt = (
                "Invoca la Skill `documentar_session` para cerrar esta sesión de trabajo. "
                "Antes de lanzarla, sintetiza la conversación actual con el usuario (los últimos turns) "
                "en el brief que pide el skill (3 bloques): "
                "**Discoveries** (hallazgos durables), **Code changes** (commits/edits con el porqué), "
                "**Feedback del usuario** (correcciones o validaciones). "
                "Si los 3 bloques quedan vacíos, NO lances el agente — responde al usuario "
                "\"no hay nada durable que documentar de esta sesión\". "
                "Si hay contenido, lanza el sub-agente session-documenter con el brief, "
                "y devuelve el reporte estructurado tal cual al usuario. "
                "Reindexa el vector store al final si el agente no lo hace."
            )
            ctx.workers.spawn(chat_id, prompt)
            await update.message.reply_text("📝 Documentando sesión...")

        app.add_handler(CommandHandler("memoria", cmd_memoria))
        app.add_handler(CommandHandler("recordar", cmd_recordar))
        if enable_documentar:
            app.add_handler(CommandHandler("documentar", cmd_documentar))
