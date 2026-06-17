"""Map raw tool-use events (Claude or Codex) to friendly user-facing strings."""

from __future__ import annotations

import re


def friendly_bash(cmd: str) -> str:
    """Translate common bash commands into a one-line description in Spanish."""
    c = cmd.strip()
    c = re.sub(r'^sleep\s+\d+\s*&&\s*', '', c).strip()
    if "ssh" in c and "docker exec" in c:
        if "bench build" in c:
            server = re.search(r"ssh\s+(\S+)", c)
            srv = server.group(1) if server else "servidor"
            return f"Compilando assets en {srv}"
        if "bench migrate" in c:
            return "Ejecutando migraciones de base de datos"
        if "bench install-app" in c:
            app = re.search(r"install-app\s+(\S+)", c)
            return f"Instalando app {app.group(1) if app else ''}"
        if "manage.py shell" in c or "manage.py" in c:
            return "Consultando Django en servidor remoto"
        if "bench" in c:
            return "Ejecutando comando bench en servidor"
        return "Ejecutando tarea en servidor remoto"
    if c.startswith("ssh") or re.match(r'^ssh\s', c):
        server = re.search(r'ssh\s+(?:-\S+\s+(?:\S+\s+)?)*(\S+)', c)
        if not server:
            server = re.search(r'ssh\s+(\S+)', c)
        srv = server.group(1) if server else "servidor"
        if "docker" in c:
            return f"Verificando contenedores en {srv}"
        if "service" in c or "systemctl" in c:
            return f"Verificando servicios en {srv}"
        return f"Conectando a {srv}"
    if "docker" in c:
        if "restart" in c:
            return "Reiniciando contenedores"
        if "logs" in c:
            return "Revisando logs de contenedor"
        if "ps" in c or "service ls" in c:
            return "Listando contenedores/servicios"
        return "Operación Docker"
    if "git" in c:
        if "pull" in c:
            return "Descargando cambios de Git"
        if "push" in c:
            return "Subiendo cambios a Git"
        if "commit" in c:
            return "Creando commit"
        return "Operación Git"
    if "bench build" in c:
        return "Compilando assets de Frappe"
    if "psql" in c or "postgres" in c.lower():
        return "Consultando base de datos"
    if "curl" in c:
        return "Consultando API"
    if "systemctl" in c:
        return "Gestionando servicios del sistema"
    return c[:60]


def extract_claude_activity(event: dict) -> str | None:
    """Read a Claude --output-format=stream-json 'assistant' event and pull the tool name."""
    if event.get("type") != "assistant":
        return None
    msg = event.get("message", {})
    content_list = msg.get("content", [])
    for item in content_list:
        if item.get("type") != "tool_use":
            continue
        name = item.get("name", "")
        inp = item.get("input", {})
        if name == "Bash":
            return friendly_bash(inp.get("command", ""))
        if name == "Read":
            p = inp.get("file_path", "?")
            fname = p.rsplit("/", 1)[-1] if "/" in p else p
            return f"Leyendo {fname}"
        if name in ("Write", "Edit"):
            p = inp.get("file_path", "?")
            fname = p.rsplit("/", 1)[-1] if "/" in p else p
            action = "Escribiendo" if name == "Write" else "Editando"
            return f"{action} {fname}"
        if name == "Grep":
            return f"Buscando en código: '{inp.get('pattern', '?')[:40]}'"
        if name == "Glob":
            return "Buscando archivos"
        if name == "Task":
            desc = inp.get("description", "")
            return f"Sub-tarea: {desc}" if desc else "Ejecutando sub-tarea"
        if name == "WebSearch":
            return "Buscando en web"
        if name == "WebFetch":
            return "Consultando página web"
        if name.startswith("mcp__GitLab__"):
            return f"GitLab: {name.replace('mcp__GitLab__', '').replace('_', ' ')}"
        if name.startswith("mcp__n8n__"):
            return f"n8n: {name.replace('mcp__n8n__', '').replace('_', ' ')}"
        return f"Usando {name}"
    return None


def extract_codex_activity(item: dict) -> str | None:
    """Map a Codex item (item.started / item.completed) to a friendly description.

    Codex emits items with type: command_execution, agent_message, file_change,
    file_read, web_search, mcp_tool_call, reasoning, error.
    """
    item_type = item.get("type", "")

    if item_type == "command_execution":
        cmd = (item.get("command") or "").strip()
        # Codex on Windows wraps commands in powershell.exe -Command "bash -lc '...'"
        # — unwrap so friendly_bash sees the real command.
        m = re.search(r"bash\s+-l?c\s+['\"](.+?)['\"]\s*$", cmd, re.DOTALL)
        if m:
            cmd = m.group(1)
        else:
            m = re.search(r"cmd(?:\.exe)?\s+/[cC]\s+['\"](.+?)['\"]\s*$", cmd, re.DOTALL)
            if m:
                cmd = m.group(1)
            else:
                m = re.search(
                    r"powershell(?:\.exe)?\s+(?:-\S+\s+)*-Command\s+['\"](.+?)['\"]\s*$",
                    cmd, re.DOTALL,
                )
                if m:
                    cmd = m.group(1)
        cmd = cmd.replace('\\\\', '\\').replace('\\"', '"')
        return friendly_bash(cmd)

    if item_type == "file_change":
        path = item.get("path") or item.get("file_path") or "?"
        fname = path.rsplit("/", 1)[-1] if "/" in path else path.rsplit("\\", 1)[-1]
        return f"Editando {fname}"

    if item_type == "file_read":
        path = item.get("path") or item.get("file_path") or "?"
        fname = path.rsplit("/", 1)[-1] if "/" in path else path.rsplit("\\", 1)[-1]
        return f"Leyendo {fname}"

    if item_type == "web_search":
        q = item.get("query") or ""
        return f"Buscando en web: '{q[:40]}'" if q else "Buscando en web"

    if item_type == "mcp_tool_call":
        server = item.get("server") or ""
        tool = item.get("tool") or item.get("name") or ""
        if server and tool:
            return f"MCP {server}: {tool}"
        return f"MCP: {tool or server or 'tool'}"

    if item_type == "reasoning":
        return "Razonando..."

    if item_type == "agent_message":
        return None  # text goes to the live message, not the activity slot

    if not item_type:
        return None

    return f"Usando {item_type}"
