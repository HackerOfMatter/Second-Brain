"""Pluggable completion providers, and which model does which job.

One interface, three backends. The default is Ollama, so the whole system runs
with nothing leaving the machine; `cloud` is an opt-in escape hatch for when
you want stronger reasoning on a specific job; `heuristic` is a no-LLM
fallback so a capture is never lost because a model is down.

Adding a backend means implementing `Provider.complete_json` / `complete_text`
and registering it in `get_provider`. Nothing above this layer knows which
backend is in use.

## Two lanes

Every call names a **role** — `parse`, `generate`, `grade`, `explain`, `ask` —
and the role decides which model answers it. The split is not about how
important the job feels; it is about what the job actually needs:

* **Fast lane** (`llm.model`). `parse` runs on every capture while you wait,
  and the rules in `sb/extract.py` have already settled the parts that matter
  most. A small model finishing in two seconds beats a large one finishing in
  fifteen.
* **Good lane** (`llm.study_model`). Card generation writes something
  permanent that spaced repetition will drill into you; marking a typed answer
  is a judgement about meaning; explaining and answering are teaching. Quality
  is worth the wait, and none of these run while you are mid-sentence.

`study_model` blank means one model for everything, which is the right setting
on a machine that cannot spare the VRAM.

## When the good model is not there

Naming a model you have not pulled must not break studying. Before using the
study lane the installed tag list is checked (cached briefly, one cheap call to
`/api/tags`), and an absent model falls back to the fast one with the reason
recorded where `doctor` can print it. A typo in config.yaml costs you quality,
never a working system.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Protocol, Tuple

from ..config import LLMConfig


class Provider(Protocol):
    name: str
    is_llm: bool  # False for the rule-based fallback; callers branch on this
    model: str  # the tag actually in use, for health reporting

    def available(self) -> bool:
        """Cheap reachability check; must not raise."""

    def complete_text(self, prompt: str, system: Optional[str] = None) -> str: ...

    def complete_json(
        self, prompt: str, system: Optional[str] = None, schema_hint: Optional[str] = None
    ) -> Dict[str, Any]: ...


# --------------------------------------------------------------------------
# what is actually installed
# --------------------------------------------------------------------------

#: `/api/tags` is cheap but not free, and the answer changes only when someone
#: runs `ollama pull`. A few seconds of staleness is invisible; asking on every
#: one of twenty card-generation calls is not.
_TAGS_TTL = 20.0
_tags_cache: Dict[str, Tuple[float, List[str]]] = {}


def installed_models(cfg: LLMConfig, force: bool = False) -> List[str]:
    """Tags Ollama has locally. Empty when it is unreachable — which is also
    the answer that makes every "is it pulled?" check fail safe."""
    key = cfg.ollama_url
    now = time.monotonic()
    if not force:
        hit = _tags_cache.get(key)
        if hit and now - hit[0] < _TAGS_TTL:
            return hit[1]
    tags: List[str] = []
    try:
        import httpx

        r = httpx.get(f"{cfg.ollama_url.rstrip('/')}/api/tags", timeout=3.0)
        if r.status_code == 200:
            tags = [m.get("name", "") for m in r.json().get("models", [])]
    except Exception:
        tags = []
    _tags_cache[key] = (now, tags)
    return tags


def has_model(installed: List[str], want: str) -> bool:
    """Tag matching that copes with the implicit `:latest`.

    `ollama pull phi4` reports back as `phi4:latest`, so a config that says
    `phi4` has to match it — otherwise the fallback fires on a model that is
    sitting right there.
    """
    if not want:
        return False
    want = want.strip()
    candidates = {want, want if ":" in want else f"{want}:latest"}
    for tag in installed:
        tag = (tag or "").strip()
        if tag in candidates or (":" not in want and tag.split(":", 1)[0] == want):
            return True
    return False


def resolve_model(cfg: LLMConfig, role: str = "") -> Tuple[str, str]:
    """(model to use, why it is not the one you asked for).

    The second value is empty in the normal case and carries an explanation
    when the study lane fell back — which is exactly what `doctor` should print
    instead of leaving you wondering why answers got worse.
    """
    wanted = cfg.model_for(role)
    if wanted == cfg.model or not wanted:
        return cfg.model, ""
    installed = installed_models(cfg)
    if not installed:
        # Ollama is unreachable; the caller's own availability check will
        # handle that. Do not blame a missing pull for a stopped server.
        return wanted, ""
    if has_model(installed, wanted):
        return wanted, ""
    return cfg.model, (
        f"{wanted!r} is not pulled — using {cfg.model!r}. Run: ollama pull {wanted}"
    )


# --------------------------------------------------------------------------
# construction
# --------------------------------------------------------------------------


def get_provider(cfg: LLMConfig, role: str = "") -> Provider:
    """The configured provider, bound to the model this role should use."""
    name = (cfg.provider or "ollama").lower()
    if name == "ollama":
        from .ollama import OllamaProvider

        model, _ = resolve_model(cfg, role)
        return OllamaProvider(cfg, model=model, keep_alive=cfg.keep_alive_for(role))
    if name == "cloud":
        from .cloud import CloudProvider

        return CloudProvider(cfg)
    if name in ("heuristic", "none", "off"):
        from .heuristic import HeuristicProvider

        return HeuristicProvider(cfg)
    raise ValueError(f"unknown llm provider {cfg.provider!r}")


def resolve_provider(cfg: LLMConfig, role: str = "") -> Provider:
    """The provider actually used for a call: the configured one if reachable,
    otherwise the offline fallback when that is allowed."""
    provider = get_provider(cfg, role)
    if provider.available():
        return provider
    if cfg.fallback_to_heuristic:
        from .heuristic import HeuristicProvider

        return HeuristicProvider(cfg)
    raise RuntimeError(
        f"LLM provider {provider.name!r} is unreachable and "
        "llm.fallback_to_heuristic is disabled"
    )


def lane_report(cfg: LLMConfig) -> Dict[str, Any]:
    """What `doctor` and `/api/health` print: which model does what, and
    whether each one is actually on disk."""
    installed = installed_models(cfg)
    fast = {
        "model": cfg.model,
        "pulled": has_model(installed, cfg.model) if installed else None,
    }
    out: Dict[str, Any] = {"fast": fast, "study": None, "roles": {}}
    if cfg.study_model:
        model, why = resolve_model(cfg, "generate")
        out["study"] = {
            "model": cfg.study_model,
            "pulled": has_model(installed, cfg.study_model) if installed else None,
            "in_use": model,
            "warning": why,
        }
    from ..config import ROLES

    for role in ROLES:
        out["roles"][role] = resolve_model(cfg, role)[0]
    return out
