"""No-LLM fallback provider.

Used when the configured backend is unreachable (Ollama not running, no API
key) and `llm.fallback_to_heuristic` is on. It cannot reason, so it advertises
`is_llm = False` and callers take their rule-based path instead of prompting
it. The point is that a capture typed at 2am with Ollama closed still lands in
the vault as a structured Project.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..config import LLMConfig


class HeuristicProvider:
    name = "heuristic"
    is_llm = False
    model = "rule-based"

    def __init__(self, cfg: Optional[LLMConfig] = None):
        self.cfg = cfg

    def available(self) -> bool:
        return True

    def complete_text(self, prompt: str, system: Optional[str] = None) -> str:
        raise RuntimeError(
            "No language model is available. Start Ollama (`ollama serve`) or set "
            "llm.provider: cloud in config.yaml."
        )

    def complete_json(
        self, prompt: str, system: Optional[str] = None, schema_hint: Optional[str] = None
    ) -> Dict[str, Any]:
        raise RuntimeError("HeuristicProvider cannot answer free-form prompts")
