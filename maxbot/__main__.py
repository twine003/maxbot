"""`python -m maxbot --config bot.toml` entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .app import build_app


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="maxbot",
        description="Telegram bot framework with pluggable runners + plugins.",
    )
    parser.add_argument(
        "--config", "-c", required=True, type=Path,
        help="Path to a bot.toml deployment config.",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Validate config and exit (no polling).",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        help="Python logging level (DEBUG, INFO, WARNING, ERROR).",
    )
    args = parser.parse_args()

    log_level = getattr(logging, args.log_level.upper(), logging.INFO)
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=log_level,
    )
    if log_level > logging.DEBUG:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
    log = logging.getLogger("maxbot")

    try:
        app, ctx = build_app(args.config)
    except Exception as e:
        log.error("Bootstrap failed: %s", e)
        return 1

    if args.check:
        log.info("Config OK. Runners: %s. Plugins: %s",
                 ctx.runners.all_names(), ctx.config.plugins.enabled)
        return 0

    log.info(
        "maxbot starting (workspace=%s, default=%s, runners=%s, plugins=%s)",
        ctx.config.workspace, ctx.config.default_model,
        ctx.runners.all_names(), ctx.config.plugins.enabled,
    )
    app.run_polling(drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
