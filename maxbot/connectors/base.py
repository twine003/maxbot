"""Connector contract — the abstraction for chat platforms (Telegram, Discord, …).

A Connector is the bot's I/O surface for a single chat platform. It owns the
event loop integration with that platform (e.g. python-telegram-bot Application,
discord.py Client) and exposes a minimal, platform-agnostic API that workers,
streaming, plugins and handlers can use without knowing the platform.

Implementation status
---------------------
- v0.1: Telegram is the only connector. The ABC is designed to be expanded as
  new connectors are added. Some methods (e.g. `format_text`) currently lean
  on Telegram conventions (MarkdownV2) and may grow richer enums later.

How to add a new connector
--------------------------
See `docs/AUTHORING_CONNECTORS.md`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass
class IncomingMessage:
    """Platform-agnostic representation of an incoming user message."""
    chat_id: int
    text: str
    user_id: int | None = None
    user_handle: str | None = None
    raw: Any = None  # platform-native event (Update, discord.Message, …) for advanced use


@dataclass
class ConnectorCapabilities:
    """What a connector supports — plugins / workers can branch on this."""
    supports_edit: bool = True          # in-place message editing for streaming
    supports_documents: bool = True     # send_document()
    supports_voice_input: bool = False  # incoming voice notes
    supports_chat_action: bool = True   # e.g. 'typing…' indicator
    rate_limit_edits_per_min: int = 30  # for streaming throttle calibration


class Connector(ABC):
    """Base class for all chat-platform connectors.

    Lifecycle:
        1. `__init__(config, app_context)` — receive deployment config + bot context
        2. `register()` — wire up event handlers (called once at bootstrap)
        3. `run_forever()` — start the platform's event loop (blocking)
        4. `shutdown()` — clean shutdown
    """

    name: str = "connector"
    capabilities: ConnectorCapabilities = ConnectorCapabilities()

    @abstractmethod
    async def send_text(self, chat_id: int, text: str) -> int | None:
        """Send a plain or platform-formatted text message. Returns message id."""

    @abstractmethod
    async def edit_text(self, chat_id: int, message_id: int, text: str) -> None:
        """In-place edit (used by streaming). No-op on connectors without edits."""

    @abstractmethod
    async def send_document(self, chat_id: int, path: str, filename: str | None = None) -> None:
        """Send a file as an attachment/upload."""

    @abstractmethod
    async def send_chat_action(self, chat_id: int, action: str) -> None:
        """Show 'typing…' / 'uploading…' indicators (best-effort, may no-op)."""

    @abstractmethod
    async def download_file(self, file_id: str, dest_path: str) -> None:
        """Download a user-uploaded file to disk."""

    @abstractmethod
    def register_text_handler(self, handler: Callable[[IncomingMessage], Awaitable[None]]) -> None:
        ...

    @abstractmethod
    def register_command(self, name: str, handler: Callable[..., Awaitable[None]]) -> None:
        ...

    @abstractmethod
    async def run_forever(self) -> None:
        ...

    @abstractmethod
    async def shutdown(self) -> None:
        ...
