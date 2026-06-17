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
    if target.exists():
        print(f"==> Deployment '{args.name}' already exists — leaving it untouched.")
    else:
        print(f"==> Scaffolding deployment '{args.name}' from the example")
        shutil.copytree(example, target)
        env_example = target / ".env.example"
        env_file = target / ".env"
        if env_example.exists() and not env_file.exists():
            shutil.copy(env_example, env_file)

    # 4. next steps
    activate = (
        r".venv\Scripts\activate" if os.name == "nt" else "source .venv/bin/activate"
    )
    print(
        "\n"
        "============================================================\n"
        " maxbot is installed.  Two steps left:\n"
        "============================================================\n"
        f"  1. Edit deployments/{args.name}/.env and set:\n"
        "       TELEGRAM_BOT_TOKEN=<token from @BotFather>\n"
        "       ALLOWED_CHAT_IDS=<your Telegram chat id>\n"
        "\n"
        "  2. Activate the venv and run the bot:\n"
        f"       {activate}\n"
        f"       maxbot --config deployments/{args.name}/bot.toml\n"
        "\n"
        "  Tip: validate the config without polling with\n"
        f"       maxbot --config deployments/{args.name}/bot.toml --check\n"
        "============================================================\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
