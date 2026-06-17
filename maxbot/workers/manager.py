"""WorkerManager — owns the per-chat worker lifecycle and the spawn/cancel logic.

Replaces the global `_workers` dict from the legacy bot.py with an object the
handlers + plugins can interact with via the BotContext.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ..connectors.telegram.ui.attachments import send_attachments, send_chunks
from ..connectors.telegram.ui.markdown import split_at_safe_point
from ..connectors.telegram.ui.streaming import safe_edit
from .state import WorkerState

if TYPE_CHECKING:
    from ..config import BotConfig
    from ..connectors.telegram.connector import TelegramConnector
    from ..core.sessions import SessionStore
    from ..runners.base import RunnerRegistry

log = logging.getLogger(__name__)


class WorkerManager:
    def __init__(
        self,
        config: "BotConfig",
        connector: "TelegramConnector",
        sessions: "SessionStore",
        runners: "RunnerRegistry",
    ):
        self.config = config
        self.connector = connector
        self.sessions = sessions
        self.runners = runners
        self._workers: dict[int, list[WorkerState]] = {}

    # ------- worker lifecycle -------
    def list_active(self, chat_id: int) -> list[WorkerState]:
        return [w for w in self._workers.get(chat_id, []) if w.task and not w.task.done()]

    def cancel_all(self, chat_id: int) -> int:
        workers = self.list_active(chat_id)
        for worker in workers:
            worker.cancel_requested = True
            if worker.proc:
                try:
                    worker.proc.kill()
                except Exception:
                    pass
            if worker.task:
                worker.task.cancel()
        if chat_id in self._workers and not self.list_active(chat_id):
            self._workers.pop(chat_id, None)
        return len(workers)

    def spawn(
        self,
        chat_id: int,
        prompt: str,
        force_session: str | None = None,
        force_model: str | None = None,
    ) -> WorkerState:
        # Drop completed entries first
        if chat_id in self._workers:
            self._workers[chat_id] = [w for w in self._workers[chat_id] if w.task and not w.task.done()]
            if not self._workers[chat_id]:
                self._workers.pop(chat_id, None)

        model = force_model or self.sessions.get_active_model(chat_id)
        worker = WorkerState(model=model)
        worker.task = asyncio.create_task(
            self._loop(chat_id, prompt, worker, force_session=force_session, force_model=model)
        )
        self._workers.setdefault(chat_id, []).append(worker)
        return worker

    # ------- loop -------
    async def _loop(
        self,
        chat_id: int,
        prompt: str,
        worker: WorkerState,
        force_session: str | None = None,
        force_model: str | None = None,
    ) -> None:
        try:
            model = force_model or self.sessions.get_active_model(chat_id)
            if force_session:
                session_id, resume = force_session, True
            else:
                session_id, resume = self.sessions.get_session(chat_id, model)
            worker.model = model
            worker.session_id = session_id or ""

            runner = self.runners.get(model)
            result = await runner.run_turn(
                self.connector, chat_id, prompt, session_id, resume, worker,
            )
            await self._send_result(chat_id, result.text, worker)

        except asyncio.CancelledError:
            log.info("Worker[%d] cancelled", chat_id)
        except Exception as e:
            log.error("Worker[%d] loop error: %s", chat_id, e)
            try:
                await self.connector.bot.send_message(chat_id=chat_id, text=f"Error en worker: {e}")
            except Exception:
                pass
        finally:
            workers = self._workers.get(chat_id, [])
            if worker in workers:
                workers.remove(worker)
            if not workers:
                self._workers.pop(chat_id, None)
            log.info(
                "Worker[%d] stopped (remaining: %d)",
                chat_id, len(self._workers.get(chat_id, [])),
            )

    async def _send_result(self, chat_id: int, result: str, worker: WorkerState) -> None:
        """Either edit the live streaming message into its final state or send fresh."""
        bot = self.connector.bot
        if worker.stream_msg_id is not None:
            # Edge case: only activity ran (no LLM text emitted). The live message
            # still shows "🛠️ last activity"; replace it with the CLI result.
            if worker.stream_activity_only and result:
                clean = await send_attachments(bot, chat_id, result)
                if clean:
                    try:
                        if len(clean) <= self.config.telegram_msg_max:
                            await safe_edit(bot, chat_id, worker.stream_msg_id, clean)
                        else:
                            cut = split_at_safe_point(clean, self.config.telegram_msg_max)
                            await safe_edit(bot, chat_id, worker.stream_msg_id, clean[:cut])
                            rest = clean[cut:]
                            async def _send(content, parse_mode=None):
                                await bot.send_message(chat_id=chat_id, text=content, parse_mode=parse_mode)
                            await send_chunks(_send, rest)
                    except Exception as e:
                        log.warning("activity→result transition failed: %s — fallback to send_long", e)
                        await self.connector.send_long(chat_id, clean)
                return
            # Normal path: stream already showed the text, only drain attachments now
            if result:
                await send_attachments(bot, chat_id, result)
            return
        # No live message (early failure) → classic send
        await self.connector.send_long(chat_id, result)
