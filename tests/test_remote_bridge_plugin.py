"""remote_bridge plugin against the REAL gateway (uvicorn, loopback): voice turns,
approvals through the Telegram callback, the agent CLI, and /pc."""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from .conftest import gateway_module

MB, AG = "m" * 40, "a" * 40


def _port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def gw(tmp_path):
    gateway_module()
    import uvicorn
    from remote_bridge.gateway import Gateway, Settings
    from remote_bridge.alexa import AlexaVerifier

    class NoVerify(AlexaVerifier):
        async def verify(self, headers, body, request_json):
            return None

    g = Gateway(Settings(data_dir=tmp_path / "gw", maxbot_token=MB, agent_token=AG,
                         alexa_skill_id="skill-x", alexa_allowed_user_ids={"user-1"},
                         alexa_budget_seconds=3.0, alexa_progressive=False), verifier=NoVerify())
    ports = (_port(), _port())
    servers = []
    loop = asyncio.new_event_loop()

    def run():
        asyncio.set_event_loop(loop)
        for app, port in ((g.public, ports[0]), (g.internal, ports[1])):
            s = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
            s.install_signal_handlers = lambda: None
            servers.append(s)
        loop.run_until_complete(asyncio.gather(*(s.serve() for s in servers)))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    for _ in range(100):
        if len(servers) == 2 and all(s.started for s in servers):
            break
        time.sleep(0.05)
    g.public_url, g.internal_url, g.loop = f"http://127.0.0.1:{ports[0]}", f"http://127.0.0.1:{ports[1]}", loop
    yield g
    for s in servers:
        s.should_exit = True
    t.join(10)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.sent.append({"chat_id": chat_id, "text": text, "markup": reply_markup})
        return SimpleNamespace(message_id=len(self.sent))


def make_plugin(gw, monkeypatch, *, voice_text="Tienes dos pendientes."):
    from maxbot.plugins.remote_bridge.plugin import RemoteBridgePlugin
    from maxbot.runners.oneshot import OneshotResult

    monkeypatch.setenv("BRIDGE_MAXBOT_TOKEN", MB)
    plugin = RemoteBridgePlugin({"gateway_url": gw.internal_url, "owner_chat_id": 111})
    bot = FakeBot()
    long_msgs = []

    async def send_long(chat_id, text):
        long_msgs.append((chat_id, text))

    calls = []

    async def fake_oneshot(config, sessions, runners, prompt, session_key, model, *, restrict=None, timeout=None):
        calls.append({"prompt": prompt, "session_key": session_key, "model": model, "restrict": restrict})
        await asyncio.sleep(float(getattr(plugin, "_test_delay", 0.05)))
        return OneshotResult(voice_text, "sid", False, 50)

    monkeypatch.setattr("maxbot.plugins.remote_bridge.plugin.run_oneshot", fake_oneshot)
    ctx = SimpleNamespace(
        config=SimpleNamespace(default_model="claude"),
        sessions=None, runners=None,
        setup=SimpleNamespace(paired_chat_id=111, owner_user_id=222),
        connector=SimpleNamespace(bot=bot, send_long=send_long, app=None, job_queue=None),
        is_allowed=lambda update: True,
    )
    plugin.ctx = ctx
    plugin._token = MB
    return plugin, bot, long_msgs, calls


def alexa(gw, intent, slots=None):
    payload = {
        "version": "1.0",
        "context": {"System": {"application": {"applicationId": "skill-x"}, "user": {"userId": "user-1"}}},
        "request": {"type": "IntentRequest", "requestId": "r", "timestamp": "2026-01-01T00:00:00Z",
                    "intent": {"name": intent, "slots": {k: {"value": v} for k, v in (slots or {}).items()}}},
    }
    return httpx.post(f"{gw.public_url}/alexa/v1", json=payload, timeout=15).json()["response"]


def test_voice_turn_end_to_end(gw, monkeypatch):
    plugin, bot, long_msgs, calls = make_plugin(gw, monkeypatch)
    loop = asyncio.new_event_loop()

    async def poll():
        try:
            await plugin._poll_forever()
        except asyncio.CancelledError:
            pass

    th = threading.Thread(target=lambda: loop.run_until_complete(poll()), daemon=True)
    th.start()
    deadline = time.time() + 5
    while not gw.events.consumer_alive() and time.time() < deadline:
        time.sleep(0.05)
    resp = alexa(gw, "ConsultaIntent", {"consulta": "qué tengo pendiente"})
    assert resp["outputSpeech"]["text"] == "Tienes dos pendientes."
    c = calls[0]
    assert c["session_key"] == "alexa_111" and c["model"] == "claude"
    assert c["prompt"].startswith("[Canal: voz") and "SOLO LECTURA" in c["prompt"]
    assert c["restrict"] is not None and "Bash" in c["restrict"].disallowed_tools
    assert not long_msgs                          # heard by voice -> no Telegram copy
    m = gw.alexa.metrics[-1]
    assert m["answered"] and m["runner_ms"] >= 30   # Windows timer granularity

    # slow turn nobody waits for -> the answer goes to Telegram
    plugin._test_delay = 4.0
    resp = alexa(gw, "ConsultaIntent", {"consulta": "algo lento"})
    assert "Sigo trabajando" in resp["outputSpeech"]["text"]
    deadline = time.time() + 8
    while not long_msgs and time.time() < deadline:
        time.sleep(0.1)
    assert long_msgs and long_msgs[0][0] == 111 and "no la escuchaste" in long_msgs[0][1]
    loop.call_soon_threadsafe(lambda: [t.cancel() for t in asyncio.all_tasks(loop)])
    th.join(5)


def test_approval_via_telegram_callback(gw, monkeypatch):
    plugin, bot, _, _ = make_plugin(gw, monkeypatch)
    from remote_bridge.policy import ApprovalStore
    approval = gw.hub.approvals.request("pc-x", "desktop.screenshot", {"scale": 1}, "agent:cli")

    async def scenario():
        await plugin._ask_owner({**approval.public()})
        msg = bot.sent[-1]
        assert msg["chat_id"] == 111 and "desktop.screenshot" in msg["text"]
        buttons = msg["markup"].inline_keyboard[0]
        data = buttons[0].callback_data
        assert data == f"rb:ok:{approval.id}"

        answered, edited = [], []

        class Q:
            def __init__(self, data, user_id, chat_id):
                self.data = data
                self.message = SimpleNamespace(text=msg["text"])
                self._u, self._c = user_id, chat_id

            async def answer(self, text=None, show_alert=False):
                answered.append(text)

            async def edit_message_text(self, text):
                edited.append(text)

        def update(user_id, chat_id, data):
            return SimpleNamespace(callback_query=Q(data, user_id, chat_id),
                                   effective_user=SimpleNamespace(id=user_id),
                                   effective_chat=SimpleNamespace(id=chat_id))

        # someone else in the chat cannot approve
        await plugin._on_decision(update(999, 111, data), None)
        assert answered[-1] == "Solo el dueño puede aprobar." and not edited
        assert gw.hub.approvals.get(approval.id).status == "pending"
        # the owner can; the device is offline so it is approved but the run fails cleanly
        await plugin._on_decision(update(222, 111, data), None)
        assert gw.hub.approvals.get(approval.id).decided_by == "telegram:222"
        assert edited

    asyncio.run(scenario())


def test_agent_cli_against_gateway(gw, tmp_path):
    env = {"BRIDGE_AGENT_TOKEN": AG, "BRIDGE_INTERNAL_URL": gw.internal_url, "PATH": ""}
    import os
    env = {**os.environ, **env}
    r = subprocess.run([sys.executable, "-m", "maxbot.plugins.remote_bridge.cli", "devices"],
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and json.loads(r.stdout) == {"devices": []}
    r = subprocess.run([sys.executable, "-m", "maxbot.plugins.remote_bridge.cli", "call", "pc-x", "device.ping"],
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 2 and json.loads(r.stdout)["status"] == "unknown_device"
    # the agent credential cannot approve
    r = httpx.post(f"{gw.internal_url}/internal/v1/approvals/abc/decision", json={"approve": True},
                   headers={"Authorization": f"Bearer {AG}"})
    assert r.status_code == 403
    # no token -> clean error, no traceback
    env.pop("BRIDGE_AGENT_TOKEN")
    r = subprocess.run([sys.executable, "-m", "maxbot.plugins.remote_bridge.cli", "devices"],
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 3 and "Traceback" not in r.stderr


def test_pc_command_lists_devices(gw, monkeypatch):
    plugin, _, _, _ = make_plugin(gw, monkeypatch)
    gw.registry.add("pc-casa", label="PC de casa", tools={})
    replies = []
    update = SimpleNamespace(message=SimpleNamespace(reply_text=lambda t: _append(replies, t)))
    asyncio.run(plugin._cmd_pc(update, None))
    assert "PC de casa" in replies[0] and "desconectada" in replies[0]


async def _append(lst, t):
    lst.append(t)
