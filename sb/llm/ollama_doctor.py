"""Is Ollama really working — and if not, which step is broken and how to fix it.

`OllamaProvider.available()` answers one question: did `/api/tags` return
200 within two seconds. That is enough to choose between the model and the
rule-based fallback, but not enough to tell lj *why* the model is off. Four
very different faults all showed up as the same grey dot in the footer:

  1. Ollama is not installed, or not where Windows puts it.
  2. It is installed but not running (the tray app was quit, or never
     started after a reboot).
  3. Something else is listening on the port.
  4. The server answers, but the configured model is not pulled, or cannot
     load (not enough VRAM), so every real call fails anyway.

`check()` walks those in order and stops explaining at the first break, so
the answer is one sentence and one button, not a wall of red. `fix()` does
the parts a program can safely do on its own: start the server, and — only
when asked — pull the missing models. Neither sends anything but the
configured model names to the local server.

Sources for the endpoints and paths used here:
  * API: https://github.com/ollama/ollama/blob/main/docs/api.md
    (`/api/version`, `/api/tags`, `/api/pull`, `/api/generate`)
  * Windows: https://docs.ollama.com/windows
    (binaries in `%LOCALAPPDATA%\\Programs\\Ollama`, logs in
    `%LOCALAPPDATA%\\Ollama\\server.log`, the tray app starts the server,
    `ollama serve` starts it by hand, `OLLAMA_HOST` defaults to
    `http://localhost:11434`)
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from ..config import LLMConfig
from . import has_model, installed_models, _tags_cache

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"

#: How long `fix()` waits for a freshly started server to answer. The tray
#: app on a cold machine takes a few seconds; twenty is generous.
START_WAIT_S = 20.0

#: A model load on a 12GB card can take ~30 s the first time.
GENERATE_TIMEOUT_S = 90.0

#: Pulls are large (phi4 is ~9 GB). Long, but bounded.
PULL_TIMEOUT_S = 60 * 60.0


# --------------------------------------------------------------------------
# where things are
# --------------------------------------------------------------------------


def _endpoint(cfg: LLMConfig):
    url = urlparse(cfg.ollama_url if "://" in cfg.ollama_url else f"http://{cfg.ollama_url}")
    host = url.hostname or "localhost"
    port = url.port or 11434
    # 0.0.0.0 is a bind address; Windows will not connect to it.
    dial = "127.0.0.1" if host == "0.0.0.0" else host
    if ":" in dial:
        dial = f"[{dial}]"
    return host, port, f"{url.scheme or 'http'}://{dial}:{port}"


def _is_local(host: str) -> bool:
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def find_binary() -> Optional[Path]:
    """`ollama` on PATH, else the Windows installer's default location."""
    hit = shutil.which("ollama")
    if hit:
        return Path(hit)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        p = Path(local) / "Programs" / "Ollama" / "ollama.exe"
        if p.exists():
            return p
    return None


def find_tray_app() -> Optional[Path]:
    """The Windows tray app. Starting *it* rather than `ollama serve` is what
    the installer does at login, so the server comes back the same way it
    normally runs (and keeps its settings)."""
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    p = Path(local) / "Programs" / "Ollama" / "ollama app.exe"
    return p if p.exists() else None


def server_log() -> Optional[Path]:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    p = Path(local) / "Ollama" / "server.log"
    return p if p.exists() else None


def _log_tail(lines: int = 12) -> List[str]:
    p = server_log()
    if not p:
        return []
    try:
        data = p.read_bytes()[-8000:].decode("utf-8", errors="replace")
    except OSError:
        return []
    return [l for l in data.splitlines() if l.strip()][-lines:]


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1" if host == "0.0.0.0" else host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _version(base: str) -> Optional[str]:
    try:
        r = httpx.get(f"{base}/api/version", timeout=2.0)
        if r.status_code == 200:
            return str(r.json().get("version") or "?")
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------
# the check
# --------------------------------------------------------------------------


def _step(key: str, label: str, status: str, detail: str = "", fix: str = "", action: str = "") -> Dict[str, Any]:
    return {"key": key, "label": label, "status": status, "detail": detail,
            "fix": fix, "action": action}


def _wanted(cfg: LLMConfig) -> List[tuple]:
    out = [("chat", cfg.model)]
    if cfg.study_model and cfg.study_model != cfg.model:
        out.append(("study", cfg.study_model))
    if cfg.embed_model:
        out.append(("search", cfg.embed_model))
    return out


def check(cfg: LLMConfig, deep: bool = True) -> Dict[str, Any]:
    """Every link in the chain, in order. `deep` adds a real one-word
    generation and one embedding — the only proof a model can actually load."""
    steps: List[Dict[str, Any]] = []
    host, port, base = _endpoint(cfg)
    local = _is_local(host)

    def done(state: str, headline: str) -> Dict[str, Any]:
        failed = next((s for s in steps if s["status"] == FAIL), None)
        return {
            "state": state,            # ready | degraded | offline | off
            "headline": headline,
            "url": base,
            "steps": steps,
            "action": (failed or {}).get("action")
            or next((s["action"] for s in steps if s["action"]), ""),
            "log": _log_tail() if state == "offline" and local else [],
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    if (cfg.provider or "ollama").lower() != "ollama":
        steps.append(_step("provider", "Provider", SKIP,
                           f"llm.provider is {cfg.provider!r}, not ollama"))
        return done("off", f"Ollama is not in use (provider: {cfg.provider}).")

    # 1. installed — a hard failure only if nothing is answering either: a
    # server started some other way (a service, WSL) is still a server.
    binary = find_binary() if local else None
    version = _version(base)
    if not local:
        steps.append(_step("installed", "Ollama installed", SKIP, f"remote server at {host}"))
    elif binary:
        steps.append(_step("installed", "Ollama installed", OK, str(binary)))
    else:
        steps.append(_step(
            "installed", "Ollama installed", WARN if version else FAIL,
            "not on PATH or in %LOCALAPPDATA%\\Programs\\Ollama",
            "" if version else "Install it from https://ollama.com/download, then press Fix (or run check-ollama.bat).",
        ))

    # 2. listening / 3. is it Ollama
    if version is None:
        if _port_open(host, port):
            steps.append(_step(
                "server", "Server answering", FAIL,
                f"something is listening on port {port}, but it is not answering as Ollama",
                f"Another program has port {port}. Close it, or set OLLAMA_HOST and "
                f"llm.ollama_url in config.yaml to a free port.",
            ))
            return done("offline", f"Port {port} is taken by something that is not Ollama.")
        steps.append(_step(
            "server", "Server running", FAIL, f"nothing is listening on {base}",
            "Press Fix (or run check-ollama.bat) to start it." if (local and binary) else
            ("Install Ollama first." if local else f"Start Ollama on {host}."),
            action="start" if (local and binary) else "",
        ))
        return done("offline", "Ollama is not running — captures use the rule-based parser.")
    steps.append(_step("server", "Server running", OK, f"version {version} at {base}"))

    # 4. models pulled
    _tags_cache.pop(cfg.ollama_url, None)
    tags = installed_models(cfg, force=True)
    missing = []
    for role, model in _wanted(cfg):
        pulled = has_model(tags, model)
        if not pulled:
            missing.append(model)
        steps.append(_step(
            f"model:{role}", f"{role.capitalize()} model pulled",
            OK if pulled else (FAIL if role == "chat" else WARN),
            model,
            "" if pulled else f"Press Pull, or run: ollama pull {model}",
            action="" if pulled else "pull",
        ))

    # 5. really answers
    if deep and has_model(tags, cfg.model):
        t0 = time.monotonic()
        try:
            r = httpx.post(f"{base}/api/generate", json={
                "model": cfg.model, "prompt": "Reply with the single word OK.",
                "stream": False, "keep_alive": cfg.keep_alive,
                "options": {"num_predict": 5, "temperature": 0},
            }, timeout=GENERATE_TIMEOUT_S)
            secs = time.monotonic() - t0
            if r.status_code == 200:
                reply = str(r.json().get("response", "")).strip()[:40]
                steps.append(_step("generate", "Chat model answers", OK if secs < 30 else WARN,
                                   f"{secs:.1f}s · {reply!r}",
                                   "" if secs < 30 else "Slow: the model may not fit in GPU memory."))
            else:
                err = _error_text(r)
                steps.append(_step("generate", "Chat model answers", FAIL, err,
                                   _hint_for(err, cfg.model)))
        except Exception as exc:
            steps.append(_step("generate", "Chat model answers", FAIL,
                               f"{type(exc).__name__}: {exc}", _hint_for(str(exc), cfg.model)))
    if deep and cfg.embed_model and has_model(tags, cfg.embed_model):
        try:
            r = httpx.post(f"{base}/api/embeddings", json={
                "model": cfg.embed_model, "prompt": "test", "keep_alive": "1m",
            }, timeout=GENERATE_TIMEOUT_S)
            ok = r.status_code == 200 and bool(r.json().get("embedding"))
            steps.append(_step("embed", "Search model answers", OK if ok else WARN,
                               f"{len(r.json().get('embedding') or [])} dims" if ok else _error_text(r)))
        except Exception as exc:
            steps.append(_step("embed", "Search model answers", WARN, f"{type(exc).__name__}: {exc}"))

    if any(s["status"] == FAIL for s in steps):
        broken = next(s for s in steps if s["status"] == FAIL)
        return done("degraded", f"Ollama is running, but: {broken['label'].lower()} failed.")
    if missing:
        return done("degraded", "Ollama is running; optional models are missing: " + ", ".join(missing))
    return done("ready", f"Ollama {version} is working.")


def _error_text(r) -> str:
    try:
        return str(r.json().get("error") or r.text)[:300]
    except Exception:
        return f"HTTP {r.status_code}"


def _hint_for(err: str, model: str) -> str:
    e = err.lower()
    if "memory" in e or "cuda" in e or "vram" in e:
        return (f"{model} does not fit in memory. Close other GPU apps, or set "
                "llm.model to a smaller model in config.yaml.")
    if "not found" in e:
        return f"Run: ollama pull {model}"
    if "timeout" in e or "timed out" in e:
        return "The model took too long to load. Try again; the first load is the slowest."
    return "See %LOCALAPPDATA%\\Ollama\\server.log for the server's own error."


# --------------------------------------------------------------------------
# the fix
# --------------------------------------------------------------------------


def start_server(cfg: LLMConfig) -> Dict[str, Any]:
    """Start Ollama if it is local, installed and not already answering."""
    host, port, base = _endpoint(cfg)
    if _version(base):
        return {"started": False, "reason": "already running"}
    if not _is_local(host):
        return {"started": False, "reason": f"server is on {host}; start it there"}
    if _port_open(host, port):
        return {"started": False, "reason": f"port {port} is used by another program"}
    tray, binary = find_tray_app(), find_binary()
    if not (tray or binary):
        return {"started": False, "reason": "Ollama is not installed (https://ollama.com/download)"}

    cmd = [str(tray)] if tray else [str(binary), "serve"]
    kwargs: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                              "stderr": subprocess.DEVNULL, "close_fds": True}
    if sys.platform == "win32":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP
                                   | subprocess.CREATE_NO_WINDOW)
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(cmd, **kwargs)
    except OSError as exc:
        return {"started": False, "reason": f"could not launch {cmd[0]}: {exc}"}

    deadline = time.monotonic() + START_WAIT_S
    while time.monotonic() < deadline:
        if _version(base):
            return {"started": True, "via": cmd[0]}
        time.sleep(0.5)
    return {"started": False, "reason": f"launched {cmd[0]} but it did not answer within "
                                        f"{int(START_WAIT_S)}s — see server.log"}


def pull_missing(cfg: LLMConfig) -> Dict[str, Any]:
    _, _, base = _endpoint(cfg)
    tags = installed_models(cfg, force=True)
    pulled, failed = [], {}
    for _, model in _wanted(cfg):
        if has_model(tags, model):
            continue
        try:
            r = httpx.post(f"{base}/api/pull", json={"model": model, "stream": False},
                           timeout=PULL_TIMEOUT_S)
            if r.status_code == 200:
                pulled.append(model)
            else:
                failed[model] = _error_text(r)
        except Exception as exc:
            failed[model] = f"{type(exc).__name__}: {exc}"
    _tags_cache.pop(cfg.ollama_url, None)
    return {"pulled": pulled, "failed": failed}


def fix(cfg: LLMConfig, pull: bool = False) -> Dict[str, Any]:
    started = start_server(cfg)
    pulled = pull_missing(cfg) if pull and _version(_endpoint(cfg)[2]) else None
    return {"start": started, "pull": pulled, "check": check(cfg, deep=True)}
