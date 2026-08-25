"""Configuration.

Loaded from config.yaml next to the repo root, overridable by environment
variables (SB_VAULT, SB_LLM_PROVIDER, ...). Everything has a working default
so the system runs with an empty config file.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parent.parent


#: Jobs the system asks a model to do. Two lanes, split by what actually
#: matters for each: latency, or judgement.
#:
#:   parse     capture -> project metadata. Runs while you wait, on every
#:             capture, and the rules already did the hard part. Fast lane.
#:   generate  note -> flashcards. Runs once per note and the result is
#:             permanent — a bad card gets drilled into you on an optimal
#:             schedule. Worth a better model.
#:   grade     marking a typed recall answer. A judgement call about meaning.
#:   explain   "but why?" on a card. Teaching, not extraction.
#:   ask       answering a question from the whole vault, with citations.
ROLES = ("parse", "generate", "grade", "explain", "ask")


class LLMConfig(BaseModel):
    # "ollama" (default, fully local) | "cloud" (opt-in) | "heuristic" (no LLM)
    provider: str = "ollama"
    # Ollama
    ollama_url: str = "http://localhost:11434"
    #: The fast lane. Everything not listed in `study_roles`.
    model: str = "llama3.1:8b"
    embed_model: str = "nomic-embed-text"

    #: The good lane. Blank means "use `model` for everything", which is the
    #: right default for a machine that cannot spare the VRAM.
    study_model: str = ""
    #: Which jobs get the good model. Anything not here uses `model`.
    study_roles: List[str] = Field(
        default_factory=lambda: ["generate", "grade", "explain", "ask"]
    )
    #: How long Ollama holds each model in VRAM after a call.
    #:
    #: These differ on purpose. A 12GB card cannot hold an 8B and a 14B at
    #: once, so the two lanes take turns — and the turn-taking should favour
    #: whichever one you are in the middle of using. The fast lane lets go
    #: quickly because a capture is a single call; the study lane holds on
    #: through a review session, where reloading between every card would cost
    #: more than the answers.
    keep_alive: str = "5m"
    study_keep_alive: str = "30m"
    # Cloud escape hatch, off unless provider == "cloud". Key read from env.
    cloud_provider: str = "anthropic"  # anthropic | openai
    cloud_model: str = "claude-sonnet-4-5"
    cloud_api_key_env: str = "ANTHROPIC_API_KEY"
    # Behaviour
    timeout_s: float = 120.0
    temperature: float = 0.1
    # If the configured provider is unreachable, fall back to the offline
    # rule-based parser rather than failing the capture. Captures must never
    # be lost because a model is down.
    fallback_to_heuristic: bool = True


    def model_for(self, role: str = "") -> str:
        """Which model does this job. Unknown roles get the fast lane."""
        if role and self.study_model and role in self.study_roles:
            return self.study_model
        return self.model

    def keep_alive_for(self, role: str = "") -> str:
        if role and self.study_model and role in self.study_roles:
            return self.study_keep_alive
        return self.keep_alive


class PlannerConfig(BaseModel):
    """How Project steps become calendar blocks."""

    work_start: str = "09:00"
    work_end: str = "18:00"
    workdays: List[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])  # Mon-Fri
    max_minutes_per_day: int = 180
    block_gap_minutes: int = 15
    deadline_buffer_days: int = 1  # finish work this many days before the deadline
    # Treat recurring Area blocks as busy time so project work never lands on
    # top of a habit. Off by default: Areas are elastic by nature, and a habit
    # you can slide by twenty minutes should not push a deadline.
    respect_area_blocks: bool = False

    def start_time(self) -> dt.time:
        return dt.time.fromisoformat(self.work_start)

    def end_time(self) -> dt.time:
        return dt.time.fromisoformat(self.work_end)


class CalendarConfig(BaseModel):
    # "ics" writes a local .ics into the vault (works immediately, no accounts).
    # "google" pushes to Google Calendar via OAuth (see docs/google-calendar.md).
    # "both" does both.
    sink: str = "ics"
    ics_filename: str = "secondbrain.ics"
    google_calendar_id: str = "primary"
    google_credentials_file: str = "credentials.json"
    google_token_file: str = "token.json"
    reminder_minutes: List[int] = Field(default_factory=lambda: [1440, 60])
    timezone: str = ""  # blank = system local

    # Project deadlines are tasks, not events. Where those tasks go:
    #   "auto"   follow `sink` — the sane default, so turning Google on or off
    #            moves events and due dates together
    #   "ics"    VTODO components inside secondbrain.ics
    #   "google" Google Tasks (shows in the Tasks strip of Google Calendar)
    #   "both"   both, "none" to turn due-date tasks off entirely
    task_sink: str = "auto"
    google_tasklist: str = "Second Brain"  # created on first sync if missing
    # Google Tasks has no colour API, so the category emoji in the title is
    # the only colour signal a task gets. Events get a real colour as well.
    emoji_prefix: bool = True
    color_events: bool = True
    # Extend or override sb/taxonomy.py without editing code:
    #   categories:
    #     hw: {keywords: [calc, orgo]}
    #     church: {emoji: "⛪", color: teal, hex: "#009688", google_color_id: "7"}
    categories: Dict[str, Dict[str, Any]] = Field(default_factory=dict)


class AreasConfig(BaseModel):
    """Areas are recurring events, not deadlines (§2)."""

    default_time: str = "18:00"
    default_duration_minutes: int = 30
    # The "option to change": one weekly event where every Area's schedule is
    # up for revision, rather than a per-Area check-in cluttering the week.
    schedule_review: bool = True
    schedule_review_weekday: int = 6  # Sunday
    schedule_review_time: str = "19:00"
    schedule_review_minutes: int = 20


class StudyConfig(BaseModel):
    """The tutor: FSRS tuning, daily volume, and what counts as learned."""

    # FSRS. `desired_retention` is the one dial worth touching: 0.9 means you
    # see a card when the model says you have a 10% chance of having forgotten
    # it. Raising it shortens every interval and multiplies your daily load
    # steeply; 0.85–0.95 is the sane range.
    desired_retention: float = 0.9
    maximum_interval_days: int = 3650
    #: Personal FSRS weights, if you ever fit them from _decks/_reviews.jsonl.
    #: Empty means the published FSRS-5 defaults.
    weights: List[float] = Field(default_factory=list)

    # Daily volume. Caps exist so a backlog is a slope, not a wall.
    new_cards_per_day: int = 10
    max_reviews_per_day: int = 120
    session_size: int = 40
    #: Mix subjects within a session rather than finishing one deck at a time.
    #: Harder in the moment, better retention. See sb/tutor.py.
    interleave: bool = True

    # Generation
    generate_max_cards: int = 20
    generate_per_passage: int = 3

    # Graduation (blueprint §4). A card counts as learned when it has survived
    # `review.graduation_min_reps` spaced attempts *and* the model predicts it
    # will still be there this many days from now.
    mature_stability_days: float = 21.0
    min_cards_to_graduate: int = 6

    # A daily study block on the calendar, like any other recurring commitment.
    calendar_event: bool = True
    study_time: str = "19:30"
    study_minutes: int = 20


class ReviewConfig(BaseModel):
    resource_cycle_days: int = 90
    habit_checkin_weekday: int = 6  # Sunday
    graduation_mastery_threshold: float = 0.85
    graduation_min_reps: int = 4


class Config(BaseModel):
    vault: Path = Path.home() / "Obsidian" / "SecondBrain"
    host: str = "127.0.0.1"
    port: int = 8787
    llm: LLMConfig = Field(default_factory=LLMConfig)
    planner: PlannerConfig = Field(default_factory=PlannerConfig)
    calendar: CalendarConfig = Field(default_factory=CalendarConfig)
    areas: AreasConfig = Field(default_factory=AreasConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    study: StudyConfig = Field(default_factory=StudyConfig)

    @property
    def system_dir(self) -> Path:
        return self.vault / "_system"

    @property
    def deck_dir(self) -> Path:
        """Decks are durable, unlike everything in `_system/`: a calendar can
        be rebuilt from the notes, a year of review history cannot."""
        return self.vault / "_decks"

    @property
    def ics_path(self) -> Path:
        return self.system_dir / "calendar" / self.calendar.ics_filename

    def resolved_task_sink(self) -> str:
        """Where due-date tasks go, with "auto" resolved against the event
        sink. Keeping them coupled by default means an .ics-only setup never
        tries to reach Google Tasks and log an auth error every sync."""
        want = (self.calendar.task_sink or "auto").lower()
        if want != "auto":
            return want
        sink = (self.calendar.sink or "ics").lower()
        return {"ics": "ics", "google": "google", "both": "both"}.get(sink, "ics")

    def tzname(self) -> str:
        """A named zone for Google's recurring events, which reject a floating
        start. Falls back to whatever the machine calls its local zone."""
        if self.calendar.timezone:
            return self.calendar.timezone
        local = dt.datetime.now().astimezone().tzinfo
        return getattr(local, "key", None) or str(local) or "UTC"


def _apply_env(data: Dict[str, Any]) -> Dict[str, Any]:
    env_map = {
        "SB_VAULT": ("vault",),
        "SB_HOST": ("host",),
        "SB_PORT": ("port",),
        "SB_LLM_PROVIDER": ("llm", "provider"),
        "SB_LLM_MODEL": ("llm", "model"),
        "SB_OLLAMA_URL": ("llm", "ollama_url"),
        "SB_CALENDAR_SINK": ("calendar", "sink"),
    }
    for env, path in env_map.items():
        val: Optional[str] = os.environ.get(env)
        if val is None:
            continue
        cursor = data
        for key in path[:-1]:
            cursor = cursor.setdefault(key, {})
        cursor[path[-1]] = val
    return data


def load(path: Optional[Path] = None) -> Config:
    path = Path(path) if path else REPO_ROOT / "config.yaml"
    data: Dict[str, Any] = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data = _apply_env(data)
    cfg = Config(**data)
    cfg.vault = Path(os.path.expandvars(str(cfg.vault))).expanduser()
    return cfg
