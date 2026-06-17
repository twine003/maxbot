# banking plugin (example)

An **example** plugin that ships disabled. It shows three reusable patterns and
is meant to be adapted to your own scripts — none of the referenced scripts ship
with maxbot.

## What it does

1. **`/informe_balance`** — spawns a worker that runs the configured `report_script`
   and replies with its output (plus an optional `[ADJUNTO:<file>]` line so the
   connector attaches a generated file).
2. **`/servidores`** — spawns a worker that reads the configured `procedure_doc`
   and follows the steps documented there to report system/server status.
3. **`2fa <code>` text intake** — a *text pre-processor* (not a command). When a
   user sends `2fa <code>` in plain chat, the code is written to `code_intake_file`
   so a waiting sub-agent can pick it up. The pre-processor returns `True`, so the
   worker never sees the text.

## Commands

- `/informe_balance`
- `/servidores`

## Configuration (`[plugins.banking]`)

| Key | Type | Default | Meaning |
|---|---|---|---|
| `report_script` | str | `./scripts/generate_report.py` | Script run by `/informe_balance`. Relative paths resolve against `[bot].workspace`. |
| `procedure_doc` | str | `./scripts/server_status.md` | Doc the runner reads for `/servidores`. |
| `code_intake_file` | str | `./data/code_pending.json` | Where `2fa <code>` text is written. |
| `enable_2fa_intake` | bool | true | If false, the text pre-processor is not registered. |

```toml
[plugins.banking]
report_script = "./scripts/generate_report.py"
procedure_doc = "./scripts/server_status.md"
code_intake_file = "./data/code_pending.json"
enable_2fa_intake = true
```

## Dependencies

- The configured scripts/docs must exist on the host running the bot.
- The active runner needs Bash/Read tools allowed (see `[bot].allowed_tools`).

## First-time setup

1. Create your own `report_script` and `procedure_doc`, or point the options at
   existing ones.
2. Add `"banking"` to `[plugins].enabled`.
3. Restart.

If the configured scripts don't exist, the commands still register but every
invocation fails at runtime (the runner returns the error). The plugin does not
validate paths at boot — keep it disabled if you don't use it.

## Day-to-day management

- Run the report: `/informe_balance`.
- Check servers: `/servidores`.
- Feed a one-time code while a sub-agent waits for it: reply `2fa 123456` in chat.
