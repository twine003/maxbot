#!/usr/bin/env python3
"""One-command installer for maxbot.

Usage:
    python install.py            # set up .venv, install maxbot, scaffold deployment
    python install.py --name foo # name the scaffolded deployment folder "foo"

What it does (idempotent — safe to re-run):
  1. Creates a virtual environment in `.venv/` (if missing).
  2. Installs maxbot (editable) into that venv.
  3. Scaffolds `deployments/<name>/` from `deployments/example/` (if missing),
     copying `.env.example` -> `.env` so you only have to fill in your token.
  4. Prints the exact commands to configure and run the bot.

No secrets are touched. The bot will not start until you put your Telegram token
in the deployment's `.env`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"


def venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def run(cmd: list[str]) -> None:
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    subprocess.check_call(cmd)


def main() -> int:
    parser = argparse.ArgumentParser(description="Install maxbot.")
    parser.add_argument("--name", default="my-bot", help="Name of the deployment folder to scaffold.")
    parser.add_argument("--no-prompt", action="store_true", help="Skip the interactive token prompt.")
    args = parser.parse_args()

    if sys.version_info < (3, 10):
        print("maxbot requires Python 3.10 or newer. You have", sys.version)
        return 1

    # 1. venv
    if not venv_python().exists():
        print("==> Creating virtual environment in .venv/")
        venv.EnvBuilder(with_pip=True).create(VENV_DIR)
    else:
        print("==> Reusing existing .venv/")

    py = str(venv_python())

    # 2. install maxbot
    print("==> Installing maxbot into the virtual environment")
    run([py, "-m", "pip", "install", "--upgrade", "pip"])
    run([py, "-m", "pip", "install", "-e", str(ROOT)])

    # 3. scaffold deployment
    example = ROOT / "deployments" / "example"
    target = ROOT / "deployments" / args.name
    env_file = target / ".env"
    if target.exists():
        print(f"==> Deployment '{args.name}' already exists — leaving it untouched.")
    else:
        print(f"==> Scaffolding deployment '{args.name}' from the example")
        shutil.copytree(example, target)
        env_example = target / ".env.example"
        if env_example.exists() and not env_file.exists():
            shutil.copy(env_example, env_file)

    # 3b. The ONLY secret needed up front is the Telegram bot token. Everything
    # else (which chat owns the bot, which AI agent, its persona) is configured
    # by talking to the bot itself on Telegram after it starts.
    if not args.no_prompt and _env_value(env_file, "TELEGRAM_BOT_TOKEN") == "":
        print(
            "\n==> Telegram bot token\n"
            "    Create a bot with @BotFather on Telegram and paste its token here.\n"
            "    (Leave blank to fill it in later in the .env file.)"
        )
        try:
            token = input("    TELEGRAM_BOT_TOKEN = ").strip()
        except EOFError:
            token = ""
        if token:
            _set_env_value(env_file, "TELEGRAM_BOT_TOKEN", token)
            print("    Saved.")

    # 4. next steps
    activate = (
        r".venv\Scripts\activate" if os.name == "nt" else "source .venv/bin/activate"
    )
    have_token = _env_value(env_file, "TELEGRAM_BOT_TOKEN") != ""
    token_step = (
        "" if have_token
        else f"  0. Put your bot token in deployments/{args.name}/.env (TELEGRAM_BOT_TOKEN=...)\n\n"
    )
    print(
        "\n"
        "============================================================\n"
        " maxbot is installed.  Final steps:\n"
        "============================================================\n"
        f"{token_step}"
        "  1. Start the bot:\n"
        f"       {activate}\n"
        f"       maxbot --config deployments/{args.name}/bot.toml\n"
        "\n"
        "  2. The console will print a PAIRING CODE. Open Telegram, message\n"
        "     your bot, and send that code to claim it.\n"
        "\n"
        "  3. The bot then walks you through choosing + installing an AI agent\n"
        "     (Claude Code / Codex), authenticating it, and writing its persona\n"
        "     — all from the chat. No more files to edit.\n"
        "============================================================\n"
    )
    return 0


def _env_value(env_file: Path, key: str) -> str:
    if not env_file.exists():
        return ""
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return ""


def _set_env_value(env_file: Path, key: str, value: str) -> None:
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
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


if __name__ == "__main__":
    raise SystemExit(main())
