"""Ollama backend — the default, and the reason this system is private by
construction: every token stays on the machine.

Uses the /api/chat endpoint with format="json" for structured extraction, and
/api/embeddings for the RAG index (used by the retrieval layer later).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from ..config import LLMConfig
from . import _json


class OllamaProvider:
    name = "ollama"
    is_llm = True

    def __init__(
        self,
        cfg: LLMConfig,
        model: Optional[str] = None,
        keep_alive: Optional[str] = None,
    ):
        self.cfg = cfg
        self.base = cfg.ollama_url.rstrip("/")
        #: Which tag this instance talks to. Set per call site by the role, so
        #: one process can drive a small model for captures and a larger one
        #: for the tutor without two configurations.
        self.model = model or cfg.model
        #: How long Ollama holds it in VRAM afterwards. A 12GB card cannot keep
        #: an 8B and a 14B resident at once, so the lanes take turns and this
        #: is what decides who waits.
        self.keep_alive = keep_alive or cfg.keep_alive

    # -- health -------------------------------------------------------------

    def available(self) -> bool:
        try:
            r = httpx.get(f"{self.base}/api/tags", timeout=2.0)
            return r.status_code == 200
        except Exception:
            return False

    def models(self) -> List[str]:
        try:
            r = httpx.get(f"{self.base}/api/tags", timeout=5.0)
            r.raise_for_status()
            return [m["name"] for m in r.json().get("models", [])]
        except Exception:
            return []

    # -- completion ---------------------------------------------------------

    def _chat(self, prompt: str, system: Optional[str], json_mode: bool) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {"temperature": self.cfg.temperature},
        }
        if json_mode:
            payload["format"] = "json"
        r = httpx.post(f"{self.base}/api/chat", json=payload, timeout=self.cfg.timeout_s)
        r.raise_for_status()
        return r.json().get("message", {}).get("content", "")

    def complete_text(self, prompt: str, system: Optional[str] = None) -> str:
        return self._chat(prompt, system, json_mode=False)

    def complete_json(
        self, prompt: str, system: Optional[str] = None, schema_hint: Optional[str] = None
    ) -> Dict[str, Any]:
        if schema_hint:
            prompt = f"{prompt}\n\nReturn JSON matching exactly this shape:\n{schema_hint}"
        return _json.extract(self._chat(prompt, system, json_mode=True))

    # -- embeddings (used by the RAG index) ---------------------------------

    def embed(self, texts: List[str]) -> List[List[float]]:
        """Always the embedding model, never the chat one.

        `keep_alive` is short here on purpose: `nomic-embed-text` is small but
        an index rebuild calls it hundreds of times in a row, and holding it
        for half an hour afterwards would evict whichever chat model you are
        about to use.
        """
        out: List[List[float]] = []
        for text in texts:
            r = httpx.post(
                f"{self.base}/api/embeddings",
                json={
                    "model": self.cfg.embed_model,
                    "prompt": text,
                    "keep_alive": "2m",
                },
                timeout=self.cfg.timeout_s,
            )
            r.raise_for_status()
            out.append(r.json()["embedding"])
        return out
