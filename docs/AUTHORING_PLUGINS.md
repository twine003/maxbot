# Authoring a Plugin

Plugins are the **lego pieces** of maxbot. They extend the bot with new commands, scheduled jobs, text pre-processors and shared state. Each plugin is independently activatable from `bot.toml`.

## 1. Anatomy of a plugin

A plugin is a Python package with this layout:

```
my_plugin/
├── __init__.py     # re-exports the Plugin class
├── plugin.py       # the actual implementation
└── CONTEXT.md      # capabilities manifest (REQUIRED for first-class plugins)
```

The `__init__.py` MUST expose either `Plugin` or `PLUGIN` so the loader finds it:

```python
# my_plugin/__init__.py
from .plugin import MyPlugin as Plugin
__all__ = ["Plugin"]
```

### `CONTEXT.md` — the capability manifest

Every plugin SHOULD ship a `CONTEXT.md` next to `plugin.py`. The loader reads it at boot and exposes it via:

- `ctx.shared['capabilities'][<name>]['context_md']` — programmatically.
- `/capabilities <name>` — to the user via Telegram.
- Optionally to the LLM at boot — if the deployment sets `include_capabilities_in_system_instruction = true` in `bot.toml`, MAXBOT.md + every plugin's CONTEXT.md is appended to the runner's system prompt.

A plugin without `CONTEXT.md` will load (warning logged), but it won't be discoverable via `/capabilities` and the LLM won't know about it unless the user explicitly invokes its commands.

Use this template:

```markdown
# my_plugin

## What it does
[1-2 line summary]

## Commands
- `/foo <arg>` — what it does
- `/bar` — what it does

## Configuration (`[plugins.my_plugin]`)
| Key | Type | Default | Meaning |
|---|---|---|---|
| `option_a` | str | `"x"` | … |

## Dependencies
- External scripts, services, files, credentials this plugin needs.

## First-time setup
1. Step 1
2. Step 2

## Day-to-day management
- How to use the commands.
- How to disable, debug, troubleshoot.
```

## 2. The Plugin contract

```python
# my_plugin/plugin.py
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from maxbot.context import BotContext
from maxbot.plugins.base import Plugin


class MyPlugin(Plugin):
    name = "my_plugin"             # unique — matches the bot.toml entry
    description = "Saluda al usuario y guarda el contador en shared state"

    def register(self, ctx: BotContext) -> None:
        app = ctx.connector.app

        async def cmd_hola(update: Update, _: ContextTypes.DEFAULT_TYPE):
            if not ctx.is_allowed(update):
                return
            count = ctx.shared.setdefault("my_plugin", {}).get("count", 0) + 1
            ctx.shared["my_plugin"]["count"] = count
            await update.message.reply_text(f"hola! (#{count})")

        app.add_handler(CommandHandler("hola", cmd_hola))

    def shutdown(self, ctx: BotContext) -> None:
        # Optional. Called when the bot shuts down cleanly.
        pass
```

That's the whole contract:

- **`name`** — unique string, must match an entry in `[plugins].enabled` in `bot.toml`.
- **`description`** — one-liner shown in logs at registration time.
- **`register(ctx)`** — wire up your commands, jobs, text preprocessors. Called once at boot.
- **`shutdown(ctx)`** — optional cleanup hook.

The constructor receives `options: dict` — the body of `[plugins.my_plugin]` in `bot.toml`. Access via `self.options`.

## 3. What you get from `BotContext`

| Attribute | Use it for |
|---|---|
| `ctx.config` | Read deployment config (`workspace`, `allowed_chat_ids`, etc) |
| `ctx.connector` | The active chat connector — see [AUTHORING_CONNECTORS.md](AUTHORING_CONNECTORS.md) |
| `ctx.connector.bot` | Raw PTB bot for Telegram-specific calls |
| `ctx.connector.app` | Raw PTB Application — `add_handler`, `job_queue` |
| `ctx.sessions` | Per-chat session state (`get_active_model`, `mark_initialized`, …) |
| `ctx.tasks_store` | Task persistence (used by `tasks` + `heartbeat` plugins) |
| `ctx.workers` | `ctx.workers.spawn(chat_id, prompt)` → fire a runner turn |
| `ctx.runners` | `ctx.runners.get(name)`, `ctx.runners.all_names()` |
| `ctx.shared` | Free-form dict for cross-plugin state (key per plugin name recommended) |
| `ctx.is_allowed(update)` | Helper — enforces the allowed_chat_ids check |

## 4. Common patterns

### 4.a — Adding a command that fires a worker

```python
async def cmd_informe(update, _):
    if not ctx.is_allowed(update): return
    chat_id = update.effective_chat.id
    ctx.workers.spawn(chat_id, "Lee X y reportame Y.")
    await update.message.reply_text("Procesando…")

app.add_handler(CommandHandler("informe", cmd_informe))
```

### 4.b — Intercepting text BEFORE it goes to the worker

Useful for "magic words" like `2fa <code>` that should NOT be sent to the LLM.

```python
async def my_preprocessor(update, _context, ctx) -> bool:
    text = (update.message.text or "").strip()
    if not text.startswith("!quick "):
        return False
    cmd = text.removeprefix("!quick ")
    # do something fast, locally
    await update.message.reply_text(f"OK: {cmd}")
    return True   # IMPORTANT: True stops further processing

ctx.shared.setdefault("text_preprocessors", []).append(my_preprocessor)
```

Pre-processors are tried in registration order; returning `True` consumes the message.

### 4.c — Scheduled jobs (use `job_queue`)

```python
async def my_tick(_):
    await do_periodic_thing(ctx)

ctx.connector.job_queue.run_repeating(my_tick, interval=600, first=30, name="my_plugin_tick")
```

### 4.d — Reading plugin options

```toml
[plugins.my_plugin]
threshold = 0.5
notify = "always"
```

```python
class MyPlugin(Plugin):
    def register(self, ctx):
        threshold = self.options.get("threshold", 1.0)
        notify = self.options.get("notify", "errors")
```

## 5. Where plugins live

Two locations are searched, in order:

1. **Built-in**: `maxbot.plugins.<name>` — for plugins shipped in this repo.
2. **External**: paths listed in `[plugins].search_paths` in `bot.toml`. Each path is a directory containing `<name>/__init__.py`.

Example of an external plugin in a deployment:

```
deployments/example/
├── bot.toml
└── plugins/
    └── my_quirks/
        ├── __init__.py
        └── plugin.py
```

```toml
# bot.toml
[plugins]
enabled = ["tasks", "memory", "will_quirks"]
search_paths = ["./plugins"]
```

## 6. Activation

In `bot.toml`:

```toml
[plugins]
enabled = ["tasks", "banking", "memory", "heartbeat", "my_plugin"]

[plugins.my_plugin]
threshold = 0.5
```

Restart the bot. The loader logs each successful registration:

```
Plugin loaded: my_plugin — Saluda al usuario y guarda el contador
```

A failing plugin logs the error and the bot keeps running — other plugins are unaffected.

## 7. Checklist before publishing

- [ ] Class extends `maxbot.plugins.base.Plugin`
- [ ] `name` and `description` are set
- [ ] `CONTEXT.md` written and placed next to `plugin.py` (capabilities, config, setup, management)
- [ ] All commands check `ctx.is_allowed(update)` first
- [ ] Heavy work goes through `ctx.workers.spawn(...)` — don't block in handlers
- [ ] Persistent state lives in a JSON file under `ctx.config.workspace` (or in shared state if ephemeral)
- [ ] Plugin options have sensible defaults (work without any `[plugins.my_plugin]` block)
- [ ] Errors logged, not raised — failures must not crash the bot
