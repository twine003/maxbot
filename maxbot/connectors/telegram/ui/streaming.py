"""Live streaming of LLM output into a Telegram message via edit_message_text.

Uses a single message that grows in-place. When the message reaches `msg_max`,
the current edit is finalized and a new live message is started.
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING

from telegram.constants import ParseMode

from .markdown import md_to_telegram, split_at_safe_point

if TYPE_CHECKING:
    from ..workers.state import WorkerState

log = logging.getLogger(__name__)

# Telegram allows ~30 edits/min on the same message → 2.0s spacing is the hard cap.
RATE_LIMIT_RE = re.compile(r"retry after\s+(\d+)")


async def safe_edit(bot, chat_id: int, message_id: int, text: str) -> None:
    """Edit a message with MarkdownV2; fallback to Markdown classic; fallback to plain."""
    formatted = md_to_telegram(text)
    try:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=formatted,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    except Exception as e:
        err = str(e).lower()
        if "not modified" in err:
            return
        if "retry after" in err or "flood" in err or "429" in err:
            raise
    try:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=text, parse_mode=ParseMode.MARKDOWN,
        )
        return
    except Exception:
        pass
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
    except Exception as e:
        err = str(e).lower()
        if "not modified" not in err:
            log.warning("edit final fallback failed: %s", e)


async def safe_send(bot, chat_id: int, text: str) -> int | None:
    """Send a message with MarkdownV2 → Markdown → plain fallback. Returns message_id."""
    formatted = md_to_telegram(text)
    try:
        msg = await bot.send_message(chat_id=chat_id, text=formatted, parse_mode=ParseMode.MARKDOWN_V2)
        return msg.message_id
    except Exception:
        pass
    try:
        msg = await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
        return msg.message_id
    except Exception:
        pass
    try:
        msg = await bot.send_message(chat_id=chat_id, text=text)
        return msg.message_id
    except Exception as e:
        log.warning("send fallback failed: %s", e)
        return None


def reset_stream(worker: "WorkerState") -> None:
    """Reset live-streaming state for the next turn."""
    worker.stream_msg_id = None
    worker.stream_msg_text = ""
    worker.stream_carry = ""
    worker.stream_last_edit = 0.0
    worker.stream_using_partial = False
    worker.stream_activity_only = True


async def stream_update(
    bot, worker: "WorkerState", chat_id: int,
    msg_max: int, cooldown_seconds: float, force: bool = False,
) -> None:
    """Push `worker.stream_msg_text` into the live Telegram message.

    - Creates the live message if none exists.
    - Edits in-place when the new text fits.
    - When length > msg_max, finalize the current message and open a new one
      with the overflow stored in `worker.stream_carry`.
    - Throttled by `cooldown_seconds` (Telegram rate-limit safety).
    """
    if not worker.stream_msg_text and not worker.stream_carry:
        return

    now = time.monotonic()
    if not force and (now - worker.stream_last_edit) < cooldown_seconds:
        return
    worker.stream_last_edit = now

    text = worker.stream_msg_text

    # Case 1: no live message yet → create one
    if worker.stream_msg_id is None:
        first = text[:msg_max] if text else "…"
        mid = await safe_send(bot, chat_id, first or "…")
        if mid is not None:
            worker.stream_msg_id = mid
            if len(text) > msg_max:
                cut = split_at_safe_point(text, msg_max)
                worker.stream_msg_text = text[:cut]
                worker.stream_carry = text[cut:]
        return

    # Case 2: current message is full → finalize, open new one with carry
    if len(text) > msg_max:
        cut = split_at_safe_point(text, msg_max)
        first_part = text[:cut]
        rest = text[cut:]
        try:
            await safe_edit(bot, chat_id, worker.stream_msg_id, first_part)
        except Exception as e:
            err = str(e).lower()
            if "retry after" in err or "flood" in err or "429" in err:
                m = RATE_LIMIT_RE.search(err)
                wait_s = int(m.group(1)) if m else 5
                worker.stream_last_edit = time.monotonic() + wait_s
                return
        new_part = rest[:msg_max] or "…"
        mid = await safe_send(bot, chat_id, new_part)
        if mid is not None:
            worker.stream_msg_id = mid
            worker.stream_msg_text = rest[:msg_max] if rest else ""
            worker.stream_carry = rest[msg_max:] if len(rest) > msg_max else ""
        else:
            worker.stream_msg_id = None
        return

    # Case 3: normal edit of the live message
    try:
        await safe_edit(bot, chat_id, worker.stream_msg_id, text)
    except Exception as e:
        err = str(e).lower()
        if "retry after" in err or "flood" in err or "429" in err:
            m = RATE_LIMIT_RE.search(err)
            wait_s = int(m.group(1)) if m else 5
            log.warning("stream edit rate-limited, backing off %ds", wait_s)
            worker.stream_last_edit = time.monotonic() + wait_s
            return
        log.warning("stream edit failed: %s", e)
