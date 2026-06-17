# Authoring a Runner

A **Runner** is an adapter for an AI engine — typically a CLI like `claude`, `codex`, or a hypothetical `gemini`. Runners own:

- Locating the binary on disk (`find_executable`)
- Building the per-turn subprocess command (`build_command`)
- Streaming the turn's output into the Telegram live message (`run_turn`)
- Updating session bookkeeping (`sessions.mark_initialized` when a turn succeeds)

A Runner does NOT own chat state, the Telegram bot, or plugin behavior. Those come in via `BotContext` / `TelegramConnector`.

## 1. The Runner contract

```python
# maxbot/runners/base.py
class Runner(ABC):
    name: str = ""
    label: str = ""

    def __init__(self, config: BotConfig, runner_config: RunnerConfig, sessions: SessionStore): ...

    @abstractmethod
    def find_executable(self) -> str: ...

    @abstractmethod
    def build_command(self, prompt: str, session_id: str | None, resume: bool) -> list[str]: ...

    @abstractmethod
    async def run_turn(self, connector, chat_id, prompt, session_id, resume, worker) -> RunnerResult: ...
```

And a result object:

```python
@dataclass
class RunnerResult:
    text: str
    session_id: str | None = None
    error: bool = False
```

## 2. Minimum viable runner

```python
# maxbot/runners/gemini.py
import asyncio, shutil
from .base import Runner, RunnerResult


class GeminiRunner(Runner):
    name = "gemini"
    label = "Gemini"

    def find_executable(self):
        return shutil.which("gemini") or "gemini"

    def build_command(self, prompt, session_id, resume):
        full = self.get_system_instruction() + prompt
        cmd = [self.executable, "chat", full]
        if resume and session_id:
            cmd += ["--session", session_id]
        return cmd

    async def run_turn(self, connector, chat_id, prompt, session_id, resume, worker):
        cmd = self.build_command(prompt, session_id, resume)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.config.workspace),
        )
        stdout, _ = await proc.communicate()
        text = stdout.decode("utf-8", errors="replace").strip()
        if proc.returncode == 0:
            self.sessions.mark_initialized(chat_id, "gemini", session_id)
            return RunnerResult(text or "(sin respuesta)", session_id=session_id)
        return RunnerResult(f"Error: gemini exited {proc.returncode}", error=True)
```

That's enough to chat with a runner. It won't stream live — `run_turn` returns the result in one shot — but it will work.

## 3. Adding streaming (the polished version)

For a ChatGPT-style live message that grows letter-by-letter, your `run_turn` must:

1. Send an initial "🤔 Pensando..." message and store its id in `worker.stream_msg_id`.
2. As stdout lines arrive, parse them and:
   - If a tool/activity event → push `worker.stream_msg_text = ""` + edit the live message with `🛠️ <activity>`.
   - If a text delta arrives → append to `worker.stream_msg_text`, set `worker.stream_activity_only = False`, call `stream_update(...)` (throttled).
3. On final result → force a `stream_update(force=True)` to flush.

Use the helpers in `maxbot.connectors.telegram.ui.streaming`:

```python
from maxbot.connectors.telegram.ui.streaming import (
    reset_stream, safe_edit, stream_update,
)
```

Look at [`maxbot/runners/claude.py`](../maxbot/runners/claude.py) and [`maxbot/runners/codex.py`](../maxbot/runners/codex.py) for the full pattern.

## 4. System instructions

Each runner can have its own system instruction (because different CLIs respond differently to "Sos X" — Codex famously refuses to execute when given identity). Override via `bot.toml`:

```toml
[runners.gemini]
system_instruction = "@gemini_system.md"   # @ prefix = read from file
# OR
system_instruction = """
Eres un asistente directo. No te presentes.
"""
```

The default is `config.system_instruction` (bot-wide). Use `self.get_system_instruction()` to retrieve.

## 5. Sessions

Sessions are per (chat_id, runner_name). When a turn succeeds, call:

```python
self.sessions.mark_initialized(chat_id, self.name, session_id)
```

`session_id` may come from the CLI (codex emits a `thread.started` event with `thread_id`) or be one you generated yourself (claude takes `--session-id`).

## 6. Registering the runner

Add it to `maxbot/app.py`:

```python
_RUNNER_CLASSES = {
    "claude": ClaudeRunner,
    "codex": CodexRunner,
    "gemini": GeminiRunner,   # ← new
}
```

Enable it in `bot.toml`:

```toml
[runners.gemini]
enabled = true
cli = "auto"  # or an explicit path
```

The bot will now accept `/change_model gemini`.

## 7. Cross-platform CLI lookup

`maxbot/runners/_common.py` ships two helpers you should reuse:

- `find_in_user_local_bin("foo")` — searches `~/.local/bin` and (on Windows) every `C:\Users\<user>\.local\bin`.
- `find_in_npm_globals("foo")` — searches npm global install dirs on Windows.

If your CLI lives elsewhere (Homebrew, asdf, etc.) add a similar helper rather than hardcoding paths in `find_executable`.

## 8. The HOME problem on Windows services

When running as a Windows SYSTEM service, `HOME`/`USERPROFILE` point to `C:\Windows\system32\config\systemprofile`, which breaks CLIs that read their config from `~/.claude` or `~/.codex`. Use `patched_env(self.executable)` to fix this — it derives the right home from the CLI's path.

```python
from maxbot.runners._common import patched_env

env = patched_env(self.executable, drop_keys=("CLAUDECODE",))
```

## 9. Checklist

- [ ] Class extends `maxbot.runners.base.Runner`
- [ ] `name` and `label` set
- [ ] `find_executable` returns an existing path when possible
- [ ] `build_command` accepts `(prompt, session_id, resume)`
- [ ] `run_turn` calls `sessions.mark_initialized(...)` on success
- [ ] Streaming uses the helpers in `connectors/telegram/ui/streaming.py`
- [ ] `patched_env(self.executable)` is used for subprocess env
- [ ] Errors are returned as `RunnerResult(..., error=True)`, not raised
- [ ] Registered in `_RUNNER_CLASSES` in `app.py`
