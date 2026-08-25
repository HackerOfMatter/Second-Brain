"""Cloud backend — off by default, present so the switch is a config line
rather than a refactor.

Nothing calls this unless `llm.provider: cloud` is set in config.yaml and the
API key env var is populated. Raw HTTP via httpx keeps the vendor SDKs out of
the dependency list.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import httpx

from ..config import LLMConfig
from . import _json


class CloudProvider:
    name = "cloud"
    is_llm = True

    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.vendor = cfg.cloud_provider.lower()
        self.model = cfg.cloud_model
        self.api_key = os.environ.get(cfg.cloud_api_key_env, "")

    def available(self) -> bool:
        return bool(self.api_key)

    def complete_text(self, prompt: str, system: Optional[str] = None) -> str:
        if not self.api_key:
            raise RuntimeError(f"{self.cfg.cloud_api_key_env} is not set")
        if self.vendor == "anthropic":
            return self._anthropic(prompt, system)
        if self.vendor == "openai":
            return self._openai(prompt, system)
        raise ValueError(f"unknown cloud provider {self.vendor!r}")

    def complete_json(
        self, prompt: str, system: Optional[str] = None, schema_hint: Optional[str] = None
    ) -> Dict[str, Any]:
        if schema_hint:
            prompt = f"{prompt}\n\nReturn ONLY JSON matching this shape:\n{schema_hint}"
        return _json.extract(self.complete_text(prompt, system))

    # -- vendors ------------------------------------------------------------

    def _anthropic(self, prompt: str, system: Optional[str]) -> str:
        payload: Dict[str, Any] = {
            "model": self.cfg.cloud_model,
            "max_tokens": 4096,
            "temperature": self.cfg.temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
            timeout=self.cfg.timeout_s,
        )
        r.raise_for_status()
        blocks = r.json().get("content", [])
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")

    def _openai(self, prompt: str, system: Optional[str]) -> str:
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        r = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.cfg.cloud_model,
                "messages": messages,
                "temperature": self.cfg.temperature,
            },
            timeout=self.cfg.timeout_s,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
