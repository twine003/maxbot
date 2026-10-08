"""One complete, non-streaming turn — for callers that are not a Telegram chat.

`run_turn` drives a live Telegram message; background callers (the heartbeat
plugin, voice assistants, bridges) only want the final text. This is that path,
shared so each of them does not grow its own copy of the event parsing.

    result = await run_oneshot(ctx.config, ctx.sessions, ctx.runners,
                               prompt, session_key="alexa_123", model="claude")

`session_key` is any string: sessions.json is keyed by it, so a caller gets its
own conversation thread (e.g. "heartbeat_<task>", "alexa_<owner>").

`restrict` (Claude only) narrows what the turn may do, enforced by the CLI
rather than asked for in the prompt:
    allowed_tools     replaces [bot].allowed_tools for this turn
    disallowed_tools  hard deny list (wins over any allow rule in Claude settings)
    strict_mcp        load no MCP servers at all
    model             CLI model for this turn only (e.g. a faster one for voice)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ._common import CLAUDE_ENV_DROP, CLAUDE_ENV_DROP_PREFIXES, patched_env

if TYPE_CHECKING:
    from ..config import BotConfig
    from ..core.sessions import SessionStore
    from .base import RunnerRegistry

log = logging.getLogger(__name__)


@dataclass
class Restrict:
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    strict_mcp: bool = True
    model: str | None = None


@dataclass
class OneshotResult:
    text: str
    session_id: str | None = None
    error: bool = False
    elapsed_ms: int = 0


async def run_oneshot(
    config: "BotConfig",
    sessions: "SessionStore",
    runners: "RunnerRegistry",
    prompt: str,
    session_key: str,
    model: str,
    *,
    restrict: Restrict | None = None,
    timeout: float | None = None,
) -> OneshotResult:
    started = time.monotonic()
    timeout = timeout or config.timeout_seconds
    sid, resume = sessions.get_session(session_key, model)
    runner = runners.get(model)

    if restrict is not None and model != "claude":
        # Codex runs one app-server per bot with a process-wide sandbox; a per-turn
        # restriction cannot be enforced there, so refuse rather than pretend.
        return OneshotResult(f"El runner {model} no admite un perfil restringido.", error=True)

    # Persistent backends (Codex app-server) drive turns over a live connection.
    if restrict is None and hasattr(runner, "run_oneshot"):
        text, thread_id = await runner.run_oneshot(prompt, sid, resume)
        if thread_id:
            sessions.mark_initialized(session_key, model, thread_id)
        return OneshotResult(text, thread_id, text.startswith("Error de Codex"),
                             int((time.monotonic() - started) * 1000))

    if model == "claude":
        cmd = runner.build_command(prompt, sid, resume, stream=True,
                                   allowed_tools=restrict.allowed_tools if restrict else None)
        if restrict:
            for tool in restrict.disallowed_tools:
                cmd += ["--disallowedTools", tool]
            if restrict.strict_mcp:
                cmd += ["--strict-mcp-config"]
            if restrict.model:
                cmd += ["--model", restrict.model]
        env = patched_env(runner.executable, drop_keys=CLAUDE_ENV_DROP,
                          drop_prefixes=CLAUDE_ENV_DROP_PREFIXES)
    else:
        cmd = runner.build_command(prompt, sid, resume)
        env = patched_env(runner.executable)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(config.workspace),
        env=env,
        limit=10 * 1024 * 1024,
    )

    result_text: str | None = None
    error = False
    current_session_id = sid
    try:
        while True:
            try:
                raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                error = True
                break
            if not raw_line:
                break
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if model == "codex":
                if event.get("type") == "thread.started":
                    current_session_id = event.get("thread_id") or current_session_id
                    continue
                if event.get("type") == "item.completed":
                    item = event.get("item", {})
                    if item.get("type") == "agent_message":
                        result_text = item.get("text", "")
                    elif item.get("type") == "error":
                        result_text = f"Error de Codex: {item.get('message', 'unknown error')}"
                        error = True
                    continue
                if event.get("type") == "turn.completed":
                    if current_session_id:
                        sessions.mark_initialized(session_key, model, current_session_id)
                    break
                if event.get("type") == "turn.failed":
                    result_text = f"Error de Codex: {event.get('error', {}).get('message', 'unknown error')}"
                    error = True
                    break
            else:  # claude
                if event.get("type") == "result":
                    result_text = event.get("result", "")
                    error = bool(event.get("is_error"))
                    current_session_id = event.get("session_id") or current_session_id
                    sessions.mark_initialized(session_key, model, current_session_id)
                    break
    except Exception as e:
        proc.kill()
        await proc.wait()
        log.error("oneshot turn error: %s", e)
        error = True

    await proc.wait()
    return OneshotResult(result_text or "(sin respuesta)", current_session_id, error or result_text is None,
                         int((time.monotonic() - started) * 1000))
