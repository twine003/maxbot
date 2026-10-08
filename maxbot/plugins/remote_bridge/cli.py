"""Device tools for the model — stdlib only, so it runs from any shell the runner has.

    python -m maxbot.plugins.remote_bridge.cli devices
    python -m maxbot.plugins.remote_bridge.cli call <device> <tool> [--args '{"k": "v"}']
                                                    [--wait-approval 180] [--out DIR]

Uses BRIDGE_AGENT_TOKEN (and BRIDGE_INTERNAL_URL, default http://127.0.0.1:8771).
That credential can list devices and request calls, never approve them: tools
marked "confirm" make the owner tap Aprobar in Telegram, and this command waits
for that decision. Images in a result are saved to --out and printed as
`[ADJUNTO:<path>]` lines, which MaxBot turns into Telegram attachments.

Exit codes: 0 ok · 1 tool failed · 2 refused/denied/expired · 3 gateway unreachable.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def _req(method: str, path: str, body: dict | None = None, timeout: float = 60.0) -> tuple[int, dict]:
    base = os.environ.get("BRIDGE_INTERNAL_URL", "http://127.0.0.1:8771").rstrip("/")
    token = os.environ.get("BRIDGE_AGENT_TOKEN", "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {"error": str(e)}


def _emit(result: dict, out_dir: Path) -> int:
    content = result.get("content") or []
    printable = {k: v for k, v in result.items() if k != "content"}
    texts = []
    for item in content:
        if item.get("type") == "image":
            ext = ".jpg" if "jpeg" in item.get("mimeType", "") else ".png"
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"captura-{int(time.time())}-{uuid.uuid4().hex[:6]}{ext}"
            path.write_bytes(base64.b64decode(item.get("data", "")))
            texts.append(f"[ADJUNTO:{path}]")
        elif item.get("type") == "text":
            texts.append(item.get("text", ""))
    print(json.dumps(printable, ensure_ascii=False))
    for t in texts:
        print(t)
    return 0 if result.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="remote_bridge.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("devices")
    c = sub.add_parser("call")
    c.add_argument("device")
    c.add_argument("tool")
    c.add_argument("--args", default="{}")
    c.add_argument("--timeout", type=float, default=30)
    c.add_argument("--wait-approval", type=float, default=180)
    c.add_argument("--out", type=Path, default=Path(tempfile.gettempdir()) / "alejandro-bridge")
    a = ap.parse_args(argv)

    if not os.environ.get("BRIDGE_AGENT_TOKEN"):
        print("BRIDGE_AGENT_TOKEN no está definido", file=sys.stderr)
        return 3
    try:
        if a.cmd == "devices":
            status, body = _req("GET", "/internal/v1/devices", timeout=10)
            print(json.dumps(body, ensure_ascii=False, indent=2))
            return 0 if status == 200 else 3
        try:
            arguments = json.loads(a.args)
        except ValueError:
            print("--args debe ser JSON", file=sys.stderr)
            return 2
        payload = {"tool": a.tool, "arguments": arguments, "timeout": a.timeout,
                   "request_id": str(uuid.uuid4()), "requested_by": "agent-cli"}
        status, body = _req("POST", f"/internal/v1/devices/{a.device}/call", payload, timeout=a.timeout + 10)
        if status == 202 and body.get("status") == "approval_required":
            aid = body["approval_id"]
            print(f"Esperando aprobación del dueño en Telegram (id {aid})…", file=sys.stderr)
            deadline = time.monotonic() + a.wait_approval
            while time.monotonic() < deadline:
                time.sleep(2)
                _, st = _req("GET", f"/internal/v1/approvals/{aid}", timeout=10)
                if st.get("status") == "executed" and st.get("result"):
                    return _emit(st["result"], a.out)
                if st.get("status") in ("denied", "expired"):
                    print(json.dumps({"ok": False, "status": st.get("status")}))
                    return 2
            print(json.dumps({"ok": False, "status": "approval_timeout"}))
            return 2
        if status != 200:
            print(json.dumps(body, ensure_ascii=False))
            return 2 if status in (403, 404, 413, 422) else 3
        return _emit(body, a.out)
    except (urllib.error.URLError, OSError) as e:
        print(json.dumps({"ok": False, "status": "gateway_unreachable", "error": str(e)}))
        return 3


if __name__ == "__main__":
    sys.exit(main())
