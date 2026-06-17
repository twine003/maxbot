"""Telegram connector — the reference implementation of `maxbot.connectors.base.Connector`."""

from .ui.markdown import md_to_telegram, escape_mdv2, split_at_safe_point, smart_split
from .ui.streaming import safe_edit, safe_send, stream_update, reset_stream
from .ui.attachments import send_chunks, send_attachments, send_long_to_chat
from .ui.activity import friendly_bash, extract_claude_activity, extract_codex_activity

__all__ = [
    "md_to_telegram", "escape_mdv2", "split_at_safe_point", "smart_split",
    "safe_edit", "safe_send", "stream_update", "reset_stream",
    "send_chunks", "send_attachments", "send_long_to_chat",
    "friendly_bash", "extract_claude_activity", "extract_codex_activity",
]
