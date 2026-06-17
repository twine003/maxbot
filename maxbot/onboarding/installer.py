"""Agent-CLI detection, installation and auth helpers used by the wizard.

Everything here is best-effort and defensive: a fresh machine may not have
Node/npm, the CLIs change their flags over time, and auth is partly interactive.
Each function returns enough information for the wizard to guide the user, and
falls back to clear manual instructions when automation isn't possible.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from ..runners._common import find_in_npm_globals, find_in_user_local_bin

# Agent name -> how to find / install it.
AGENTS = {
    "claude": {
        "label": "Claude Code",
        "cli": "claude",
        "npm_package": "@anthropic-ai/claude-code",
        "env_key": "ANTHROPIC_API_KEY",
        "key_prefix": "sk-ant-",
        "console_url": "https://console.anthropic.com/settings/keys",
        "login_cmd": "claude",
    },
    "codex": {
        "label": "Codex",
        "cli": "codex",
        "npm_package": "@openai/codex",
        "env_key": "OPENAI_API_KEY",
        "key_prefix": "sk-",
        "console_url": "https://platform.openai.com/api-keys",
        "login_cmd": "codex login",
    },
}


def find_cli(name: str) -> str | None:
    """Locate an agent CLI: PATH, ~/.local/bin, then npm global dirs."""
    found = shutil.which(name)
    if found:
        return found
    found = find_in_user_local_bin(name)
    if found:
        return found
    return find_in_npm_globals(name)


def npm_path() -> str | None:
    return shutil.which("npm") or find_in_npm_globals("npm")


def node_path() -> str | None:
    return shutil.which("node")


async def _run(cmd: list[str], timeout: int = 600) -> tuple[int, str]:
    """Run a command, capture combined stdout+stderr (tail), return (rc, text)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as e:
        return 127, str(e)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "timeout"
    text = (out or b"").decode("utf-8", errors="replace")
    return proc.returncode or 0, text


async def install_agent(agent: str) -> tuple[bool, str]:
    """`npm install -g <package>` for the chosen agent. Returns (ok, log_tail)."""
    spec = AGENTS[agent]
    npm = npm_path()
    if not npm:
        return False, "npm-not-found"
    rc, text = await _run([npm, "install", "-g", spec["npm_package"]], timeout=900)
    tail = "\n".join(text.strip().splitlines()[-12:])
    if rc != 0:
        return False, tail or f"npm exited {rc}"
    # Confirm the CLI is now resolvable.
    return (find_cli(spec["cli"]) is not None), tail


async def verify_cli(agent: str) -> tuple[bool, str]:
    """Run `<cli> --version` to confirm the binary works."""
    spec = AGENTS[agent]
    cli = find_cli(spec["cli"])
    if not cli:
        return False, "not-found"
    rc, text = await _run([cli, "--version"], timeout=60)
    return rc == 0, text.strip()


def write_env_key(env_file: Path, key: str, value: str) -> None:
    """Upsert KEY=value in the deployment .env (creating it if needed)."""
    env_file.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if env_file.exists():
        lines = env_file.read_text(encoding="utf-8").splitlines()
    out, replaced = [], False
    for line in lines:
        if line.strip().startswith(f"{key}="):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    env_file.write_text("\n".join(out) + "\n", encoding="utf-8")
