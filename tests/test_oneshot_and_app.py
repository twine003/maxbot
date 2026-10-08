"""Runner one-shot path, the restricted profile, heartbeat delegation, and that the
app (Telegram connector + plugins) still assembles with the new plugin enabled."""

from __future__ import annotations

import asyncio
import json

from maxbot.config import load_config
from maxbot.core.sessions import SessionStore
from maxbot.app import build_runner_registry
from maxbot.runners.oneshot import Restrict, run_oneshot


def _setup(deployment, fake_claude):
    config = load_config(deployment / "bot.toml")
    sessions = SessionStore(config.sessions_file, config.default_model, list(config.runners))
    runners = build_runner_registry(config, sessions)
    log = fake_claude(runners.get("claude"))
    return config, sessions, runners, log


def test_oneshot_runs_turn_and_persists_session(deployment, fake_claude):
    config, sessions, runners, log = _setup(deployment, fake_claude)
    r1 = asyncio.run(run_oneshot(config, sessions, runners, "hola", "alexa_111", "claude"))
    assert r1.error is False and r1.text.startswith("Respuesta:") and r1.elapsed_ms >= 0
    sid, resume = sessions.get_session("alexa_111", "claude")
    assert resume is True and sid == r1.session_id
    asyncio.run(run_oneshot(config, sessions, runners, "otra", "alexa_111", "claude"))
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert "--session-id" in calls[0] and "--resume" in calls[1]       # same conversation
    # default profile = the bot's own allowed_tools
    assert calls[0].count("--allowedTools") == 2 and "Bash(*)" in calls[0]
    # the Telegram chat's own session is untouched
    assert sessions.get_session(111, "claude")[1] is False


def test_restricted_profile_is_enforced_on_the_cli(deployment, fake_claude):
    config, sessions, runners, log = _setup(deployment, fake_claude)
    r = asyncio.run(run_oneshot(config, sessions, runners, "hola", "alexa_111", "claude",
                                restrict=Restrict(allowed_tools=["Read", "Grep"],
                                                  disallowed_tools=["Bash", "Write"])))
    assert r.error is False
    argv = json.loads(log.read_text().splitlines()[-1])
    allowed = [argv[i + 1] for i, a in enumerate(argv) if a == "--allowedTools"]
    denied = [argv[i + 1] for i, a in enumerate(argv) if a == "--disallowedTools"]
    assert allowed == ["Read", "Grep"] and "Bash(*)" not in argv
    assert denied == ["Bash", "Write"] and "--strict-mcp-config" in argv


def test_restricted_profile_refuses_codex(deployment, fake_claude):
    config, sessions, runners, _ = _setup(deployment, fake_claude)
    r = asyncio.run(run_oneshot(config, sessions, runners, "x", "k", "codex", restrict=Restrict()))
    assert r.error is True and "restringido" in r.text


def test_oneshot_reports_cli_errors(deployment, fake_claude):
    config, sessions, runners, _ = _setup(deployment, fake_claude)
    r = asyncio.run(run_oneshot(config, sessions, runners, "FAIL please", "k", "claude"))
    assert r.error is True


def test_heartbeat_still_uses_the_shared_path(deployment, fake_claude):
    from types import SimpleNamespace
    from maxbot.plugins.heartbeat.plugin import HeartbeatPlugin
    config, sessions, runners, _ = _setup(deployment, fake_claude)
    ctx = SimpleNamespace(config=config, sessions=sessions, runners=runners)
    text = asyncio.run(HeartbeatPlugin()._run_oneshot(ctx, "tarea", "heartbeat_t1", 111))
    assert text.startswith("Respuesta:")
    assert sessions.get_session("heartbeat_t1", "claude")[1] is True


def test_app_builds_with_remote_bridge_enabled(deployment, monkeypatch):
    """Telegram wiring + plugin registration, no network involved."""
    from maxbot.app import build_app
    monkeypatch.setenv("BRIDGE_MAXBOT_TOKEN", "m" * 40)
    monkeypatch.setenv("BRIDGE_AGENT_TOKEN", "a" * 40)
    toml = (deployment / "bot.toml").read_text() + (
        '\n[plugins]\nenabled = ["tasks", "remote_bridge"]\n'
        '[plugins.remote_bridge]\ngateway_url = "http://127.0.0.1:1"\n')
    (deployment / "bot.toml").write_text(toml)
    app, ctx = build_app(deployment / "bot.toml")
    import os
    assert "remote_bridge" in ctx.shared["capabilities"]
    assert "maxbot.plugins.remote_bridge.cli" in ctx.shared["capabilities_manifest"]
    # the approving credential is no longer visible to runner subprocesses
    assert "BRIDGE_MAXBOT_TOKEN" not in os.environ
    assert os.environ.get("BRIDGE_AGENT_TOKEN") == "a" * 40
    handlers = [type(h).__name__ for group in app.handlers.values() for h in group]
    assert "CallbackQueryHandler" in handlers and "CommandHandler" in handlers


def test_app_builds_without_bridge_token(deployment, monkeypatch, caplog):
    from maxbot.app import build_app
    monkeypatch.delenv("BRIDGE_MAXBOT_TOKEN", raising=False)
    (deployment / "bot.toml").write_text((deployment / "bot.toml").read_text()
                                         + '\n[plugins]\nenabled = ["remote_bridge"]\n')
    app, ctx = build_app(deployment / "bot.toml")      # must not crash the bot
    assert "plugin disabled" in caplog.text
