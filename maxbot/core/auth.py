"""Chat-id allowlist enforcement."""

from __future__ import annotations

from telegram import Update


def is_allowed(update: Update, allowed_ids: set[int]) -> bool:
    """Returns True if the chat is in the allowlist.

    An EMPTY allowlist denies everyone. This is deliberate: a fresh bot has no
    allowed chats until it is claimed via the pairing flow (see the onboarding
    wizard). The wizard runs at a higher handler priority and handles pairing
    before this check is ever reached, so denying-on-empty closes the window
    where an unclaimed bot would otherwise answer strangers.
    """
    if not allowed_ids:
        return False
    chat = update.effective_chat
    if chat is None:
        return False
    return chat.id in allowed_ids
