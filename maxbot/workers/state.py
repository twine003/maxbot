"""WorkerState — mutable per-turn state owned by the manager and updated by runners."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class WorkerState:
    """Per-turn state shared between the manager, the runner, and the streaming layer."""
    task: asyncio.Task | None = None
    proc: asyncio.subprocess.Process | None = None
    model: str = ""
    session_id: str = ""
    start_time: float = 0.0
    last_activity: str = "Iniciando..."
    last_reported_activity: str = ""
    last_progress_sent: float = 0.0
    cancel_requested: bool = False
    activity_log: list = field(default_factory=list)

    # Streaming bookkeeping (live message that grows via edit_message_text)
    _seen_text_len: int = 0
    _text_buffer: str = ""
    _last_text_send: float = 0.0
    stream_msg_id: int | None = None
    stream_msg_text: str = ""
    stream_carry: str = ""
    stream_last_edit: float = 0.0
    stream_using_partial: bool = False
    stream_activity_only: bool = True
