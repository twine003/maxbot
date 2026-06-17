"""Send long messages safely (chunking + MarkdownV2 fallback) + attachment support.

Attachments use the `[ADJUNTO:/abs/path]` sentinel inside the result text; this
module strips them out, sends them as documents, and returns the cleaned text.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Awaitable, Callable

from telegram.constants import ParseMode

from .markdown import md_to_telegram, smart_split

log = logging.getLogger(__name__)

ATTACHMENT_RE = re.compile(r'^\[ADJUNTO:(.+?)\]\s*$', re.MULTILINE)
TELEGRAM_HARD_LIMIT = 4096


async def send_chunks(send_fn: Callable[..., Awaitable], text: str) -> None:
    formatted = md_to_telegram(text)
    fmt_chunks: list[str] = []
    remaining = formatted
    while remaining:
        chunk = smart_split(remaining, TELEGRAM_HARD_LIMIT)
        fmt_chunks.append(chunk)
        remaining = remaining[len(chunk):]
    plain_chunks: list[str] = []
    remaining = text
    while remaining:
        chunk = smart_split(remaining, TELEGRAM_HARD_LIMIT)
        plain_chunks.append(chunk)
        remaining = remaining[len(chunk):]
    for idx, fc in enumerate(fmt_chunks):
        try:
            await send_fn(fc, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception:
            pc = plain_chunks[idx] if idx < len(plain_chunks) else fc
            try:
                await send_fn(pc, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                try:
                    await send_fn(pc)
                except Exception as e:
                    log.error("Failed to send chunk %d: %s", idx, e)
    for idx in range(len(fmt_chunks), len(plain_chunks)):
        try:
            await send_fn(plain_chunks[idx], parse_mode=ParseMode.MARKDOWN)
        except Exception:
            try:
                await send_fn(plain_chunks[idx])
            except Exception as e:
                log.error("Failed to send extra chunk %d: %s", idx, e)


async def send_attachments(bot, chat_id: int, text: str) -> str:
    """Find [ADJUNTO:...] markers in `text`, send each as a document, return cleaned text."""
    matches = ATTACHMENT_RE.findall(text)
    if not matches:
        return text
    cleaned = ATTACHMENT_RE.sub('', text).strip()
    for file_path in matches:
        file_path = file_path.strip()
        p = Path(file_path)
        if p.is_file():
            try:
                with open(p, 'rb') as f:
                    await bot.send_document(chat_id=chat_id, document=f, filename=p.name)
            except Exception as e:
                log.error("Failed to send attachment %s: %s", file_path, e)
                await bot.send_message(chat_id=chat_id, text=f"Error enviando adjunto {p.name}: {e}")
        else:
            log.warning("Attachment not found: %s", file_path)
            await bot.send_message(chat_id=chat_id, text=f"Archivo no encontrado: {file_path}")
    return cleaned


async def send_long_to_chat(bot, chat_id: int, text: str) -> None:
    text = await send_attachments(bot, chat_id, text)
    if not text:
        return
    async def _send(content, parse_mode=None):
        await bot.send_message(chat_id=chat_id, text=content, parse_mode=parse_mode)
    await send_chunks(_send, text)
