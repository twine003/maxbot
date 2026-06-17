"""Plugin discovery and activation.

The loader:
  1. Iterates `config.plugins.enabled` — the list of plugin names to activate.
  2. For each name, looks for `maxbot.plugins.<name>:Plugin` (built-in plugins).
  3. If not found, searches `config.plugins.search_paths` for an importable
     `<path>/<name>/__init__.py` exposing a `Plugin` subclass.
  4. Instantiates with `options = config.plugins.options[name]`.
  5. Calls `plugin.register(ctx)`.
  6. Reads `CONTEXT.md` next to the plugin (if present) and stores it in
     `ctx.shared['capabilities']` so the `/capabilities` handler can return it.

Plugins extend `maxbot.plugins.base.Plugin` and live in their own subpackage:

    my_plugin/
    ├── __init__.py     # exposes a `Plugin` class
    ├── plugin.py       # the actual implementation
    └── CONTEXT.md      # capabilities manifest (RECOMMENDED)
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .base import Plugin

if TYPE_CHECKING:
    from ..context import BotContext

log = logging.getLogger(__name__)


def _import_from_path(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if not spec or not spec.loader:
        raise ImportError(f"Cannot load spec for {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _discover_plugin_class(name: str, search_paths: list[Path]) -> tuple[type[Plugin], Path]:
    """Return (PluginClass, package_dir) for the named plugin."""
    # 1. Try built-in package
    try:
        mod = importlib.import_module(f"maxbot.plugins.{name}")
        plugin_cls = getattr(mod, "Plugin", None) or getattr(mod, "PLUGIN", None)
        if plugin_cls and issubclass(plugin_cls, Plugin):
            pkg_dir = Path(mod.__file__).parent
            return plugin_cls, pkg_dir
    except ImportError:
        pass

    # 2. Try external search paths
    for base in search_paths:
        candidate = base / name / "__init__.py"
        if candidate.exists():
            mod = _import_from_path(f"maxbot_plugin_{name}", candidate)
            plugin_cls = getattr(mod, "Plugin", None) or getattr(mod, "PLUGIN", None)
            if plugin_cls and issubclass(plugin_cls, Plugin):
                return plugin_cls, candidate.parent

    raise ImportError(
        f"Plugin '{name}' not found. Looked in maxbot.plugins.{name} and "
        f"{[str(p) for p in search_paths]}."
    )


def _read_plugin_context(pkg_dir: Path) -> str | None:
    """Read CONTEXT.md from the plugin's package directory, if present."""
    ctx_path = pkg_dir / "CONTEXT.md"
    if not ctx_path.exists():
        return None
    try:
        return ctx_path.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("Could not read %s: %s", ctx_path, e)
        return None


def load_plugins(ctx: "BotContext") -> list[Plugin]:
    """Discover, instantiate and register all enabled plugins. Returns the list."""
    cfg = ctx.config.plugins
    search_paths = [Path(p) for p in cfg.search_paths]
    capabilities = ctx.shared.setdefault("capabilities", {})
    loaded: list[Plugin] = []
    for name in cfg.enabled:
        try:
            cls, pkg_dir = _discover_plugin_class(name, search_paths)
        except ImportError as e:
            log.error("Skipping plugin '%s': %s", name, e)
            continue
        try:
            plugin = cls(options=cfg.options.get(name, {}))
            plugin.register(ctx)
            loaded.append(plugin)
            label = plugin.name or name
            ctx_md = _read_plugin_context(pkg_dir)
            tg_cmds = list(getattr(plugin, "telegram_commands", []) or [])
            capabilities[label] = {
                "description": plugin.description or "",
                "context_md": ctx_md,
                "options": cfg.options.get(name, {}),
                "telegram_commands": tg_cmds,
            }
            if not ctx_md:
                log.warning(
                    "Plugin '%s' has no CONTEXT.md — capability discovery will be minimal.", label,
                )
            log.info("Plugin loaded: %s — %s", label, plugin.description or "")
        except Exception as e:
            log.exception("Plugin '%s' failed to register: %s", name, e)
    return loaded
