# maxbot

A Telegram bot **framework** that puts a coding CLI (Claude Code or Codex) on the
other end of your chat. Pluggable runners, pluggable chat connectors, and
feature **plugins** you turn on and off from a single config file. Cross-platform
(Windows + Linux + macOS).

> The engine is the `maxbot/` Python package. Each real bot is a **deployment**
> in `deployments/<name>/` with its own `bot.toml`, `.env`, and state. Your
> secrets never live in the framework.

## Install (one command)

You need **Python 3.10+**. You do **not** need an AI agent CLI installed up
front — the bot installs and authenticates it for you during setup.

```bash
git clone https://github.com/twine003/maxbot.git
cd maxbot
python install.py
```

The installer creates a virtualenv in `.venv/`, installs maxbot, scaffolds a
deployment in `deployments/my-bot/`, and asks you for **one** thing: your
Telegram bot token (from [@BotFather](https://t.me/BotFather)).

Windows PowerShell equivalent: `.\install.ps1` · Linux/macOS: `./install.sh`

## Run, then finish setup from Telegram

```bash
source .venv/bin/activate            # Windows: .venv\Scripts\activate
maxbot --config deployments/my-bot/bot.toml
```

Everything else is configured by **talking to the bot** — no more files to edit:

1. **Pair it.** The console prints a one-time **pairing code**. Open Telegram,
   message your bot, and send that code. The first chat to send it becomes the
   bot's owner (this sets `ALLOWED_CHAT_IDS` automatically; nobody else can use
   the bot).
2. **Pick an AI agent.** The bot asks whether you want **Claude Code** or
   **Codex**, then installs that CLI for you (via npm).
3. **Authenticate it.** Paste an API key in the chat, or log in via the browser
   — the bot walks you through it.
4. **Write its ALMA.** A short guided wizard (6 questions) writes the bot's
   personality and rules into `system_instruction.md` + a `persona/` folder, and
   applies it live.

Then just chat. Validate the config without connecting any time with `--check`.

> Re-run onboarding from scratch by deleting `deployments/my-bot/setup_state.json`.

## Turning plugins on and off

Plugins are lego pieces. Edit `[plugins].enabled` in your `bot.toml` and restart:

```toml
[plugins]
enabled = ["tasks", "heartbeat"]     # add/remove names here

[plugins.heartbeat]                  # optional per-plugin options
interval_seconds = 1800
```

| Plugin | What it adds | Needs setup? |
|---|---|---|
| `tasks` | `/tarea`, `/tareas`, … schedule prompts to run later | no |
| `heartbeat` | runs scheduled tasks + cleans old media | no |
| `memory` | `/memoria`, `/recordar`, `/documentar` (semantic memory) | yes — your own `semantic_memory.py` |
| `banking` | `/informe_balance`, `/servidores`, one-time code intake (**example**) | yes — your own scripts |

`memory` and `banking` ship as **examples**: their code is there, but they call
external scripts you provide, so they're disabled by default. Each plugin's
options are documented in `maxbot/plugins/<name>/CONTEXT.md`.

## Built-in commands (always available)

`/start` · `/new_session` · `/change_model <name>` · `/status` · `/cancelar` ·
`/capabilities [plugin]` · `/help`

## Project layout

```
maxbot/                  the framework (pip package)
├── core/                config, sessions, tasks, auth
├── runners/             AI engines: claude.py, codex.py
├── connectors/          chat platforms: telegram/
├── workers/             background turn lifecycle
├── handlers/            built-in commands + media
└── plugins/             activatable lego pieces

deployments/
└── example/             template copied to deployments/<name>/ by the installer

docs/                    how to author runners, connectors, and plugins
MAXBOT.md                the framework manifest (deep reference)
```

## Extending maxbot

| To add… | Guide |
|---|---|
| A new AI engine (runner) | [docs/AUTHORING_RUNNERS.md](docs/AUTHORING_RUNNERS.md) |
| A new chat platform (connector) | [docs/AUTHORING_CONNECTORS.md](docs/AUTHORING_CONNECTORS.md) |
| A new feature (plugin) | [docs/AUTHORING_PLUGINS.md](docs/AUTHORING_PLUGINS.md) |

A minimal plugin:

```python
# my_plugin/plugin.py
from telegram.ext import CommandHandler
from maxbot.plugins.base import Plugin

class MyPlugin(Plugin):
    name = "my_plugin"
    description = "What my plugin does"

    def register(self, ctx) -> None:
        ctx.connector.app.add_handler(CommandHandler("hola", self._hola))

    async def _hola(self, update, context):
        await update.message.reply_text("hola!")
```

Drop it in a folder, point `[plugins].search_paths` at it, add `"my_plugin"` to
`[plugins].enabled`, and restart.

## License

MIT — see [LICENSE](LICENSE).
