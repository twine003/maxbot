# maxbot

A Telegram bot **framework** that puts a coding CLI (Claude Code or Codex) on the
other end of your chat. Pluggable runners, pluggable chat connectors, and
feature **plugins** you turn on and off from a single config file. Cross-platform
(Windows + Linux + macOS).

> The engine is the `maxbot/` Python package. Each real bot is a **deployment**
> in `deployments/<name>/` with its own `bot.toml`, `.env`, and state. Your
> secrets never live in the framework.

## Install (one command)

You need **Python 3.10+** and at least one CLI runner on your PATH — the
[`claude`](https://docs.claude.com/en/docs/claude-code) CLI and/or the `codex` CLI.

```bash
git clone https://github.com/USER/maxbot.git
cd maxbot
python install.py
```

That creates a virtualenv in `.venv/`, installs maxbot into it, and scaffolds a
ready-to-edit deployment in `deployments/my-bot/`.

Windows PowerShell equivalent: `.\install.ps1` · Linux/macOS: `./install.sh`

Prefer to install just the package? `pip install git+https://github.com/USER/maxbot.git`

## Configure (2 fields)

Open `deployments/my-bot/.env` and fill in two values:

```env
TELEGRAM_BOT_TOKEN=<token from @BotFather>
ALLOWED_CHAT_IDS=<your numeric chat id, from @userinfobot>
```

`ALLOWED_CHAT_IDS` is an allow-list — only those chats can talk to the bot.

## Run

```bash
source .venv/bin/activate            # Windows: .venv\Scripts\activate
maxbot --config deployments/my-bot/bot.toml
```

Validate the config first without connecting: add `--check`.

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
