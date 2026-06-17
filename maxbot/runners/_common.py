"""Shared subprocess + env helpers for runners."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def find_in_user_local_bin(executable: str) -> str | None:
    """Search ~/.local/bin and (on Windows) every C:\\Users\\<user>\\.local\\bin."""
    candidates: list[str] = [
        str(Path.home() / ".local" / "bin" / f"{executable}.exe"),
        str(Path.home() / ".local" / "bin" / executable),
    ]
    if sys.platform == "win32":
        users_root = Path(os.environ.get("SystemDrive", "C:") + "\\Users")
        skip = {"Public", "Default", "Default User", "All Users"}
        if users_root.exists():
            for user_dir in sorted(users_root.iterdir()):
                if user_dir.is_dir() and user_dir.name not in skip:
                    candidates.append(str(user_dir / ".local" / "bin" / f"{executable}.exe"))
                    candidates.append(str(user_dir / ".local" / "bin" / executable))
    for c in candidates:
        if Path(c).exists():
            return c
    return None


def find_in_npm_globals(executable: str) -> str | None:
    """Search npm global install locations on Windows."""
    candidates: list[str] = []
    if sys.platform == "win32":
        users_root = Path(os.environ.get("SystemDrive", "C:") + "\\Users")
        skip = {"Public", "Default", "Default User", "All Users"}
        if users_root.exists():
            for user_dir in sorted(users_root.iterdir()):
                if user_dir.is_dir() and user_dir.name not in skip:
                    candidates += [
                        str(user_dir / "AppData" / "Roaming" / "npm" / f"{executable}.cmd"),
                        str(user_dir / "AppData" / "Roaming" / "npm" / executable),
                    ]
        prog_files = os.environ.get("ProgramFiles", "C:\\Program Files")
        candidates += [
            str(Path(prog_files) / "nodejs" / f"{executable}.cmd"),
            str(Path(prog_files) / "nodejs" / executable),
        ]
    for c in candidates:
        if Path(c).exists():
            return c
    return None


def resolve_user_home(executable_path: str) -> Path:
    """If the executable lives in <home>/.local/bin, return <home>. Else Path.home()."""
    parts = Path(executable_path).parts
    if ".local" in parts:
        idx = parts.index(".local")
        candidate = Path(*parts[:idx])
        if candidate.exists():
            return candidate
    return Path.home()


def patched_env(executable_path: str, drop_keys: tuple[str, ...] = ()) -> dict:
    """Build a subprocess env with HOME / USERPROFILE / APPDATA corrected for SYSTEM service."""
    env = os.environ.copy()
    for k in drop_keys:
        env.pop(k, None)
    home = resolve_user_home(executable_path)
    env["HOME"] = str(home)
    if sys.platform == "win32":
        env["USERPROFILE"] = str(home)
        env["APPDATA"] = str(home / "AppData" / "Roaming")
        env["LOCALAPPDATA"] = str(home / "AppData" / "Local")
    return env
