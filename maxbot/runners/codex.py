"""Codex CLI runner — adapter for `codex exec --json`."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from typing import TYPE_CHECKING

from ..connectors.telegram.ui.activity import extract_codex_activity
from ..connectors.telegram.ui.streaming import (
    reset_stream, safe_edit, stream_update,
)
from .base import Runner, RunnerResult
from ._common import find_in_npm_globals, patched_env

if TYPE_CHECKING:
    from ..connectors.telegram.connector import TelegramConnector
    from ..workers.state import WorkerState

log = logging.getLogger(__name__)
_RATE_LIMIT_RE = re.compile(r"retry after\s+(\d+)")


class CodexRunner(Runner):
    name = "codex"
    label = "Codex"

    def find_executable(self) -> str:
        found = find_in_npm_globals("codex")
        if found:
            return found
        found = shutil.which("codex")
        if found:
            return found
        log.warning("codex not found — falling back to 'codex' (PATH lookup)")
        return "codex"

    def build_command(self, prompt: str, session_id: str | None, resume: bool) -> list[str]:
        # Lesson 2026-05-29: any identity in Codex's system instruction makes it
        # greet on every turn instead of executing. Default config.runners.codex
        # ships with system_instruction = "".
        full_prompt = self.get_system_instruction() + prompt
        if resume and session_id:
            return [
                self.executable, "exec", "resume",
                "--dangerously-bypass-approvals-and-sandbox",
                "--json", "--skip-git-repo-check",
                session_id, full_prompt,
            ]
        return [
            self.executable, "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--json", "--skip-git-repo-check",
            full_prompt,
        ]

    async def run_turn(
        self,
        connector: "TelegramConnector",
        chat_id: int,
        prompt: str,
        session_id: str | None,
        resume: bool,
        worker: "WorkerState",
    ) -> RunnerResult:
        cmd = self.build_command(prompt, session_id, resume)
        env = patched_env(self.executable)
        bot = connector.bot

        for attempt in range(1, self.config.max_retries + 1):
            log.info("Worker[%d] codex attempt %d: %s", chat_id, attempt, prompt[:80])

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
            reset_stream(worker)

            try:
                initial = await bot.send_message(chat_id=chat_id, text="🤔 Pensando...")
                worker.stream_msg_id = initial.message_id
                worker.stream_last_edit = time.monotonic()
            except Exception as e:
                log.warning("codex initial msg send failed: %s", e)

            result_text = ""
            timed_out = False
            current_session_id = session_id

            try:
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
                        continue

                    ev_type = event.get("type")

                    if ev_type == "thread.started":
                        current_session_id = event.get("thread_id") or current_session_id
                        if current_session_id:
                            worker.session_id = current_session_id
                        continue

                    if ev_type == "item.started":
                        await self._handle_item_activity(event, worker, bot, chat_id)
                        continue

                    if ev_type == "item.completed":
                        item = event.get("item", {})
                        if item.get("type") == "agent_message":
                            text = item.get("text", "") or ""
                            if text:
                                # The latest agent_message is the canonical phase output.
                                # Replace the live message with it.
                                result_text = text
                                worker.stream_activity_only = False
                                worker.stream_msg_text = text
                                worker.stream_carry = ""
                                try:
                                    await stream_update(
                                        bot, worker, chat_id,
                                        msg_max=self.config.telegram_msg_max,
                                        cooldown_seconds=self.config.progress_min_cooldown,
                                        force=True,
                                    )
                                except Exception as e:
                                    log.warning("codex agent_message stream update failed: %s", e)
                        elif item.get("type") == "error":
                            err_msg = item.get("message", "unknown error")
                            result_text = f"Error de Codex: {err_msg}"
                        continue

                    if ev_type == "turn.completed":
                        if current_session_id:
                            self.sessions.mark_initialized(chat_id, "codex", current_session_id)
                        # Restore guard (lesson 2026-05-29): if activity took the screen
                        # but there was a previous agent_message, restore it before flush.
                        if result_text and (worker.stream_activity_only or not worker.stream_msg_text):
                            worker.stream_activity_only = False
                            worker.stream_msg_text = result_text
                            worker.stream_carry = ""
                        if worker.stream_msg_id is not None and worker.stream_msg_text:
                            try:
                                await stream_update(
                                    bot, worker, chat_id,
                                    msg_max=self.config.telegram_msg_max,
                                    cooldown_seconds=self.config.progress_min_cooldown,
                                    force=True,
                                )
                            except Exception as e:
                                log.warning("codex final flush failed: %s", e)
                        return RunnerResult(result_text or "(sin respuesta)", session_id=current_session_id)

                    if ev_type == "turn.failed":
                        err = event.get("error", {}).get("message", "unknown error")
                        return RunnerResult(f"Error de Codex: {err}", error=True)

                    # Heartbeat fallback
                    now = time.monotonic()
                    if not worker.stream_msg_text and (now - worker.last_progress_sent) >= self.config.progress_stale_interval:
                        worker.last_progress_sent = now
                        try:
                            await bot.send_message(
                                chat_id=chat_id,
                                text=f"Sigo trabajando... {worker.last_activity}",
                            )
                        except Exception as e:
                            log.warning("Codex progress send failed: %s", e)

                await proc.wait()
                stderr_data = await proc.stderr.read()
                err = stderr_data.decode("utf-8", errors="replace").strip() if stderr_data else ""

                if timed_out:
                    if attempt < self.config.max_retries:
                        await asyncio.sleep(self.config.retry_delay)
                        cmd = self.build_command(prompt, current_session_id, bool(current_session_id))
                        continue
                    # Fix 2026-06-05: no descartar texto parcial al morir por timeout.
                    partial = result_text or worker.stream_msg_text
                    if partial:
                        if worker.stream_msg_id is not None and worker.stream_msg_text:
                            try:
                                await stream_update(
                                    bot, worker, chat_id,
                                    msg_max=self.config.telegram_msg_max,
                                    cooldown_seconds=self.config.progress_min_cooldown,
                                    force=True,
                                )
                            except Exception as e:
                                log.warning("codex partial flush on timeout failed: %s", e)
                        return RunnerResult(
                            partial + "\n\n⏱️ _(respuesta cortada por timeout — pedime que continúe)_",
                            session_id=current_session_id,
                        )
                    return RunnerResult("Error: Codex tardó demasiado (timeout).", error=True)

                if proc.returncode == 0:
                    if current_session_id:
                        self.sessions.mark_initialized(chat_id, "codex", current_session_id)
                    if result_text and (worker.stream_activity_only or not worker.stream_msg_text):
                        worker.stream_activity_only = False
                        worker.stream_msg_text = result_text
                        worker.stream_carry = ""
                    if worker.stream_msg_id is not None and worker.stream_msg_text:
                        try:
                            await stream_update(
                                bot, worker, chat_id,
                                msg_max=self.config.telegram_msg_max,
                                cooldown_seconds=self.config.progress_min_cooldown,
                                force=True,
                            )
                        except Exception as e:
                            log.warning("codex eof flush failed: %s", e)
                    return RunnerResult(result_text or "(sin respuesta)", session_id=current_session_id)

                if attempt < self.config.max_retries:
                    await asyncio.sleep(self.config.retry_delay)
                    cmd = self.build_command(prompt, current_session_id, bool(current_session_id))
                    continue
                # Fix 2026-06-05: si terminó sin éxito pero hubo texto parcial, devolverlo
                # en vez de "(sin respuesta)" mudo.
                if result_text or worker.stream_msg_text:
                    return RunnerResult(result_text or worker.stream_msg_text, session_id=current_session_id)
                return RunnerResult(f"Error: {err[:500]}" if err else "(sin respuesta)", error=True)

            except asyncio.CancelledError:
                proc.kill()
                await proc.wait()
                log.info("Worker[%d] codex cancelled", chat_id)
                return RunnerResult("Worker cancelado.")
            except Exception as e:
                proc.kill()
                await proc.wait()
                log.error("Worker[%d] codex error: %s", chat_id, e)
                if attempt < self.config.max_retries:
                    await asyncio.sleep(self.config.retry_delay)
                    continue
                return RunnerResult(f"Error inesperado: {e}", error=True)

        return RunnerResult("(sin respuesta)", error=True)

    async def _handle_item_activity(self, event, worker, bot, chat_id):
        item = event.get("item", {})
        activity = extract_codex_activity(item)
        if not activity or worker.stream_msg_id is None:
            return
        worker.last_activity = activity
        elapsed_s = time.monotonic() - worker.start_time
        if not worker.activity_log or worker.activity_log[-1][1] != activity:
            worker.activity_log.append((elapsed_s, activity))
        now = time.monotonic()
        if now < worker.stream_last_edit + self.config.progress_min_cooldown:
            return
        worker.stream_last_edit = now
        worker.last_progress_sent = now
        # For codex we always switch to activity-only on item.started — phases
        # (text → tool → text) alternate throughout a turn.
        worker.stream_activity_only = True
        worker.stream_msg_text = ""
        try:
            await safe_edit(bot, chat_id, worker.stream_msg_id, f"🛠️ {activity}")
        except Exception as e:
            err = str(e).lower()
            if "retry after" in err or "flood" in err or "429" in err:
                m = _RATE_LIMIT_RE.search(err)
                wait_s = int(m.group(1)) if m else 5
                log.warning("codex activity edit rate-limited, backing off %ds", wait_s)
                worker.stream_last_edit = now + wait_s
            else:
                log.warning("codex activity edit failed: %s", e)
