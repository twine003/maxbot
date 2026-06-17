"""Codex app-server runner — persistent JSON-RPC backend for lower latency.

Instead of spawning `codex exec` per turn (cold start every message), this keeps
a single `codex app-server --listen stdio://` process alive and drives it over
line-delimited JSON-RPC:

    initialize                                  (once, at startup)
    thread/start {cwd, developerInstructions}   (new conversation) -> threadId
    thread/resume {threadId}                    (continue across restarts)
    turn/start {threadId, input:[{text}]}       (one user turn)
      << item/agentMessage/delta  (streaming text)
      << item/started / item/completed (tool + command activity)
      << turn/completed

Auth is whatever `codex` already uses (~/.codex/auth.json or OPENAI_API_KEY) —
nothing changes there. Approvals/sandbox default to full access (no sandbox);
set [runners.codex].sandbox to harden if desired.

Protocol validated empirically against Codex CLI 0.125.0.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from typing import TYPE_CHECKING, Any, Callable

from ..connectors.telegram.ui.activity import extract_codex_activity
from ..connectors.telegram.ui.streaming import reset_stream, safe_edit, stream_update
from .base import Runner, RunnerResult
from ._common import find_in_npm_globals, patched_env

if TYPE_CHECKING:
    from ..connectors.telegram.connector import TelegramConnector
    from ..workers.state import WorkerState

log = logging.getLogger(__name__)


class CodexAppServerClient:
    """Persistent JSON-RPC client over a `codex app-server` stdio process."""

    def __init__(self, executable: str, cwd: str, env: dict):
        self._executable = executable
        self._cwd = cwd
        self._env = env
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        # threadId -> notification handler(method, params)
        self._turn_handlers: dict[str, Callable[[str, dict], None]] = {}
        self._loaded_threads: set[str] = set()
        self._start_lock = asyncio.Lock()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def _argv(self) -> list[str]:
        base = ["app-server", "--listen", "stdio://"]
        # .cmd / .bat shims on Windows must go through cmd.exe.
        if sys.platform == "win32" and self._executable.lower().endswith((".cmd", ".bat")):
            return ["cmd", "/c", self._executable, *base]
        return [self._executable, *base]

    async def ensure_started(self) -> None:
        if self.alive:
            return
        async with self._start_lock:
            if self.alive:
                return
            argv = self._argv()
            log.info("Starting codex app-server: %s (cwd=%s)", " ".join(argv), self._cwd)
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
                env=self._env,
                limit=16 * 1024 * 1024,
            )
            self._pending.clear()
            self._turn_handlers.clear()
            self._loaded_threads.clear()
            self._reader_task = asyncio.create_task(self._read_loop())
            asyncio.create_task(self._drain_stderr())
            await self.request(
                "initialize",
                {"clientInfo": {"name": "maxbot", "version": "0.1.0"}},
                timeout=30,
            )
            log.info("codex app-server initialized")

    async def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break
            txt = line.decode("utf-8", errors="replace").rstrip()
            if txt:
                log.debug("codex app-server stderr: %s", txt[:300])

    async def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while True:
                raw = await self._proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._dispatch(msg)
        except Exception as e:  # pragma: no cover
            log.error("codex app-server read loop error: %s", e)
        finally:
            self._fail_all_pending(RuntimeError("codex app-server closed"))

    def _dispatch(self, msg: dict) -> None:
        has_id = "id" in msg
        has_method = "method" in msg
        if has_id and not has_method:
            # Response to one of our requests.
            fut = self._pending.pop(msg["id"], None)
            if fut and not fut.done():
                fut.set_result(msg)
            return
        if has_id and has_method:
            # Server -> client request (e.g. approval). We run with no sandbox, so
            # these shouldn't fire; answer permissively so the server never hangs.
            asyncio.create_task(self._answer_server_request(msg))
            return
        if has_method:
            params = msg.get("params") or {}
            tid = params.get("threadId") if isinstance(params, dict) else None
            handler = self._turn_handlers.get(tid) if tid else None
            if handler:
                try:
                    handler(msg["method"], params)
                except Exception as e:
                    log.warning("turn handler error: %s", e)

    async def _answer_server_request(self, msg: dict) -> None:
        method = msg.get("method", "")
        result: Any = {}
        if "requestApproval" in method:
            result = {"decision": "approved"}
        try:
            await self._write({"id": msg["id"], "result": result})
        except Exception as e:
            log.warning("failed to answer server request %s: %s", method, e)

    def _fail_all_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def _write(self, msg: dict) -> None:
        assert self._proc and self._proc.stdin
        data = (json.dumps(msg) + "\n").encode("utf-8")
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()

    async def request(self, method: str, params: dict | None = None, *, timeout: float = 120) -> dict:
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        await self._write({"id": rid, "method": method, "params": params or {}})
        try:
            resp = await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(rid, None)
        if resp.get("error"):
            raise RuntimeError(f"{method} failed: {resp['error'].get('message', resp['error'])}")
        return resp.get("result") or {}

    def register_turn_handler(self, thread_id: str, handler: Callable[[str, dict], None]) -> None:
        self._turn_handlers[thread_id] = handler

    def unregister_turn_handler(self, thread_id: str) -> None:
        self._turn_handlers.pop(thread_id, None)

    async def shutdown(self) -> None:
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self._proc and self._proc.returncode is None:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except (asyncio.TimeoutError, Exception):
                pass


class CodexAppServerRunner(Runner):
    name = "codex"
    label = "Codex"

    def __init__(self, config, runner_config, sessions):
        super().__init__(config, runner_config, sessions)
        self._client: CodexAppServerClient | None = None

    def find_executable(self) -> str:
        import shutil
        found = find_in_npm_globals("codex") or shutil.which("codex")
        if found:
            return found
        log.warning("codex not found — falling back to 'codex' (PATH lookup)")
        return "codex"

    def build_command(self, prompt, session_id, resume):  # pragma: no cover
        # Not used (app-server is persistent), but the ABC + heartbeat probe it.
        return [self.executable, "app-server", "--listen", "stdio://"]

    def _get_client(self) -> CodexAppServerClient:
        if self._client is None:
            env = patched_env(self.executable)
            self._client = CodexAppServerClient(self.executable, str(self.config.workspace), env)
        return self._client

    def _thread_params(self) -> dict:
        params: dict[str, Any] = {
            "cwd": str(self.config.workspace),
            "approvalPolicy": "never",
            "sandbox": getattr(self.runner_config, "sandbox", None) or "danger-full-access",
        }
        si = self.get_system_instruction()
        if si and si.strip():
            params["developerInstructions"] = si
        if self.runner_config.model:
            params["model"] = self.runner_config.model
        return params

    async def _ensure_thread(self, client: CodexAppServerClient, session_id: str | None, resume: bool) -> str:
        """Return a usable threadId, starting or resuming as needed."""
        if resume and session_id:
            if session_id in client._loaded_threads:
                return session_id
            try:
                params = {"threadId": session_id}
                si = self.get_system_instruction()
                if si and si.strip():
                    params["developerInstructions"] = si
                if self.runner_config.model:
                    params["model"] = self.runner_config.model
                await client.request("thread/resume", params)
                client._loaded_threads.add(session_id)
                return session_id
            except Exception as e:
                log.info("thread/resume failed (%s); starting a fresh thread", e)
        result = await client.request("thread/start", self._thread_params())
        thread_id = (result.get("thread") or {}).get("id")
        if not thread_id:
            raise RuntimeError("thread/start returned no thread id")
        client._loaded_threads.add(thread_id)
        return thread_id

    async def run_turn(
        self,
        connector: "TelegramConnector",
        chat_id: int,
        prompt: str,
        session_id: str | None,
        resume: bool,
        worker: "WorkerState",
    ) -> RunnerResult:
        bot = connector.bot
        client = self._get_client()
        try:
            await client.ensure_started()
            thread_id = await self._ensure_thread(client, session_id, resume)
        except Exception as e:
            log.error("Worker[%d] codex app-server setup failed: %s", chat_id, e)
            return RunnerResult(f"Error iniciando Codex: {e}", error=True)

        worker.start_time = time.monotonic()
        worker.last_activity = "Pensando..."
        worker.last_progress_sent = time.monotonic()
        reset_stream(worker)
        worker.session_id = thread_id

        try:
            initial = await bot.send_message(chat_id=chat_id, text="🤔 Pensando...")
            worker.stream_msg_id = initial.message_id
            worker.stream_last_edit = time.monotonic()
        except Exception as e:
            log.warning("codex initial msg send failed: %s", e)

        done = asyncio.Event()
        state: dict[str, Any] = {"final": "", "error": None, "turn_id": None}
        loop = asyncio.get_event_loop()

        def on_event(method: str, params: dict) -> None:
            try:
                if method == "turn/started":
                    state["turn_id"] = (params.get("turn") or {}).get("id")
                elif method == "item/agentMessage/delta":
                    delta = params.get("delta", "") or ""
                    if delta:
                        if worker.stream_activity_only:
                            worker.stream_activity_only = False
                            worker.stream_msg_text = ""
                        worker.stream_msg_text += delta
                        loop.create_task(self._flush(bot, worker, chat_id))
                elif method == "item/started":
                    self._on_item_activity(params.get("item", {}), worker, bot, chat_id, loop)
                elif method == "item/completed":
                    item = params.get("item", {}) or {}
                    if item.get("type") == "agentMessage":
                        text = item.get("text", "") or ""
                        if text:
                            state["final"] = text
                            worker.stream_activity_only = False
                            worker.stream_msg_text = text
                            loop.create_task(self._flush(bot, worker, chat_id, force=True))
                elif method == "turn/completed":
                    turn = params.get("turn", {}) or {}
                    if turn.get("error"):
                        err = turn["error"]
                        state["error"] = err.get("message") if isinstance(err, dict) else str(err)
                    done.set()
            except Exception as e:
                log.warning("codex on_event error: %s", e)

        client.register_turn_handler(thread_id, on_event)
        try:
            turn_params: dict[str, Any] = {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
            }
            if self.runner_config.model:
                turn_params["model"] = self.runner_config.model
            await client.request("turn/start", turn_params)

            # Wait for turn/completed, honoring cancellation + inactivity timeout.
            timeout = self.config.timeout_seconds
            while not done.is_set():
                if worker.cancel_requested:
                    if state.get("turn_id"):
                        try:
                            await client.request(
                                "turn/interrupt",
                                {"threadId": thread_id, "turnId": state["turn_id"]},
                                timeout=15,
                            )
                        except Exception:
                            pass
                    return RunnerResult("Tarea cancelada por el usuario.", session_id=thread_id)
                try:
                    await asyncio.wait_for(done.wait(), timeout=min(timeout, 5))
                except asyncio.TimeoutError:
                    # No event for `timeout` seconds total → give up.
                    if (time.monotonic() - worker.start_time) > timeout:
                        partial = state["final"] or worker.stream_msg_text
                        if partial:
                            await self._flush(bot, worker, chat_id, force=True)
                            return RunnerResult(
                                partial + "\n\n⏱️ _(respuesta cortada por timeout — pedime que continúe)_",
                                session_id=thread_id,
                            )
                        return RunnerResult("Error: Codex tardó demasiado (timeout).", error=True)
        except Exception as e:
            log.error("Worker[%d] codex app-server turn error: %s", chat_id, e)
            return RunnerResult(f"Error de Codex: {e}", error=True)
        finally:
            client.unregister_turn_handler(thread_id)

        self.sessions.mark_initialized(chat_id, "codex", thread_id)
        if state["error"]:
            return RunnerResult(f"Error de Codex: {state['error']}", error=True, session_id=thread_id)

        final = state["final"] or worker.stream_msg_text
        if worker.stream_msg_id is not None and worker.stream_msg_text:
            await self._flush(bot, worker, chat_id, force=True)
        return RunnerResult(final or "(sin respuesta)", session_id=thread_id)

    async def run_oneshot(self, prompt: str, session_id: str | None, resume: bool) -> tuple[str, str | None]:
        """Non-streaming turn for background jobs (heartbeat). Returns (text, thread_id)."""
        client = self._get_client()
        await client.ensure_started()
        thread_id = await self._ensure_thread(client, session_id, resume)

        done = asyncio.Event()
        state: dict[str, Any] = {"final": "", "error": None}

        def on_event(method: str, params: dict) -> None:
            if method == "item/completed":
                item = params.get("item", {}) or {}
                if item.get("type") == "agentMessage":
                    state["final"] = item.get("text", "") or state["final"]
            elif method == "turn/completed":
                turn = params.get("turn", {}) or {}
                if turn.get("error"):
                    err = turn["error"]
                    state["error"] = err.get("message") if isinstance(err, dict) else str(err)
                done.set()

        client.register_turn_handler(thread_id, on_event)
        try:
            params: dict[str, Any] = {"threadId": thread_id, "input": [{"type": "text", "text": prompt}]}
            if self.runner_config.model:
                params["model"] = self.runner_config.model
            await client.request("turn/start", params)
            await asyncio.wait_for(done.wait(), timeout=self.config.timeout_seconds)
        except asyncio.TimeoutError:
            return (state["final"] or "(timeout)", thread_id)
        except Exception as e:
            return (f"Error de Codex: {e}", thread_id)
        finally:
            client.unregister_turn_handler(thread_id)
        if state["error"]:
            return (f"Error de Codex: {state['error']}", thread_id)
        return (state["final"] or "(sin respuesta)", thread_id)

    async def _flush(self, bot, worker, chat_id, force: bool = False) -> None:
        try:
            await stream_update(
                bot, worker, chat_id,
                msg_max=self.config.telegram_msg_max,
                cooldown_seconds=self.config.progress_min_cooldown,
                force=force,
            )
        except Exception as e:
            log.warning("codex stream flush failed: %s", e)

    def _on_item_activity(self, item, worker, bot, chat_id, loop) -> None:
        # Only command/tool items represent "activity"; agentMessage is text.
        if item.get("type") in ("agentMessage", "userMessage", "reasoning"):
            return
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
        worker.stream_activity_only = True
        worker.stream_msg_text = ""
        loop.create_task(self._safe_activity_edit(bot, chat_id, worker, activity))

    async def _safe_activity_edit(self, bot, chat_id, worker, activity) -> None:
        try:
            await safe_edit(bot, chat_id, worker.stream_msg_id, f"🛠️ {activity}")
        except Exception as e:
            log.warning("codex activity edit failed: %s", e)
