"""Heartbeat plugin — periodic tick that runs scheduled tasks + cleans media."""

from .plugin import HeartbeatPlugin as Plugin

__all__ = ["Plugin"]
