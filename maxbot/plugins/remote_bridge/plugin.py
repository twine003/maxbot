"""Remote bridge — connects MaxBot to an Alejandro gateway (voice + remote devices).

The gateway (separate service, see github.com/twine003/ops-mcp-server,
remote_bridge/) is the public edge: it verifies Alexa requests and holds the
WebSocket connections of remote PCs. This plugin is a pure HTTP client of the
gateway's loopback-only API, so MaxBot opens no port and gains no dependency
(httpx already ships with python-telegram-bot).

What it adds:
  - Alexa turns: long-polls the gateway, runs each voice question through the
    bot's runner (same identity, its own session) and posts the answer back.
    Answers nobody heard by voice go to the owner's Telegram.
  - Approvals: a device tool marked "confirm" asks the owner in Telegram with
    Aprobar / Rechazar buttons. Single use, bound to that exact call.
  - /pc and /pc_estado for the owner.
  - The agent's own tool CLI (`python -m maxbot.plugins.remote_bridge.cli`),
    documented in CONTEXT.md.

Options (`[plugins.remote_bridge]`):
    gateway_url         = "http://127.0.0.1:8771"
    maxbot_token_env    = "BRIDGE_MAXBOT_TOKEN"   # removed from the env after reading
    owner_chat_id       = 0                       # default: the paired chat
    alexa               = true
    voice_model         = "claude"
    voice_profile       = "restricted"            # or "full" (same powers as Telegram)
    voice_allowed_tools = ["Read", "Grep", "Glob", "WebSearch", "WebFetch"]
    voice_timeout       = 120
    voice_cli_model     = ""                      # optional per-turn model; measured: no latency gain
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from ...runners.oneshot import Restrict, run_oneshot
from ..base import Plugin

if TYPE_CHECKING:
    from ...context import BotContext

log = logging.getLogger(__name__)

VOICE_PREFIX = (
    "[Canal: voz, Amazon Alexa. Tu respuesta se va a LEER EN VOZ ALTA. "
    "Responde en español, breve (2 o 3 frases), sin markdown, sin listas, sin tablas, "
    "sin URLs ni rutas de archivos. Si la respuesta necesita detalle, da el resumen y di "
    "que mandas el detalle por Telegram.]\n\n"
)
VOICE_RESTRICTED_NOTE = (
    "[Este canal es de SOLO LECTURA: no puedes ejecutar comandos, editar archivos ni "
    "operar computadoras. Si te piden algo así, explica que debe pedirse por Telegram.]\n\n"
)
VOICE_DENY = ["Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "Task", "KillShell"]


class RemoteBridgePlugin(Plugin):
    name = "remote_bridge"
    description = "Puente Alejandro: Alexa + PCs remotas vía gateway"
    telegram_commands = [
        ("pc", "Estado de las PCs conectadas"),
        ("pc_estado", "Detalle de una PC: /pc_estado <id>"),
    ]

    def __init__(self, options: dict[str, Any] | None = None):
        super().__init__(options)
        o = self.options
        self.gateway_url = str(o.get("gateway_url", "http://127.0.0.1:8771")).rstrip("/")
        self.alexa_enabled = bool(o.get("alexa", True))
        self.voice_model = o.get("voice_model", "claude")
        self.voice_profile = o.get("voice_profile", "restricted")
        self.voice_allowed = list(o.get("voice_allowed_tools", ["Read", "Grep", "Glob", "WebSearch", "WebFetch"]))
        self.voice_timeout = float(o.get("voice_timeout", 120))
        self.voice_cli_model = o.get("voice_cli_model") or None
        self._token = ""
        self._seq = 0
        self._voice_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self.ctx: "BotContext | None" = None

    # ---------------------------------------------------------------- setup
    def register(self, ctx: "BotContext") -> None:
        self.ctx = ctx
        env_name = self.options.get("maxbot_token_env", "BRIDGE_MAXBOT_TOKEN")
        # Read once and drop it from the process env: runner subprocesses (the
        # model's shell) inherit os.environ and must not hold the credential that
        # can approve device calls. They get BRIDGE_AGENT_TOKEN instead.
        self._token = os.environ.pop(env_name, "")
        if len(self._token) < 32:
            log.error("remote_bridge: %s missing — plugin disabled", env_name)
            return
        os.environ.setdefault("BRIDGE_INTERNAL_URL", self.gateway_url)

        app = ctx.connector.app
        app.add_handler(CommandHandler("pc", self._cmd_pc))
        app.add_handler(CommandHandler("pc_estado", self._cmd_pc_estado))
        app.add_handler(CallbackQueryHandler(self._on_decision, pattern=r"^rb:(ok|no):[0-9a-f]{12}$"))
        ctx.connector.job_queue.run_once(self._start, when=2)

    async def _start(self, _context) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll_forever())

    @property
    def owner_chat(self) -> int | None:
        explicit = self.options.get("owner_chat_id")
        if explicit:
            return int(explicit)
        return self.ctx.setup.paired_chat_id if self.ctx and self.ctx.setup else None

    def _is_owner(self, update: Update) -> bool:
        chat = update.effective_chat
        user = update.effective_user
        owner_user = self.ctx.setup.owner_user_id if self.ctx and self.ctx.setup else None
        return bool(chat and chat.id == self.owner_chat and (owner_user is None or (user and user.id == owner_user)))

    def _client(self, timeout: float = 35.0):
        import httpx
        return httpx.AsyncClient(base_url=self.gateway_url, timeout=timeout,
                                 headers={"Authorization": f"Bearer {self._token}"})

    # ---------------------------------------------------------------- event loop
    async def _poll_forever(self) -> None:
        backoff = 2.0
        primed = False
        while True:
            try:
                async with self._client(timeout=40.0) as http:
                    if not primed:
                        # Skip whatever happened while MaxBot was down: stale approvals
                        # and voice turns nobody is waiting for any more.
                        r = await http.get("/internal/v1/events", params={"after": 0, "wait": 0})
                        r.raise_for_status()
                        self._seq = r.json()["seq"]
                        primed = True
                        log.info("remote_bridge: connected to gateway (seq %d)", self._seq)
                    while True:
                        r = await http.get("/internal/v1/events", params={"after": self._seq, "wait": 25})
                        r.raise_for_status()
                        data = r.json()
                        if data["seq"] < self._seq:           # gateway restarted
                            self._seq = 0
                        for ev in data["events"]:
                            self._seq = max(self._seq, ev["seq"])
                            self._dispatch(ev)
                        backoff = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("remote_bridge: gateway poll failed (%s); retry in %.0fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                primed = False

    def _dispatch(self, ev: dict) -> None:
        kind = ev.get("type")
        if kind == "alexa_turn" and self.alexa_enabled:
            asyncio.create_task(self._voice_turn(ev))
        elif kind == "approval_requested":
            asyncio.create_task(self._ask_owner(ev))

    # ---------------------------------------------------------------- voice
    async def _voice_turn(self, ev: dict) -> None:
        job_id, text = ev["job_id"], ev.get("text", "")
        ctx = self.ctx
        owner = self.owner_chat
        restricted = self.voice_profile != "full"
        prompt = VOICE_PREFIX + (VOICE_RESTRICTED_NOTE if restricted else "") + text
        restrict = (Restrict(allowed_tools=self.voice_allowed, disallowed_tools=VOICE_DENY,
                             model=self.voice_cli_model) if restricted else None)
        model = self.voice_model or (ctx.sessions.get_active_model(owner) if owner else ctx.config.default_model)
        async with self._voice_lock:          # one voice turn at a time, in order
            started = time.monotonic()
            try:
                result = await run_oneshot(ctx.config, ctx.sessions, ctx.runners, prompt,
                                           session_key=f"alexa_{owner}", model=model,
                                           restrict=restrict, timeout=self.voice_timeout)
                answer, error = result.text, result.error
            except Exception as e:
                log.exception("remote_bridge: voice turn failed")
                answer, error = f"No pude responder: {e}", True
            runner_ms = int((time.monotonic() - started) * 1000)
        log.info("remote_bridge: voice turn %s done in %d ms (error=%s)", job_id[:8], runner_ms, error)
        delivered = {}
        try:
            async with self._client(timeout=10.0) as http:
                r = await http.post(f"/internal/v1/alexa/{job_id}/answer",
                                    json={"text": answer, "error": error, "runner_ms": runner_ms})
                delivered = r.json() if r.status_code == 200 else {}
        except Exception as e:
            log.warning("remote_bridge: could not post voice answer: %s", e)
        if owner and (not delivered.get("delivered_by_voice") or delivered.get("truncated")):
            note = "no la escuchaste por Alexa" if not delivered.get("delivered_by_voice") else "completa"
            await ctx.connector.send_long(owner, f"🔊 _{text}_ — respuesta ({note}):\n\n{answer}")

    # ---------------------------------------------------------------- approvals
    async def _ask_owner(self, ev: dict) -> None:
        owner = self.owner_chat
        if not owner:
            log.warning("remote_bridge: approval requested but no owner chat")
            return
        args = json.dumps(ev.get("arguments") or {}, ensure_ascii=False)
        if len(args) > 300:
            args = args[:300] + "…"
        aid = ev["approval_id"]
        text = (f"🔐 Aprobación requerida\n\nPC: {ev.get('device_id')}\nHerramienta: {ev.get('tool')}\n"
                f"Argumentos: {args}\nPedido por: {ev.get('requested_by')}\n"
                f"Vence en {ev.get('expires_in', 120)} s. Vale para esta llamada y nada más.")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Aprobar", callback_data=f"rb:ok:{aid}"),
                                    InlineKeyboardButton("❌ Rechazar", callback_data=f"rb:no:{aid}")]])
        try:
            await self.ctx.connector.bot.send_message(chat_id=owner, text=text, reply_markup=kb)
        except Exception as e:
            log.warning("remote_bridge: could not send approval request: %s", e)

    async def _on_decision(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if not self._is_owner(update):
            await q.answer("Solo el dueño puede aprobar.", show_alert=True)
            return
        _, verdict, aid = q.data.split(":")
        approve = verdict == "ok"
        await q.answer("Procesando…")
        try:
            async with self._client(timeout=130.0) as http:
                r = await http.post(f"/internal/v1/approvals/{aid}/decision",
                                    json={"approve": approve, "decided_by": f"telegram:{update.effective_user.id}"})
            status = r.json().get("status", r.status_code) if r.status_code == 200 else f"error {r.status_code}"
        except Exception as e:
            status = f"error: {e}"
        label = {"executed": "✅ Aprobada y ejecutada", "denied": "❌ Rechazada",
                 "expired": "⌛ Vencida (no se ejecutó)"}.get(status, f"Estado: {status}")
        try:
            await q.edit_message_text(f"{q.message.text}\n\n{label}")
        except Exception:
            pass

    # ---------------------------------------------------------------- commands
    async def _cmd_pc(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.ctx.is_allowed(update):
            return
        try:
            async with self._client(timeout=10.0) as http:
                devices = (await http.get("/internal/v1/devices")).json()["devices"]
        except Exception as e:
            await update.message.reply_text(f"No pude consultar el gateway: {e}")
            return
        if not devices:
            await update.message.reply_text("No hay PCs registradas en el gateway.")
            return
        lines = []
        for d in devices:
            if d["revoked"]:
                lines.append(f"⛔ {d['device_id']} — revocada")
            elif d["connected"]:
                n = sum(1 for lvl in d["tools"].values() if lvl != "deny")
                lines.append(f"🟢 {d['label'] or d['device_id']} ({d['device_id']}) — {d['hostname']}, "
                             f"latido hace {d['seconds_since_heartbeat']} s, {n} herramientas")
            else:
                lines.append(f"⚪ {d['label'] or d['device_id']} ({d['device_id']}) — desconectada")
        await update.message.reply_text("\n".join(lines))

    async def _cmd_pc_estado(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.ctx.is_allowed(update):
            return
        device = (context.args or [""])[0]
        if not device:
            await update.message.reply_text("Uso: /pc_estado <id-de-la-pc>  (ver /pc)")
            return
        try:
            async with self._client(timeout=40.0) as http:
                r = await http.post(f"/internal/v1/devices/{device}/call",
                                    json={"tool": "device.status", "requested_by": "telegram:/pc_estado"})
            body = r.json()
        except Exception as e:
            await update.message.reply_text(f"No pude consultar el gateway: {e}")
            return
        if r.status_code != 200 or not body.get("ok"):
            await update.message.reply_text(f"{device}: {body.get('error') or body.get('status')}")
            return
        info = json.loads(body["content"][0]["text"])
        desk = info.get("desktop_mcp", {})
        await update.message.reply_text(
            f"🟢 {device} ({info.get('hostname')})\n{info.get('os')}\n"
            f"Conector {info.get('connector_version')} — desktop-mcp: {desk.get('status')}"
            f"{' (en pausa)' if desk.get('paused') else ''}\nIda y vuelta: {body.get('roundtrip_ms')} ms")
