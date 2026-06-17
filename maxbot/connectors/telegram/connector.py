"""TelegramConnector — python-telegram-bot adapter implementing the Connector ABC."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Awaitable, Callable, TYPE_CHECKING

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..base import Connector, ConnectorCapabilities, IncomingMessage
from .ui.attachments import send_long_to_chat as _send_long_to_chat
from .ui.streaming import safe_edit, safe_send

if TYPE_CHECKING:
    from ...config import BotConfig

log = logging.getLogger(__name__)


class TelegramConnector(Connector):
    """python-telegram-bot adapter. Owns the Application + JobQueue."""

    name = "telegram"
    capabilities = ConnectorCapabilities(
        supports_edit=True,
        supports_documents=True,
        supports_voice_input=True,
        supports_chat_action=True,
        rate_limit_edits_per_min=30,
    )

    def __init__(self, config: "BotConfig"):
        self.config = config
        self.app: Application | None = None

    # ------- lifecycle -------
    def build(self, post_init: Callable[[Application], Awaitable[None]] | None = None) -> Application:
        """Create the PTB Application. Call before register_* methods."""
        builder = Application.builder().token(self.config.token)
        if post_init:
            builder = builder.post_init(post_init)
        self.app = builder.build()
        return self.app

    @property
    def bot(self):
        """Raw PTB Bot object — for plugins that need PTB-specific features."""
        return self.app.bot

    @property
    def job_queue(self):
        return self.app.job_queue

    # ------- high-level send/edit -------
    async def send_text(self, chat_id: int, text: str) -> int | None:
        return await safe_send(self.app.bot, chat_id, text)

    async def edit_text(self, chat_id: int, message_id: int, text: str) -> None:
        await safe_edit(self.app.bot, chat_id, message_id, text)

    async def send_long(self, chat_id: int, text: str) -> None:
        """Send a possibly-long, markdown-formatted message with attachment support."""
        await _send_long_to_chat(self.app.bot, chat_id, text)

    async def send_document(self, chat_id: int, path: str, filename: str | None = None) -> None:
        p = Path(path)
        with open(p, "rb") as f:
            await self.app.bot.send_document(
                chat_id=chat_id, document=f, filename=filename or p.name,
            )

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        try:
            ptb_action = getattr(ChatAction, action.upper(), ChatAction.TYPING)
            await self.app.bot.send_chat_action(chat_id=chat_id, action=ptb_action)
        except Exception:
            pass

    async def download_file(self, file_id: str, dest_path: str) -> None:
        f = await self.app.bot.get_file(file_id)
        await f.download_to_drive(dest_path)

    # ------- handler registration -------
    def register_text_handler(
        self, handler: Callable[[IncomingMessage], Awaitable[None]]
    ) -> None:
        """Register a plain-text message handler (non-command)."""
        async def _wrap(update: Update, context: ContextTypes.DEFAULT_TYPE):
            text = update.message.text or ""
            chat = update.effective_chat
            user = update.effective_user
            msg = IncomingMessage(
                chat_id=chat.id if chat else 0,
                text=text,
                user_id=user.id if user else None,
                user_handle=user.username if user else None,
                raw=update,
            )
            await handler(msg)
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _wrap))

    def register_command(self, name: str, handler: Callable[..., Awaitable[None]]) -> None:
        """Register a /command. Handler receives (update, context) — PTB signature."""
        self.app.add_handler(CommandHandler(name, handler))

    def register_ptb_handler(self, handler) -> None:
        """Escape hatch: add a raw PTB handler. Use when register_command isn't enough."""
        self.app.add_handler(handler)

    async def run_forever(self) -> None:
        log.info("TelegramConnector starting polling…")
        # PTB manages its own loop with run_polling; this method is provided for
        # parity with the ABC but callers typically use `app.run_polling` directly.
        self.app.run_polling(drop_pending_updates=True)

    async def shutdown(self) -> None:
        if self.app:
            await self.app.shutdown()
