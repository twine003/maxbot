"""Chat-id allowlist enforcement."""

from __future__ import annotations

from telegram import Update


def is_allowed(update: Update, allowed_ids: set[int]) -> bool:
    """Returns True if the chat is in the allowlist (or no allowlist configured)."""
    if not allowed_ids:
        return True
    chat = update.effective_chat
    if chat is None:
        return False
    return chat.id in allowed_ids
