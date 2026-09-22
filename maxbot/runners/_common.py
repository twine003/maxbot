"""Shared subprocess + env helpers for runners."""

from __future__ import annotations

import os
import re
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


_WRAPPER_TARGET_RE = re.compile(r'"%dp0%\\?([^"]+\.exe)"', re.IGNORECASE)


def resolve_windows_wrapper(path: str) -> str:
    """Windows: cambiar el `.cmd` de npm por el ejecutable real al que llama.

    Lanzar un `.cmd` obliga a Windows a meter `cmd.exe /c` en el medio, y
    cmd.exe CORTA la línea de comandos en el primer salto de línea que traiga un
    argumento. No se pierde solo el resto del texto: se pierden TODAS las
    banderas que venían después (`--resume`, `--append-system-prompt-file`,
    `--output-format`, `--allowedTools`…), así que el turno corre sin sesión,
    sin identidad y sin streaming.

    Comprobado el 2026-09-11 con el CLI real: un prompt de dos líneas pasado por
    `claude.cmd` llegó al modelo con una sola línea ("tu mensaje tiene una sola
    línea", respondió), y pasado al `.exe` directamente llegó entero. En
    Telegram esto se dispara con CUALQUIER mensaje de varias líneas.

    Devuelve la ruta original si no es un wrapper o si no se puede resolver.
    """
    if sys.platform != "win32":
        return path
    p = Path(path)
    if p.suffix.lower() not in (".cmd", ".bat"):
        return path
    try:
        texto = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return path
    for m in _WRAPPER_TARGET_RE.finditer(texto):
        destino = p.parent / m.group(1)
        if destino.exists():
            return str(destino)
    hermano = p.with_suffix(".exe")
    if hermano.exists():
        return str(hermano)
    # Wrappers que llaman a un .js por node (p.ej. codex.cmd) siguen pasando por
    # cmd.exe: para esos habría que invocar `node <script>`, que es otro cambio.
    return path


def resolve_user_home(executable_path: str) -> Path:
    """If the executable lives in <home>/.local/bin, return <home>. Else Path.home()."""
    parts = Path(executable_path).parts
    if ".local" in parts:
        idx = parts.index(".local")
        candidate = Path(*parts[:idx])
        if candidate.exists():
            return candidate
    return Path.home()


# Env vars scrubbed before launching the Claude CLI so inherited shell env can't
# silently redirect provider routing, the config root, or telemetry to somewhere
# other than what the deployment intends. NOTE: unlike some wrappers we KEEP
# ANTHROPIC_API_KEY / *_OAUTH_TOKEN — maxbot's onboarding may set the API key in
# the deployment .env, and the CLI's own browser login lives in those too.
CLAUDE_ENV_DROP = (
    "CLAUDECODE",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)
CLAUDE_ENV_DROP_PREFIXES = ("OTEL_",)


def patched_env(
    executable_path: str,
    drop_keys: tuple[str, ...] = (),
    drop_prefixes: tuple[str, ...] = (),
) -> dict:
    """Build a subprocess env with HOME / USERPROFILE / APPDATA corrected for SYSTEM service.

    `drop_keys` removes exact env var names; `drop_prefixes` removes every var
    whose name starts with one of the given prefixes (e.g. "OTEL_").
    """
    env = os.environ.copy()
    for k in drop_keys:
        env.pop(k, None)
    if drop_prefixes:
        for k in list(env):
            if k.startswith(drop_prefixes):
                env.pop(k, None)
    home = resolve_user_home(executable_path)
    env["HOME"] = str(home)
    if sys.platform == "win32":
        env["USERPROFILE"] = str(home)
        env["APPDATA"] = str(home / "AppData" / "Roaming")
        env["LOCALAPPDATA"] = str(home / "AppData" / "Local")
    return env
