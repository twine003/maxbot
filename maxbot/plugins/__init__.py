"""Plugin system — pluggable command bundles activated from bot.toml."""

from .base import Plugin, PluginMeta
from .loader import load_plugins

__all__ = ["Plugin", "PluginMeta", "load_plugins"]
