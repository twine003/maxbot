"""Built-in message handlers for text, photo, voice and document inputs.

Plugins can intercept text BEFORE it reaches the worker by registering a
text pre-processor on the BotContext (see `ctx.shared['text_preprocessors']`).
A pre-processor receives the update + context + bot context; if it returns True
the text is considered consumed and the worker is NOT spawned.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from typing import TYPE_CHECKING

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import MessageHandler, ContextTypes, filters

if TYPE_CHECKING:
    from ..context import BotContext

log = logging.getLogger(__name__)


def _le_hablan_a_el(update: Update, context) -> bool:
    """En un grupo, ¿este mensaje va dirigido al bot?

    Con el modo privacidad de BotFather ACTIVADO, Telegram ya filtra por él y
    solo le entrega menciones, respuestas y comandos... salvo que en la práctica
    tampoco entrega las menciones (comprobado 2026-09-11:
    el comando llegaba y la mención no). El remedio es apagar la privacidad,
    pero entonces le llega CADA mensaje del grupo y contestaría también las
    conversaciones entre las personas. Este filtro es lo que hace que apagarla
    sea seguro: en grupo solo responde si lo mencionan o si le responden a un
    mensaje suyo. En chat privado siempre responde.
    """
    chat = update.effective_chat
    if chat is None or chat.type not in ("group", "supergroup"):
        return True
    msg = update.effective_message
    if msg is None:
        return False

    reply = msg.reply_to_message
    if reply is not None and reply.from_user is not None:
        if reply.from_user.id == context.bot.id:
            return True

    username = getattr(context.bot, "username", None)
    if username:
        texto = (msg.text or msg.caption or "").lower()
        if f"@{username.lower()}" in texto:
            return True
    return False


def _with_sender(update: Update, text: str) -> str:
    """Prefija quién escribió, SOLO en grupos.

    En un grupo hay más de una persona hablando y el prompt no lleva ningún
    dato del emisor: sin esto el modelo no puede distinguir quién preguntó qué
    y le responde a todos como si fueran la misma persona. En chat privado el
    prompt se deja intacto.
    """
    chat = update.effective_chat
    if chat is None or chat.type not in ("group", "supergroup"):
        return text
    user = update.effective_user
    if user is None:
        return text
    quien = user.full_name or user.username or str(user.id)
    return f'[Mensaje de {quien} en el grupo "{chat.title or "privado"}"]\n{text}'


def register(ctx: "BotContext") -> None:
    app = ctx.connector.app
    media_dir = ctx.config.media_dir
    media_dir.mkdir(parents=True, exist_ok=True)

    # The plugins-shared registry for text pre-processors
    ctx.shared.setdefault("text_preprocessors", [])

    async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not ctx.is_allowed(update):
            return
        if not _le_hablan_a_el(update, context):
            return
        text = update.message.text
        if not text:
            return
        chat_id = update.effective_chat.id

        # Fallback for commands that arrived WITHOUT a bot_command entity
        # (e.g. copy/paste, certain clients) — PTB's CommandHandler requires
        # the entity, so the message ended up here instead of in the right
        # CommandHandler. Re-dispatch manually if the text starts with "/".
        stripped = text.lstrip()
        if stripped.startswith("/"):
            entities = update.message.entities or []
            has_bot_cmd = any(getattr(e, "type", "") == "bot_command" for e in entities)
            log.info(
                "Text starting with '/' arrived in handle_text (has_bot_command_entity=%s): %r",
                has_bot_cmd, text[:80],
            )
            if not has_bot_cmd:
                if await _redispatch_command(update, context, stripped):
                    return
                # If no command matched, warn the user instead of silently
                # forwarding the slash-text to the LLM.
                await update.message.reply_text(
                    "Eso parece un comando pero Telegram no lo marcó como tal. "
                    "Probá borrarlo y escribirlo de nuevo en una línea limpia."
                )
                return

        # Let plugins intercept first
        for preproc in ctx.shared["text_preprocessors"]:
            try:
                if await preproc(update, context, ctx):
                    return
            except Exception as e:
                log.error("text preprocessor error: %s", e)

        ctx.workers.spawn(chat_id, _with_sender(update, text))
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            pass

    async def _redispatch_command(update: Update, context: ContextTypes.DEFAULT_TYPE, stripped: str) -> bool:
        """Manually route a slash-prefixed message to a registered CommandHandler.

        Returns True if a handler was found and invoked; False otherwise.
        Used when PTB's automatic command dispatch missed because the message
        lacked the bot_command MessageEntity.
        """
        from telegram.ext import CommandHandler

        head, _, rest = stripped[1:].partition(" ")
        head = head.split("@", 1)[0].strip().lower()  # strip @botname if present
        if not head:
            return False
        # Mutate context.args to mirror what CommandHandler would have set.
        context.args = rest.strip().split() if rest.strip() else []
        for group_handlers in app.handlers.values():
            for handler in group_handlers:
                if isinstance(handler, CommandHandler) and head in {c.lower() for c in handler.commands}:
                    log.info("Redispatching '/%s' to its CommandHandler manually.", head)
                    await handler.callback(update, context)
                    return True
        return False

    async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not ctx.is_allowed(update):
            return
        if not _le_hablan_a_el(update, context):
            return
        chat_id = update.effective_chat.id
        photo = update.message.photo[-1]
        path = media_dir / f"photo_{photo.file_unique_id}.jpg"
        await ctx.connector.download_file(photo.file_id, str(path))

        caption = update.message.caption or ""
        prompt = f"El usuario envió esta imagen: {path}"
        if caption:
            prompt += f"\nCon el mensaje: {caption}"
        prompt += "\nAnaliza la imagen y responde."
        ctx.workers.spawn(chat_id, _with_sender(update, prompt))

    async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not ctx.is_allowed(update):
            return
        if not _le_hablan_a_el(update, context):
            return
        chat_id = update.effective_chat.id
        voice = update.message.voice or update.message.audio
        if not voice:
            return

        oga_path = media_dir / f"voice_{voice.file_unique_id}.oga"
        wav_path = media_dir / f"voice_{voice.file_unique_id}.wav"
        await ctx.connector.download_file(voice.file_id, str(oga_path))

        conv = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(oga_path),
            "-ar", "16000", "-ac", "1", str(wav_path),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        await conv.communicate()

        try:
            import speech_recognition as sr  # type: ignore
            recognizer = sr.Recognizer()
            with sr.AudioFile(str(wav_path)) as source:
                audio = recognizer.record(source)
            transcription = recognizer.recognize_google(audio, language="es-ES")
        except ImportError:
            transcription = "(speech_recognition no instalado — pip install SpeechRecognition)"
        except Exception as e:
            transcription = f"(error transcripción: {e})"

        await update.message.reply_text(f"[Transcripción: {transcription}]")
        ctx.workers.spawn(
            chat_id,
            _with_sender(update, f"Mensaje de voz del usuario (transcripción): {transcription}"),
        )

    async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not ctx.is_allowed(update):
            return
        if not _le_hablan_a_el(update, context):
            return
        chat_id = update.effective_chat.id
        doc = update.message.document
        fname = doc.file_name or f"doc_{doc.file_unique_id}"
        path = media_dir / fname
        await ctx.connector.download_file(doc.file_id, str(path))

        caption = update.message.caption or ""
        prompt = f"El usuario envió este archivo: {path} (nombre: {fname})"
        if caption:
            prompt += f"\nCon el mensaje: {caption}"
        prompt += "\nAnaliza el archivo y responde."
        ctx.workers.spawn(chat_id, _with_sender(update, prompt))

    async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not ctx.is_allowed(update):
            return
        if not _le_hablan_a_el(update, context):
            return
        chat_id = update.effective_chat.id
        v = update.message.video or update.message.video_note or update.message.animation
        if not v:
            return
        fname = getattr(v, "file_name", None) or f"video_{v.file_unique_id}.mp4"
        path = media_dir / fname
        await ctx.connector.download_file(v.file_id, str(path))

        caption = update.message.caption or ""
        prompt = f"El usuario envió este video: {path} (nombre: {fname})"
        if caption:
            prompt += f"\nCon el mensaje: {caption}"
        prompt += "\nAnaliza el video y responde."
        ctx.workers.spawn(chat_id, _with_sender(update, prompt))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE | filters.ANIMATION, handle_video))
