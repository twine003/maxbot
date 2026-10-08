"""Test helpers: a deployment on disk, a fake Claude CLI, and (optionally) the
real Alejandro gateway from a sibling checkout of ops-mcp-server."""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest

FAKE_CLAUDE = textwrap.dedent(r'''
    import json, os, sys
    argv = sys.argv[1:]
    log = os.environ.get("FAKE_CLAUDE_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(argv) + "\n")
    prompt = argv[argv.index("-p") + 1]
    sid = argv[argv.index("--resume") + 1] if "--resume" in argv else argv[argv.index("--session-id") + 1]
    if "FAIL" in prompt:
        print(json.dumps({"type": "result", "is_error": True, "result": "boom", "session_id": sid}))
    else:
        print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}))
        print(json.dumps({"type": "result", "result": "Respuesta: " + prompt[-40:], "session_id": sid}))
''')


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    """A minimal bot.toml + .env, as an operator would write them."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST-TOKEN-NOT-REAL")
    (tmp_path / "bot.toml").write_text(textwrap.dedent("""
        [bot]
        allowed_chat_ids = [111]
        default_model = "claude"
        system_instruction = "Eres Alejandro."
        allowed_tools = ["Bash(*)", "Read(*)"]
    """), encoding="utf-8")
    return tmp_path


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    log = tmp_path / "claude_calls.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))

    def patch(runner):
        orig = runner.build_command

        def build(*a, **kw):
            cmd = orig(*a, **kw)
            return [sys.executable, str(script), *cmd[1:]]
        runner.build_command = build
        return log
    return patch


def gateway_module():
    """Import the real gateway from ../ops-mcp-server-bridge, or skip."""
    here = Path(__file__).resolve().parents[1]
    for cand in (os.environ.get("REMOTE_BRIDGE_PATH"), here.parent / "ops-mcp-server-bridge",
                 here.parent / "ops-mcp-server"):
        if cand and (Path(cand) / "remote_bridge" / "gateway.py").exists():
            if str(cand) not in sys.path:
                sys.path.insert(0, str(cand))
            try:
                import remote_bridge.gateway  # noqa: F401
                return True
            except ImportError:
                continue
    pytest.skip("remote_bridge gateway not available (set REMOTE_BRIDGE_PATH)")
