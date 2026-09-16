"""One Second Brain at a time on 127.0.0.1:8787.

Double-clicking start.bat while the app is already running — it starts
minimized at login (story B1), so it usually is — used to end in:

    [Errno 10048] error while attempting to bind on address ('127.0.0.1', 8787)

Worse, the copy already running is the code as it was at login. After an
update the new code never loads until that process is closed by hand, and
nothing says so.

Before binding, `run.py` now asks who is on the port:

  * **Nothing** — start normally.
  * **Second Brain, same code** — open the browser on it and exit 0.
  * **Second Brain, older code** (or `--restart`) — ask it to shut down
    (`POST /api/admin/shutdown`), and if it is an older build without that
    endpoint, end the process that owns the port — only after it has
    answered as Second Brain *and* is a python process.
  * **Something else** — say which program holds the port and stop,
    rather than touching it.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

APP = "secondbrain"
ROOT = Path(__file__).resolve().parent.parent


def code_fingerprint(root: Path = ROOT) -> str:
    """A short hash of the code a server would run: `sb/**/*.py` and the pages.

    Content, not mtimes, so a git checkout that rewrites a file with the same
    bytes is not an update.
    """
    h = hashlib.sha1()
    base = Path(root) / "sb"
    files = sorted(
        p for p in base.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
        and p.suffix in (".py", ".html", ".js", ".css")
    )
    for p in files:
        h.update(p.relative_to(base).as_posix().encode())
        try:
            h.update(p.read_bytes())
        except OSError:
            continue
    return h.hexdigest()[:12]


def port_in_use(host: str, port: int, timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((_dial(host), int(port))) == 0


def _dial(host: str) -> str:
    return "127.0.0.1" if host in ("", "0.0.0.0", "::") else host


def _get_json(url: str, timeout: float) -> Optional[Dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
            return data if isinstance(data, dict) else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def probe(host: str, port: int, timeout: float = 4.0) -> Optional[Dict[str, Any]]:
    """Who is listening, or None when nobody is.

    `{"app": "secondbrain", "code": ..., "pid": ...}` for this app — `code`
    and `pid` are None for builds older than `/api/ping` — and
    `{"app": "other"}` for anything else.
    """
    if not port_in_use(host, port):
        return None
    base = f"http://{_dial(host)}:{int(port)}"
    ping = _get_json(base + "/api/ping", timeout)
    if ping and ping.get("app") == APP:
        return ping
    # Every build since phase 1 answers this with `vault_exists`.
    health = _get_json(base + "/api/health", timeout)
    if health and "vault_exists" in health:
        return {"app": APP, "code": None, "pid": None, "vault": health.get("vault")}
    return {"app": "other"}


# -- who owns the port --------------------------------------------------------


def parse_netstat(text: str, port: int) -> Optional[int]:
    """The PID listening on `port`, from `netstat -ano` output (Windows)."""
    suffix = f":{int(port)}"
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        local, state, pid = parts[1], parts[3].upper(), parts[-1]
        if local.endswith(suffix) and state in ("LISTENING", "ABHÖREN", "ESCUCHANDO") and pid.isdigit():
            return int(pid)
    return None


def parse_tasklist(text: str) -> str:
    """The image name from `tasklist /FO CSV /NH` output."""
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    if not line.startswith('"'):
        return ""
    return line.split('","', 1)[0].strip('"')


def _run(cmd) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return out.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def pid_on_port(port: int) -> Optional[int]:
    if os.name == "nt":
        return parse_netstat(_run(["netstat", "-ano", "-p", "tcp"]), port)
    text = _run(["lsof", "-t", f"-iTCP:{int(port)}", "-sTCP:LISTEN"]).split()
    return int(text[0]) if text and text[0].isdigit() else None


def process_name(pid: int) -> str:
    if not pid:
        return ""
    if os.name == "nt":
        return parse_tasklist(_run(["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"]))
    try:
        return Path(f"/proc/{int(pid)}/comm").read_text().strip()
    except OSError:
        return ""


def _end_process(pid: int) -> None:
    if os.name == "nt":
        _run(["taskkill", "/PID", str(int(pid)), "/T", "/F"])
    else:
        import signal

        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError:
            pass


# -- stopping the running copy -------------------------------------------------


def request_shutdown(host: str, port: int, timeout: float = 4.0) -> bool:
    req = urllib.request.Request(
        f"http://{_dial(host)}:{int(port)}/api/admin/shutdown",
        data=b"{}", method="POST",
        headers={"Content-Type": "application/json", "X-SB-Action": "1"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
            return bool(data.get("stopping"))
    except (urllib.error.URLError, OSError, ValueError):
        return False


def wait_free(host: str, port: int, seconds: float = 10.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not port_in_use(host, port):
            return True
        time.sleep(0.25)
    return not port_in_use(host, port)


def stop(host: str, port: int, *, say=print) -> bool:
    """Stop the Second Brain on the port. True once the port is free."""
    if request_shutdown(host, port) and wait_free(host, port, 15):
        say("  stopped the running copy.")
        return True
    # An older build: no shutdown endpoint. End its process — but only a
    # python process, and only because it has just answered as this app.
    pid = pid_on_port(port)
    name = process_name(pid) if pid else ""
    if not pid or not name.lower().startswith("python"):
        say(f"  could not stop it automatically (port owner: {name or 'unknown'} {pid or ''}).")
        return False
    say(f"  ending the older copy ({name}, PID {pid})…")
    _end_process(pid)
    return wait_free(host, port, 10)


def describe_other(port: int) -> str:
    pid = pid_on_port(port)
    name = process_name(pid) if pid else ""
    who = f"{name} (PID {pid})" if pid else "another program"
    return (
        f"Port {port} is already used by {who}, which is not Second Brain.\n"
        f"Close that program, or set a different `port:` in config.yaml."
    )


def ping_payload(cfg: Any, code: str, started: str) -> Dict[str, Any]:
    return {
        "app": APP,
        "code": code,
        "pid": os.getpid(),
        "started": started,
        "vault": str(getattr(cfg, "vault", "")),
        "python": sys.version.split()[0],
    }
