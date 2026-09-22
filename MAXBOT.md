# maxbot — Framework Manifest

This file is the canonical description of the maxbot framework for both humans and LLMs. It explains what maxbot is, what's installed, how to configure it for the first time, and how to manage it day-to-day.

When the bot runs `/capabilities` (or `/help`), the first section returned is this file's "Summary" + "Active capabilities". Plugins contribute their own `CONTEXT.md` files which are concatenated below.

---

## Summary

maxbot is a Telegram bot framework built around three pluggable concepts:

- **Runners** — AI engines (subprocess-based: Claude CLI, Codex CLI, …).
- **Connectors** — Chat-platform adapters (Telegram today; Discord/Slack via the same ABC).
- **Plugins** — Feature bundles activated from `bot.toml` (tasks, heartbeat, memory, banking, …).

A **deployment** lives in `deployments/<name>/`, owns its `bot.toml` + `.env` + state files, and is what gets shipped to a Windows machine or a Linux server.

> **`workspace` vs `state_dir`.** `workspace` is where the runner is launched — point it at
> the project the bot works on, so that project's own agent files (`CLAUDE.md`, `AGENTS.md`)
> load automatically. `state_dir` is where this deployment keeps its own files (sessions,
> tasks, media, setup state); it defaults to `workspace`. Set it when the deployment folder
> lives **outside** the project the bot operates on, so the bot's runtime files do not end up
> inside that project.

## Architecture

```
maxbot/                          ← framework (the python package)
├── core/                        cross-cutting: auth, sessions, tasks, pending_work
├── runners/                     AI engines
├── connectors/                  chat platforms
│   └── telegram/                python-telegram-bot adapter (reference)
├── workers/                     background turn lifecycle
├── handlers/                    built-in commands + media handlers
└── plugins/                     activatable lego pieces

deployments/<name>/              one folder per environment
├── bot.toml                     deployment config
├── .env                         secrets (git-ignored)
├── system_instruction.md        prompt prefix for runners that accept one
├── sessions.json                runtime: per-chat session ids
├── tasks.json                   runtime: scheduled tasks
└── logs/                        runtime: stdout/stderr
```

## Built-in commands

These are always available regardless of which plugins are enabled:

- `/start` — initial help. Shows the active model.
- `/new_session` — drop current conversation, start fresh on the active runner.
- `/change_model <name>` — switch between configured runners (e.g. `claude`, `codex`).
- `/status` — what the current worker is doing (if any).
- `/cancelar` — kill the in-flight worker for this chat.
- `/capabilities [plugin]` — show this manifest + each plugin's `CONTEXT.md`. Without args, lists everything; with a plugin name, deep-dives that one.
- `/help` — alias for `/capabilities`.

## How to deploy maxbot for the first time

The fastest path is the one-command installer (see README). Manual steps:

### Windows

```cmd
git clone <repo> c:\Users\you\maxbot
cd c:\Users\you\maxbot
python -m venv .venv
.venv\Scripts\activate
pip install -e .

REM scaffold a deployment from the template
xcopy /E /I deployments\example deployments\my-bot
cd deployments\my-bot
copy .env.example .env
REM edit .env: TELEGRAM_BOT_TOKEN (the only secret needed up front)
start.cmd
REM then finish setup from Telegram — see "First-run onboarding" below
```

To survive logouts, pin `start.cmd` as a Windows Scheduled Task with "Run whether user is logged on or not".

### Linux / macOS

```bash
git clone <repo> ~/maxbot
cd ~/maxbot
python install.py            # venv + install + scaffold deployments/my-bot
# edit deployments/my-bot/.env
./deployments/my-bot/start.sh
```

To run unattended, point a systemd unit's `ExecStart` at `start.sh`.

## First-run onboarding (pairing + agent setup)

A fresh bot configures itself by chatting with you — the only thing you set in a
file is `TELEGRAM_BOT_TOKEN`. State is tracked in `<workspace>/setup_state.json`
and the wizard runs at a high handler priority (group -50), intercepting all
messages until setup is `done`, then disabling itself.

Stages:

1. **pairing** — on boot the console prints a one-time `PAIRING CODE`. The first
   chat that sends it to the bot is claimed as the owner and persisted as
   `paired_chat_id` (merged into `allowed_chat_ids` on every boot). An empty
   allowlist denies everyone, so an unpaired bot never answers strangers.
2. **agent_select** — the bot asks for Claude Code or Codex and sets that as the
   active model for the owner chat.
3. **agent_install** — runs `npm install -g <package>` for the chosen CLI
   (`@anthropic-ai/claude-code` or `@openai/codex`). If `npm` is missing it asks
   the user to install Node.js first.
4. **agent_auth** — the user pastes an API key (written to the deployment `.env`
   as `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`) or confirms a browser login.
5. **alma** — a 6-question wizard writes `system_instruction.md` plus a
   `persona/` folder (`ALMA.md`, `REGLAS.md`, `CONTEXTO.md`) and applies the new
   system instruction live (runners read `config.system_instruction` per turn).

Re-run onboarding by deleting `setup_state.json`.

## How to activate / deactivate plugins

Edit `bot.toml` in the deployment folder:

```toml
[plugins]
enabled = ["tasks", "heartbeat"]   # add or remove names here

[plugins.tasks]                    # optional per-plugin options
# (see each plugin's CONTEXT.md for available keys)
```

Then restart the bot. The loader logs each plugin it activates:

```
maxbot.plugins.loader: Plugin loaded: tasks — /tarea, /tarea_once, …
```

A plugin that fails to load is logged + skipped — the bot keeps running with the rest.

## How to configure a runner

Each `[runners.<name>]` block in `bot.toml` accepts:

- `enabled` (bool) — turn this runner on/off.
- `cli` (str) — `"auto"` for autodiscovery, or an explicit path to the binary.
- `system_instruction` (str) — per-runner system prompt. Use `""` for runners that misbehave with identity prompts (e.g. codex). Use `"@file.md"` to read from a separate file. Falls back to `[bot].system_instruction`.

## Adding new capabilities

| To add… | See doc | Where it lives |
|---|---|---|
| A new AI engine | [docs/AUTHORING_RUNNERS.md](docs/AUTHORING_RUNNERS.md) | `maxbot/runners/<name>.py` |
| A new chat platform | [docs/AUTHORING_CONNECTORS.md](docs/AUTHORING_CONNECTORS.md) | `maxbot/connectors/<name>/` |
| A new feature / command bundle | [docs/AUTHORING_PLUGINS.md](docs/AUTHORING_PLUGINS.md) | `maxbot/plugins/<name>/` or external `<path>/<name>/` |

## How to manage the running bot

| Task | Windows | Linux |
|---|---|---|
| Start | `start.cmd` (or scheduled task) | `./start.sh` / `systemctl start maxbot` |
| Stop | `taskkill /F /IM python.exe` (find the right PID via `tasklist`) | `systemctl stop maxbot` |
| Restart | `restart.cmd <chat_id>` (graceful, sends marker) | `./restart.sh <chat_id>` / `systemctl restart maxbot` |
| Tail logs | `Get-Content -Wait logs\bot.log` | `journalctl -u maxbot -f` |
| Disable a plugin without restart | not supported — edit `bot.toml` + restart | same |

**Two bots cannot share the same Telegram token.** If you see `409 Conflict` in the log, another `getUpdates` poller is running. Find and stop it.

## Troubleshooting

- **`Bootstrap failed`** at startup → config error. Run `maxbot --config bot.toml --check` to validate without polling.
- **`Missing TELEGRAM_BOT_TOKEN`** → not in `.env`, env var, or `[bot].token` of `bot.toml`.
- **`409 Conflict`** in log → two bots polling the same token; kill the duplicate.
- **`No conversation found`** in claude errors → the session id stored in `sessions.json` was deleted on the claude side. Send `/new_session` to recreate.
- **Codex greets every turn instead of executing** → `runners.codex.system_instruction` is non-empty. Set it to `""`.

## Runtime state files

All under `<workspace>/` (defaults to the deployment folder):

- `sessions.json` — per-chat active model + per-runner session ids.
- `tasks.json` — recurring + one-time tasks (managed by the `tasks` plugin).
- `pending_work.json` — work-to-resume across a restart (transient).
- `media/` — downloaded photos/voice/documents (auto-cleaned by `heartbeat` after `media_max_age`).
- `logs/` — stdout/stderr (created by deployment scripts).
- `restart_marker` — created by `restart.cmd`/`restart.sh` to signal the new process which chat to greet.

## Self-description protocol

When this bot is asked "what can you do?" or "what's installed?", the answer it should give is:

1. Read this file (`MAXBOT.md`) for framework-level context.
2. Read `maxbot/plugins/<name>/CONTEXT.md` for each plugin listed in `[plugins].enabled`.
3. Summarize, mentioning the active runner, the enabled plugins, and the user-visible commands.

If `include_capabilities_in_system_instruction` is true in `bot.toml`, this manifest is prepended to the runner's system prompt automatically at boot — no `/capabilities` needed.
