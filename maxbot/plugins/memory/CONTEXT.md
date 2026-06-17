# memory plugin

## What it does

Exposes commands that delegate to an external semantic-memory script (`semantic_memory.py`) for vector-store search and write. Also exposes `/documentar` which triggers a session-documenter sub-agent (Claude-only).

## Commands

- `/memoria <query>` — search the semantic memory and report results scored by relevance.
  - Score thresholds: ≥0.55 → confident hit; 0.45–0.55 → medium confidence; <0.45 → no relevant result.
- `/recordar <texto>` — store a detail in semantic memory. The bot picks a slug, name, and type from the text.
- `/documentar` — close the current session: synthesize discoveries + code changes + user feedback, then invoke the `documentar_session` skill. **Only works with the `claude` runner** — Codex has no skills/sub-agents.

## Configuration (`[plugins.memory]`)

| Key | Type | Default | Meaning |
|---|---|---|---|
| `semantic_memory_script` | str | `./scripts/semantic_memory.py` | Path to the embedder/searcher script. Relative paths resolve against `[bot].workspace`. |
| `enable_documentar` | bool | true | If false, `/documentar` is not registered (use on deployments without Claude). |

```toml
[plugins.memory]
semantic_memory_script = "./scripts/semantic_memory.py"
enable_documentar = false
```

## Dependencies

- The semantic_memory.py script must exist and be executable by the bot's user.
- The active runner must be able to run `python <script>` from the workspace cwd.
- For `/documentar`: the Claude runner with the `documentar_session` skill installed in `~/.claude/skills/`.
- The backing vector store (Ollama, Gemini-via-n8n, or whatever the script wraps) must be reachable.

## First-time setup

1. Install / verify `semantic_memory.py` is at the configured path.
2. Test it manually: `python <path> search "test" -n 1 --json` should return JSON without erroring.
3. Add `"memory"` to `[plugins].enabled`.
4. Restart the bot.

If the script doesn't exist, the commands will still register but every invocation will fail at runtime (the runner returns an error). The plugin itself doesn't validate the path at boot.

## Day-to-day management

- Routine search: `/memoria que decidimos con la categoría 14 de banking`
- Save a preference: `/recordar prefiero respuestas cortas sin tablas grandes`
- Close a session: `/documentar` (Claude only)

The system_instruction (when claude is the active runner) already instructs the bot to use semantic memory proactively — meaning it will run `semantic_memory.py` itself, not via `/memoria`, during any non-trivial conversation. `/memoria` is for explicit user-driven searches.

If `/documentar` is disabled (`enable_documentar = false`) the bot will respond to `/documentar` with PTB's default "command not recognized" — there is no graceful fallback. Consider documenting this in the deployment README.
