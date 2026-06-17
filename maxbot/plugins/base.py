"""Plugin contract — the lego pieces of maxbot.

A Plugin is a bundle of behavior that activates when listed in `bot.toml`'s
`[plugins].enabled` array. Each plugin owns:

  - Telegram commands (`/foo`, `/bar`)
  - Optionally: text pre-processors that intercept incoming text before the worker
  - Optionally: scheduled jobs (registered via `ctx.connector.job_queue`)
  - Optionally: shared state stored under `ctx.shared['my_plugin']`

A Plugin MUST be a class with:
  - class attribute `name` — the unique plugin name (matches the entry in bot.toml)
  - class attribute `description` — one-line human description
  - method `register(self, ctx: BotContext) -> None` — wire up handlers/jobs
  - optional method `shutdown(self, ctx: BotContext) -> None` — cleanup hook

The loader instantiates the Plugin with the plugin's TOML options dict:

    [plugins.my_plugin]
    interval = 1800
    notify = "always"

becomes:

    plugin = MyPlugin(options={"interval": 1800, "notify": "always"})

How to author a plugin
----------------------
See `docs/AUTHORING_PLUGINS.md`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..context import BotContext


@dataclass
class PluginMeta:
    name: str
    description: str
    module_path: str
    class_name: str


class Plugin(ABC):
    """Base class for all plugins. Subclass and implement `register`."""

    name: str = ""
    description: str = ""

    # Commands the plugin wants surfaced in Telegram's slash menu.
    # Each entry: (command_name_without_slash, short_description ≤ 50 chars).
    # The loader aggregates these and passes them to bot.set_my_commands at boot.
    telegram_commands: list[tuple[str, str]] = []

    def __init__(self, options: dict[str, Any] | None = None):
        self.options = options or {}

    @abstractmethod
    def register(self, ctx: "BotContext") -> None:
        """Wire up commands, text preprocessors, jobs. Called once at bootstrap."""

    def shutdown(self, ctx: "BotContext") -> None:  # noqa: B027
        """Optional cleanup hook called at bot shutdown."""
        return None
