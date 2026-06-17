"""Recurring + one-time task persistence (used by the heartbeat plugin)."""

from __future__ import annotations

import json
import re
from pathlib import Path


class TaskStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        if not self.path.exists():
            return {"recurring": [], "one_time": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"recurring": [], "one_time": []}
        data.setdefault("recurring", [])
        data.setdefault("one_time", [])
        return data

    def save(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def parse_interval(text: str) -> int | None:
    """Best-effort parse of 'cada N min|h|horas' / 'diario' patterns. Returns minutes."""
    t = text.lower()
    if "diario" in t or "cada dia" in t or "cada día" in t:
        return 1440
    if "cada hora" in t and not re.search(r"cada\s+\d+\s*hora", t):
        return 60
    m = re.search(r"cada\s+(\d+)\s*h(?:oras?)?", t)
    if m:
        return int(m.group(1)) * 60
    m = re.search(r"cada\s+(\d+)\s*min(?:utos?)?", t)
    if m:
        return int(m.group(1))
    return None
