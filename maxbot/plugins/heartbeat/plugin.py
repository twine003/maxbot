"""Heartbeat — runs every N seconds, executes due tasks, cleans up stale media.

Options (from `[plugins.heartbeat]` in bot.toml):
    interval_seconds = 1800   # default 30 min
    first_run_after  = 60     # seconds before the first tick after boot
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from ...connectors.telegram.ui.attachments import send_long_to_chat
from ...runners._common import CLAUDE_ENV_DROP, CLAUDE_ENV_DROP_PREFIXES, patched_env
from ..base import Plugin


def _resolve_tz(task):
    """Devuelve el tzinfo de la tarea: zoneinfo(tz) si se puede, si no un offset fijo."""
    tzname = task.get("tz", "UTC")
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tzname)
    except Exception:
        return timezone(timedelta(hours=task.get("utc_offset_hours", 0)))


def _due_daily_at(task, now_utc) -> bool:
    """True si una tarea recurrente con hora local fija (`at_local_time` = 'HH:MM')
    debe correr ahora: ya pasó la hora local objetivo y NO corrió hoy (fecha local).
    Dispara una sola vez por día, en el primer tick a/después de la hora indicada.
    """
    at = task.get("at_local_time")
    if not at:
        return False
    try:
        hh, mm = (int(x) for x in str(at).split(":"))
    except (ValueError, TypeError):
        return False
    tz = _resolve_tz(task)
    local_now = now_utc.astimezone(tz)
    target = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if local_now < target:
        return False
    last_run = task.get("last_run")
    if last_run:
        try:
            if datetime.fromisoformat(last_run).astimezone(tz).date() == local_now.date():
                return False  # ya corrió hoy
        except ValueError:
            pass
    return True

if TYPE_CHECKING:
    from ...context import BotContext

log = logging.getLogger(__name__)


class HeartbeatPlugin(Plugin):
    name = "heartbeat"
    description = "Tick periódico: ejecuta tareas programadas + limpia media vieja"

    def register(self, ctx: "BotContext") -> None:
        interval = int(self.options.get("interval_seconds", 1800))
        first = int(self.options.get("first_run_after", 60))
        lock = asyncio.Lock()

        async def tick(_):
            if lock.locked():
                log.info("Heartbeat: previous tick still running, skipping.")
                return
            async with lock:
                await self._tick(ctx)

        ctx.connector.job_queue.run_repeating(
            tick, interval=interval, first=first, name="heartbeat",
        )
        log.info("Heartbeat scheduled every %d seconds", interval)

    async def _tick(self, ctx: "BotContext") -> None:
        cfg = ctx.config
        bot = ctx.connector.bot

        # Cleanup old media
        now_ts = datetime.now(timezone.utc).timestamp()
        for f in cfg.media_dir.iterdir():
            try:
                if f.is_file() and (now_ts - f.stat().st_mtime) > cfg.media_max_age:
                    f.unlink(missing_ok=True)
            except OSError:
                pass

        # Execute due tasks
        now = datetime.now(timezone.utc)
        tasks = ctx.tasks_store.load()
        if not cfg.allowed_chat_ids:
            log.info("Heartbeat: no allowed_chat_ids, skipping task execution")
            return
        target_chat = next(iter(cfg.allowed_chat_ids))

        for task in tasks["recurring"]:
            if not task.get("enabled", True):
                continue
            if task.get("at_local_time"):
                # Tarea diaria a hora local fija (cron-lite): dispara 1 vez/día.
                if not _due_daily_at(task, now):
                    continue
            else:
                interval = task.get("interval_minutes", 30)
                last_run = task.get("last_run")
                if last_run:
                    elapsed = (now - datetime.fromisoformat(last_run)).total_seconds() / 60
                    if elapsed < interval:
                        continue
            log.info("Heartbeat: executing '%s'", task.get("name", task["id"]))
            hb_key = f"heartbeat_{task['id']}"
            try:
                result = await self._run_oneshot(ctx, task["prompt"], hb_key, target_chat)
            except Exception as e:
                result = f"Error: {e}"
            task["last_run"] = now.isoformat()
            notify = task.get("notify", "always")
            if notify == "always" or (notify == "errors" and "error" in result.lower()):
                header = f"[Tarea: {task.get('name', task['id'][:8])}]\n\n"
                await send_long_to_chat(bot, target_chat, header + result)

        remaining = []
        for task in tasks["one_time"]:
            run_at = datetime.fromisoformat(task["run_at"])
            if now >= run_at:
                log.info("Heartbeat: one-time '%s'", task["id"][:8])
                try:
                    result = await self._run_oneshot(
                        ctx, task["prompt"], f"onetime_{task['id']}", target_chat,
                    )
                except Exception as e:
                    result = f"Error: {e}"
                notify = task.get("notify", "always")
                if notify == "always" or (notify == "errors" and "error" in result.lower()):
                    await send_long_to_chat(bot, target_chat, f"[Tarea programada]\n\n{result}")
            else:
                remaining.append(task)
        tasks["one_time"] = remaining
        ctx.tasks_store.save(tasks)
        log.info("Heartbeat: tick done")

    async def _run_oneshot(
        self,
        ctx: "BotContext",
        prompt: str,
        session_key: str,
        chat_id: int,
    ) -> str:
        """Run one CLI turn with no live streaming — heartbeat output goes via send_long.

        Uses the runner's build_command but parses minimal events (just enough
        to extract the final text and persist the session id).
        """
        model = ctx.sessions.get_active_model(chat_id)
        sid, resume = ctx.sessions.get_session(session_key, model)
        runner = ctx.runners.get(model)

        # Persistent backends (e.g. Codex app-server) drive turns over a live
        # connection, not a one-shot subprocess — use their own oneshot path.
        if hasattr(runner, "run_oneshot"):
            text, thread_id = await runner.run_oneshot(prompt, sid, resume)
            if thread_id:
                ctx.sessions.mark_initialized(session_key, model, thread_id)
            return text

        # Claude has a build_command(stream=True/False). Codex has a single one.
        if hasattr(runner, "build_command") and "stream" in runner.build_command.__code__.co_varnames:
            cmd = runner.build_command(prompt, sid, resume, stream=True)
        else:
            cmd = runner.build_command(prompt, sid, resume)

        if model == "claude":
            env = patched_env(
                runner.executable,
                drop_keys=CLAUDE_ENV_DROP,
                drop_prefixes=CLAUDE_ENV_DROP_PREFIXES,
            )
        else:
            env = patched_env(runner.executable)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(ctx.config.workspace),
            env=env,
            limit=10 * 1024 * 1024,
        )

        result_text: str | None = None
        current_session_id = sid
        try:
            while True:
                try:
                    raw_line = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=ctx.config.timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
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
                        continue
                    if event.get("type") == "turn.completed":
                        if current_session_id:
                            ctx.sessions.mark_initialized(session_key, model, current_session_id)
                        break
                    if event.get("type") == "turn.failed":
                        result_text = f"Error de Codex: {event.get('error', {}).get('message', 'unknown error')}"
                        break
                else:  # claude
                    if event.get("type") == "result":
                        result_text = event.get("result", "")
                        ctx.sessions.mark_initialized(session_key, model, current_session_id)
                        break
        except Exception as e:
            proc.kill()
            await proc.wait()
            log.error("Heartbeat oneshot error: %s", e)

        await proc.wait()
        return result_text or "(sin respuesta de heartbeat)"
