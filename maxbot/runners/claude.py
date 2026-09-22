"""Claude CLI runner — adapter for `claude -p ... --output-format=stream-json`."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from ..connectors.telegram.ui.activity import extract_claude_activity
from ..connectors.telegram.ui.streaming import (
    reset_stream, safe_edit, stream_update,
)
from .base import Runner, RunnerResult
from ._common import (
    CLAUDE_ENV_DROP,
    CLAUDE_ENV_DROP_PREFIXES,
    find_in_user_local_bin,
    patched_env,
    resolve_windows_wrapper,
)

if TYPE_CHECKING:
    from ..connectors.telegram.connector import TelegramConnector
    from ..workers.state import WorkerState

log = logging.getLogger(__name__)
_RATE_LIMIT_RE = re.compile(r"retry after\s+(\d+)")


class ClaudeRunner(Runner):
    name = "claude"
    label = "Claude"

    def find_executable(self) -> str:
        found = find_in_user_local_bin("claude")
        if not found:
            found = shutil.which("claude")
        if not found:
            log.error("claude executable not found")
            return "claude"
        # En Windows, `claude` resuelve al .cmd de npm y eso mete cmd.exe en el
        # medio, que corta el comando en el primer salto de línea del prompt.
        real = resolve_windows_wrapper(found)
        if real != found:
            log.info("claude: uso el binario real %s (evito el wrapper .cmd)", real)
        return real

    def build_command(self, prompt: str, session_id: str | None, resume: bool, *, stream: bool = True) -> list[str]:
        if resume and session_id:
            cmd = [self.executable, "-p", prompt, "--resume", session_id]
        else:
            sid = session_id or str(uuid.uuid4())
            cmd = [self.executable, "-p", prompt, "--session-id", sid]
        # System instruction goes via --append-system-prompt-file rather than
        # being prepended to the prompt: it stays out of the user-visible turn,
        # appends to Claude's own system prompt, and avoids hitting argv/command
        # -line length limits (the manifest + ALMA can be many KB).
        sp_file = self._write_system_prompt_file()
        if sp_file:
            cmd += ["--append-system-prompt-file", str(sp_file)]
        # Pin a specific model if configured (e.g. claude-opus-4-8 / sonnet).
        if self.runner_config.model:
            cmd += ["--model", self.runner_config.model]
        if stream:
            # --include-partial-messages → emits content_block_delta events letter-by-letter
            # (Anthropic SDK format), enabling ChatGPT/clawdbot-style live streaming.
            cmd += ["--output-format", "stream-json", "--verbose", "--include-partial-messages"]
        for tool in self.config.allowed_tools:
            cmd += ["--allowedTools", tool]
        return cmd

    def _write_system_prompt_file(self) -> Path | None:
        """Persist the current system instruction to a workspace file for
        --append-system-prompt-file. Rewritten each turn so live changes (e.g.
        the ALMA wizard updating it) take effect. Returns None if empty."""
        text = self.get_system_instruction()
        if not text or not text.strip():
            return None
        path = self.config.state_dir / ".maxbot_claude_system.md"
        try:
            path.write_text(text, encoding="utf-8")
        except OSError as e:
            log.warning("could not write system prompt file: %s", e)
            return None
        return path

    async def run_turn(
        self,
        connector: "TelegramConnector",
        chat_id: int,
        prompt: str,
        session_id: str | None,
        resume: bool,
        worker: "WorkerState",
    ) -> RunnerResult:
        cmd = self.build_command(prompt, session_id, resume, stream=True)
        env = patched_env(
            self.executable,
            drop_keys=CLAUDE_ENV_DROP,
            drop_prefixes=CLAUDE_ENV_DROP_PREFIXES,
        )
        bot = connector.bot

        for attempt in range(1, self.config.max_retries + 1):
            log.info("Worker[%d] claude attempt %d: %s", chat_id, attempt, prompt[:80])

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.config.workspace),
                env=env,
                limit=10 * 1024 * 1024,
            )
            worker.proc = proc
            worker.start_time = time.monotonic()
            worker.last_activity = "Pensando..."
            worker.last_progress_sent = time.monotonic()
            # Un reintento (sesión perdida, formato de salida, etc.) NO debe
            # dejar otro "Pensando..." colgado en el chat: el usuario ve dos
            # burbujas para un solo mensaje suyo y parece que el bot se duplicó.
            # Se reutiliza la burbuja que ya está puesta.
            msg_previo = worker.stream_msg_id
            reset_stream(worker)
            worker._seen_text_len = 0

            try:
                if msg_previo is not None:
                    try:
                        await safe_edit(bot, chat_id, msg_previo, "🤔 Pensando...")
                        worker.stream_msg_id = msg_previo
                    except Exception as e:
                        log.warning("no pude reusar el mensaje de progreso: %s", e)
                if worker.stream_msg_id is None:
                    initial = await bot.send_message(chat_id=chat_id, text="🤔 Pensando...")
                    worker.stream_msg_id = initial.message_id
                worker.stream_last_edit = time.monotonic()
            except Exception as e:
                log.warning("initial msg send failed: %s", e)

            result_text: str | None = None
            timed_out = False
            session_not_found = False
            use_stream = "--output-format" in " ".join(cmd)

            try:
                if use_stream:
                    while True:
                        if worker.cancel_requested:
                            proc.kill()
                            await proc.wait()
                            return RunnerResult("Tarea cancelada por el usuario.")

                        try:
                            raw_line = await asyncio.wait_for(
                                proc.stdout.readline(), timeout=self.config.timeout_seconds,
                            )
                        except asyncio.TimeoutError:
                            proc.kill()
                            await proc.wait()
                            timed_out = True
                            break

                        if not raw_line:
                            break
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue

                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            if result_text is None:
                                result_text = line
                            else:
                                result_text += "\n" + line
                            continue

                        await self._handle_activity(event, worker, bot, chat_id)
                        await self._handle_partial(event, worker, bot, chat_id)
                        await self._handle_assistant(event, worker, bot, chat_id)

                        if event.get("type") == "result":
                            if event.get("is_error"):
                                errors = event.get("errors", [])
                                err_msg = "; ".join(errors) if errors else "unknown error"
                                log.error("Worker[%d] Claude error: %s", chat_id, err_msg)
                                if any("No conversation found" in e for e in errors):
                                    session_not_found = True
                                    result_text = None
                                    break
                                result_text = f"Error de Claude: {err_msg}"
                                break
                            result_text = event.get("result", "")
                            cost = event.get("total_cost_usd")
                            duration = event.get("duration_ms")
                            if cost is not None and duration is not None:
                                log.info("Worker[%d] finished: %.1fs, $%.4f", chat_id, duration / 1000, cost)
                            self.sessions.mark_initialized(chat_id, "claude", session_id)
                            if worker.stream_msg_id is not None and worker.stream_msg_text:
                                try:
                                    await stream_update(
                                        bot, worker, chat_id,
                                        msg_max=self.config.telegram_msg_max,
                                        cooldown_seconds=self.config.progress_min_cooldown,
                                        force=True,
                                    )
                                except Exception as e:
                                    log.warning("stream flush failed: %s", e)
                            break

                        await self._maybe_stale_heartbeat(worker, bot, chat_id)
                else:
                    try:
                        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self.config.timeout_seconds)
                        result_text = stdout.decode("utf-8", errors="replace").strip()
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
                        timed_out = True

                elapsed = time.monotonic() - worker.start_time
                log.info(
                    "Worker[%d] done in %.1fs (timeout=%s, result=%s)",
                    chat_id, elapsed, timed_out, "yes" if result_text else "no",
                )

                if timed_out:
                    if attempt < self.config.max_retries:
                        await asyncio.sleep(self.config.retry_delay)
                        cmd = self.build_command(prompt, session_id, True)
                        continue
                    # Fix 2026-06-05: nunca dejar un mensaje parcial mudo/colgado. Si el
                    # proceso murió por timeout pero ya había texto streameado, finalizarlo
                    # y devolverlo con aviso, en vez de descartarlo silenciosamente.
                    if worker.stream_msg_text:
                        try:
                            await stream_update(
                                bot, worker, chat_id,
                                msg_max=self.config.telegram_msg_max,
                                cooldown_seconds=self.config.progress_min_cooldown,
                                force=True,
                            )
                        except Exception as e:
                            log.warning("partial flush on timeout failed: %s", e)
                        return RunnerResult(
                            worker.stream_msg_text
                            + "\n\n⏱️ _(respuesta cortada por timeout — pedime que continúe)_",
                            session_id=session_id,
                        )
                    return RunnerResult("Error: Claude tardó demasiado (timeout).", error=True)

                if session_not_found:
                    cmd = self.build_command(prompt, session_id, False)
                    continue

                if result_text is not None:
                    # El camino sin streaming (fallback de texto) nunca veía un
                    # evento `result`, así que la sesión se quedaba sin marcar y
                    # el turno siguiente volvía a chocar. Marcarla aquí también.
                    self.sessions.mark_initialized(chat_id, "claude", session_id)
                    return RunnerResult(result_text or "(sin respuesta)", session_id=session_id)

                await proc.wait()
                stderr_data = await proc.stderr.read()
                err = stderr_data.decode("utf-8", errors="replace").strip() if stderr_data else ""
                log.error("Worker[%d] no result. stderr: %s", chat_id, err[:300])

                if "No conversation found" in err:
                    cmd = self.build_command(prompt, session_id, False)
                    continue

                # "Session ID X is already in use": la conversación YA existe en
                # disco pero quedó marcada como no inicializada (típico: el bot se
                # reinició a mitad de turno, o el turno terminó por el camino sin
                # streaming). Entonces cada mensaje salía con `--session-id`, el CLI
                # lo rechazaba en 1 segundo y caíamos al fallback de texto: el turno
                # corría a ciegas y el usuario se quedaba mirando "Pensando..." varios
                # minutos. Reanudar SIN perder el streaming y dejar la marca puesta.
                if "already in use" in err:
                    log.info(
                        "Worker[%d] sesión ya existe en disco → --resume conservando streaming",
                        chat_id,
                    )
                    self.sessions.mark_initialized(chat_id, "claude", session_id)
                    cmd = self.build_command(prompt, session_id, True, stream=use_stream)
                    continue

                if use_stream and attempt == 1:
                    log.info("Worker[%d] retrying with text format", chat_id)
                    cmd = self.build_command(prompt, session_id, True, stream=False)
                    continue

                if attempt < self.config.max_retries:
                    await asyncio.sleep(self.config.retry_delay)
                    cmd = self.build_command(prompt, session_id, True)
                    continue
                # Fix 2026-06-05: si el proceso terminó sin un evento `result` pero ya había
                # texto streameado, preservarlo en vez de mandar "(sin respuesta)" mudo.
                if worker.stream_msg_text:
                    try:
                        await stream_update(
                            bot, worker, chat_id,
                            msg_max=self.config.telegram_msg_max,
                            cooldown_seconds=self.config.progress_min_cooldown,
                            force=True,
                        )
                    except Exception as e:
                        log.warning("partial flush on no-result failed: %s", e)
                    return RunnerResult(worker.stream_msg_text, session_id=session_id)
                return RunnerResult(f"Error: {err[:500]}" if err else "(sin respuesta)", error=True)

            except asyncio.CancelledError:
                proc.kill()
                await proc.wait()
                log.info("Worker[%d] claude cancelled", chat_id)
                return RunnerResult("Worker cancelado.")
            except Exception as e:
                proc.kill()
                await proc.wait()
                log.error("Worker[%d] claude error: %s", chat_id, e)
                if attempt < self.config.max_retries:
                    await asyncio.sleep(self.config.retry_delay)
                    continue
                return RunnerResult(f"Error inesperado: {e}", error=True)

        return RunnerResult("(sin respuesta)", error=True)

    # ---- per-event handlers ----
    async def _handle_activity(self, event, worker, bot, chat_id):
        activity = extract_claude_activity(event)
        if not activity:
            return
        worker.last_activity = activity
        elapsed_s = time.monotonic() - worker.start_time
        if not worker.activity_log or worker.activity_log[-1][1] != activity:
            worker.activity_log.append((elapsed_s, activity))
        if worker.stream_activity_only and worker.stream_msg_id is not None:
            now = time.monotonic()
            if now >= worker.stream_last_edit + self.config.progress_min_cooldown:
                worker.stream_last_edit = now
                worker.last_progress_sent = now
                try:
                    await safe_edit(bot, chat_id, worker.stream_msg_id, f"🛠️ {activity}")
                except Exception as e:
                    err = str(e).lower()
                    if "retry after" in err or "flood" in err or "429" in err:
                        m = _RATE_LIMIT_RE.search(err)
                        wait_s = int(m.group(1)) if m else 5
                        log.warning("activity edit rate-limited, backing off %ds", wait_s)
                        worker.stream_last_edit = now + wait_s
                    else:
                        log.warning("activity edit failed: %s", e)

    async def _handle_partial(self, event, worker, bot, chat_id):
        if event.get("type") != "stream_event":
            return
        inner = event.get("event", {}) or {}
        if inner.get("type") != "content_block_delta":
            return
        delta = inner.get("delta", {}) or {}
        if delta.get("type") != "text_delta":
            return
        piece = delta.get("text", "")
        if not piece:
            return
        if worker.stream_activity_only:
            worker.stream_activity_only = False
            worker.stream_msg_text = piece
        else:
            worker.stream_msg_text += piece
        worker.stream_using_partial = True
        worker._seen_text_len = len(worker.stream_msg_text)
        try:
            await stream_update(
                bot, worker, chat_id,
                msg_max=self.config.telegram_msg_max,
                cooldown_seconds=self.config.progress_min_cooldown,
            )
        except Exception as e:
            log.warning("partial stream update failed: %s", e)

    async def _handle_assistant(self, event, worker, bot, chat_id):
        if event.get("type") != "assistant":
            return
        if worker.stream_using_partial:
            return  # already streamed via deltas; don't duplicate
        msg_content = event.get("message", {}).get("content", [])
        full_text = ""
        for item in msg_content:
            if item.get("type") == "text":
                full_text += item.get("text", "")
        if len(full_text) <= worker._seen_text_len:
            return
        delta = full_text[worker._seen_text_len:]
        worker._seen_text_len = len(full_text)
        if worker.stream_activity_only:
            worker.stream_activity_only = False
            worker.stream_msg_text = delta
        else:
            worker.stream_msg_text += delta
        try:
            await stream_update(
                bot, worker, chat_id,
                msg_max=self.config.telegram_msg_max,
                cooldown_seconds=self.config.progress_min_cooldown,
            )
        except Exception as e:
            log.warning("stream update failed: %s", e)

    async def _maybe_stale_heartbeat(self, worker, bot, chat_id):
        now = time.monotonic()
        if worker.stream_msg_text:
            return
        if (now - worker.last_progress_sent) < self.config.progress_stale_interval:
            return
        worker.last_progress_sent = now
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=f"Sigo trabajando... {worker.last_activity}",
            )
        except Exception as e:
            log.warning("Fallback progress send failed: %s", e)
