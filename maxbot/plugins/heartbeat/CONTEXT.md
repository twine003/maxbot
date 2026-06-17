# heartbeat plugin

## What it does

Runs a periodic tick (default every 30 min) that:

1. Cleans up old files in `<workspace>/media/` (older than `media_max_age` seconds — default 1 hour).
2. Executes any recurring or one-time tasks (stored in `tasks.json` by the `tasks` plugin) whose interval has elapsed.

Without this plugin, scheduled tasks created via `/tarea` will never fire and the media directory will grow unbounded.

## Commands

None — this plugin only schedules a background job. Tasks are managed via the `tasks` plugin's commands.

## Configuration (`[plugins.heartbeat]`)

| Key | Type | Default | Meaning |
|---|---|---|---|
| `interval_seconds` | int | 1800 | How often the tick fires (30 min). |
| `first_run_after` | int | 60 | Seconds after boot before the first tick. |

```toml
[plugins.heartbeat]
interval_seconds = 1800
first_run_after = 60
```

## Dependencies

- A runner must be configured and active — task prompts are executed by whichever runner is active for the target chat.
- `[bot].allowed_chat_ids` must be non-empty — tasks fire into the first allowed chat id.

## First-time setup

Add `"heartbeat"` to `[plugins].enabled`. Done.

If you have scheduled tasks (`/tarea ...`) but they're not firing, check:
1. `heartbeat` is enabled.
2. `allowed_chat_ids` is set.
3. The log shows `Heartbeat scheduled every N seconds` at boot.
4. The log shows `Heartbeat: tick done` periodically (every `interval_seconds`).

## Day-to-day management

- To pause the heartbeat entirely without uninstalling: remove `"heartbeat"` from `[plugins].enabled` and restart.
- To run the tick more often (e.g. during a debug session): set `interval_seconds = 60` temporarily.
- Tasks are not retried on failure — if a tick errors out, the task's `last_run` is still updated. To rerun, edit `tasks.json` or use `/borrar_tarea` + `/tarea` again.
