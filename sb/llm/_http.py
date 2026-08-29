"""One HTTP client per host, shared for the life of the process.

Every call site here used to go through the module-level helpers — `httpx.get`,
`httpx.post` — and each of those builds a throwaway `httpx.Client`, which loads
the system CA bundle before it can send anything. On this machine that is ~31 ms
of `ssl.load_verify_locations` per request, paid even for a plain-HTTP call to
Ollama on localhost, where no trust store is involved at all.

It is worst exactly where it hurts most. `OllamaProvider.embed` posts once per
passage, so a full index rebuild over a few thousand chunks spent minutes doing
nothing but constructing SSL contexts and opening fresh TCP connections. A
shared client pays that once and then keeps the connection alive, which also
removes a TCP (and, for the cloud vendors, a TLS) handshake per request.

`httpx.Client` is safe to share across threads, which the API server needs: the
engine runs in a threadpool.
"""

from __future__ import annotations

import atexit
import threading
from typing import Dict

import httpx

_CLIENTS: Dict[str, httpx.Client] = {}
_LOCK = threading.Lock()


def client(base_url: str = "") -> httpx.Client:
    """The shared client for `base_url`, created on first use."""
    key = base_url.rstrip("/")
    existing = _CLIENTS.get(key)
    if existing is not None:
        return existing
    with _LOCK:
        # Re-check inside the lock: two threads can miss simultaneously, and
        # the loser would otherwise leak a client and its connection pool.
        existing = _CLIENTS.get(key)
        if existing is None:
            existing = _CLIENTS[key] = httpx.Client(
                base_url=key,
                # A local Ollama serialises requests anyway; the pool exists so
                # the dashboard's parallel health checks do not fight.
                limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
            )
        return existing


@atexit.register
def _close_all() -> None:
    for c in list(_CLIENTS.values()):
        try:
            c.close()
        except Exception:
            pass
    _CLIENTS.clear()
