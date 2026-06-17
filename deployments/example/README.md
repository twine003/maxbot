# Example deployment

This folder is a **template**. The installer copies it to `deployments/<your-name>/`
so your config and state live separately from the framework code.

## Files

| File | Purpose | In git? |
|---|---|---|
| `bot.toml` | Deployment config: runners, plugins, limits. | yes (template) |
| `.env.example` | Template for secrets. Copy to `.env`. | yes |
| `.env` | Your real token + chat id. | **no — git-ignored** |
| `system_instruction.md` | The bot's persona / house rules. | yes (template) |
| `start.cmd` / `start.sh` | Launch the bot. | yes |
| `restart.cmd` / `restart.sh` | Bounce the bot (used for self-restart). | yes |
| `sessions.json`, `tasks.json`, `media/`, `logs/` | Runtime state, created at run time. | **no — git-ignored** |

## Configure

1. `cp .env.example .env` (the installer does this) and fill in
   `TELEGRAM_BOT_TOKEN` and `ALLOWED_CHAT_IDS`.
2. Edit `system_instruction.md` to give the bot its personality.
3. In `bot.toml`, edit `[plugins].enabled` to turn features on/off.

## Run

```bash
maxbot --config bot.toml
# or
./start.sh        # Linux/macOS
start.cmd         # Windows
```

## Run unattended

- **Windows:** Task Scheduler → new task → action `start.cmd` in this folder,
  "Run whether user is logged on or not".
- **Linux:** point a systemd unit's `ExecStart` at `start.sh`, or run it under
  your process manager of choice.
