# tasks plugin

## What it does

Lets the user schedule recurring and one-time prompts that will be executed by the bot's heartbeat. Tasks are stored in `<workspace>/tasks.json`.

The plugin itself only handles CRUD via Telegram commands. The actual execution of tasks happens in the `heartbeat` plugin (which must also be enabled for tasks to fire).

## Commands

- `/tarea <descripción>` — create a recurring task. Frequency is auto-detected from the text ("cada 30min", "cada 2h", "diario"). Default if not detected: 30 min.
  - Example: `/tarea Revisá el estado de los servidores cada 6h`
- `/tarea_once <descripción>` — schedule a one-shot prompt that fires at the next heartbeat (within 30 min).
- `/tareas` — list all recurring + one-time tasks with their state, frequency, last-run timestamp.
- `/borrar_tarea <id>` — delete a task by id (id prefix is enough — first 4-8 chars).
- `/pausar_tarea <id>` — toggle a recurring task between enabled and paused.

## Configuration (`[plugins.tasks]`)

This plugin currently takes no options. The storage path is determined by `[bot].workspace`.

## Dependencies

- `<workspace>/tasks.json` (auto-created).
- The `heartbeat` plugin must be enabled for scheduled tasks to actually run.

## First-time setup

Nothing to configure. Just include `"tasks"` in `[plugins].enabled` and (recommended) `"heartbeat"` too:

```toml
[plugins]
enabled = ["tasks", "heartbeat"]
```

## Day-to-day management

- Create: `/tarea Revisá las alertas de sentry cada 1h`
- List: `/tareas`
- Pause if you don't want it firing tonight: `/pausar_tarea <id>`
- Resume: `/pausar_tarea <id>` again
- Delete forever: `/borrar_tarea <id>`

The `last_run` timestamp in `tasks.json` is updated by `heartbeat` each tick. If a task hasn't run for a long time, check that `heartbeat` is enabled and its `interval_seconds` is shorter than your task's `interval_minutes`.
