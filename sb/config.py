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

    # Reference-class forecasting (sb/forecasting.py). Scale a new estimate by
    # how long lj's own steps have actually taken. Off means estimates are
    # taken at face value, which is the planning fallacy with extra steps.
    apply_personal_multiplier: bool = True

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
    google_tasklist: str = "Second Brain"
    #: Roadmap 1.3. Read completions back from Google Tasks before pushing.
    #: The vault wins on content; Google wins on completion, because ticking
    #: is the only edit lj can make on a phone. Off makes sync one-way again.
    read_back_completions: bool = True  # created on first sync if missing
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
    #: Use weights fitted from `_decks/_reviews.jsonl` when this list is empty.
    #: A number typed here always wins — it is a decision; a fit is a
    #: suggestion. See sb/fit.py, and the ~1,000-review trigger it enforces.
    use_fitted_weights: bool = True

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

    # Card quality (sb/quality.py) — Woźniak's minimum information principle.
    # A drafted answer longer than this, or one that holds a list, is rewritten
    # as cloze cards over the note's own sentence, or dropped if nothing in the
    # note can cite it. Raise it to be filtered less; set
    # `enforce_card_quality: false` to keep every card the model returns.
    max_answer_words: int = 15
    enforce_card_quality: bool = True

    # Metacognitive calibration (sb/calibration.py). How far back the
    # "you were sure and wrong" priority queue looks. A miss from four months
    # ago is a card that has since been relearned, not a belief still held.
    overconfidence_window_days: int = 45

    # Worked-example fading (Sweller). A card with a `Worked.` block shows it
    # in full on the first attempt, as an opening fragment for the next few,
    # and not at all once the deck is this far matured — the expertise-reversal
    # effect, where the same scaffold that helps a novice hinders an expert.
    worked_example_fade_reps: int = 3
    expertise_reversal_at: float = 0.6

    # Graduation (blueprint §4). A card counts as learned when it has survived
    # `review.graduation_min_reps` spaced attempts *and* the model predicts it
    # will still be there this many days from now.
    mature_stability_days: float = 21.0
    min_cards_to_graduate: int = 6

    # A daily study block on the calendar, like any other recurring commitment.
    calendar_event: bool = True
    study_time: str = "19:30"
    study_minutes: int = 20


class IntakeConfig(BaseModel):
    """The Drop folder: notes lj already wrote, filed automatically.

    `auto_floor` is the only dial here that changes behaviour rather than
    timing. Raise it and more notes wait in the Inbox for a click; lower it
    and more are filed on the system's own judgement. It is not 0.5 because a
    bare majority is not a decision worth making on someone else's behalf —
    see the confidence note in sb/intake.py.
    """

    folder: str = "Drop"
    auto_floor: float = 0.6
    #: Ask the model only about the notes the rules could not call. Off does
    #: not disable intake — an undecided note simply goes to the Inbox
    #: without a second opinion first.
    use_model: bool = True
    #: A file Word is still writing is not a file to read. Ignore anything
    #: touched more recently than this.
    settle_seconds: float = 5.0
    #: Watch the folder while the app is open, so dropping a file *is* the
    #: whole interaction. The dashboard button does the same on demand.
    watch: bool = True
    poll_seconds: float = 20.0
    max_per_run: int = 25


class CaptureConfig(BaseModel):
    """What happens to a capture that did not say where it was going.

    The three dashboard buttons always say. The hotkey box, the Obsidian
    plugin and anything else that just hands over a line of text do not, and
    those used to land in `00-Inbox` — which is a queue, and a queue nobody
    empties is a folder with a worse name. Most of what gets captured in a
    hurry is reference material, so `resource` is the honest default: the
    note is filed, reviewable, searchable and on the review cycle from the
    moment it is written, and moving one that guessed wrong is a drag in
    Obsidian.

    This is *not* the Drop folder's floor. A dropped file the classifier is
    unsure about still goes to the Inbox with its suggestion attached, because
    there the system has actually formed a doubt and saying so is the whole
    design (`intake.auto_floor`). Silence is not doubt.
    """

    default_bucket: str = "resource"  # resource | inbox | area | project

    #: Which template shapes a plain capture. `Atomic Note` on purpose: every
    #: captured note then arrives carrying the `## In my own words` heading,
    #: which is the one slot `sb/collected.py` reads to tell a note you used
    #: from a note you merely kept. A heading that is visibly empty is the
    #: cheapest prompt there is.
    #:
    #: Any name in `_templates/` works — the template decides the headings, so
    #: changing this, or editing that file, changes what the system writes
    #: without touching code. See sb/render.py.
    default_template: str = "Atomic Note"


class ReviewConfig(BaseModel):
    resource_cycle_days: int = 90
    habit_checkin_weekday: int = 6  # Sunday
    graduation_mastery_threshold: float = 0.85
    graduation_min_reps: int = 4


class ConnectConfig(BaseModel):
    """Linking notes to notes (sb/connect.py).

    `link_on_write` is roadmap Tier 1.1. The free tiers — a note's own
    parent/child facts and literal `[[title]]`-able mentions of other notes —
    need nothing but the vault listing the caller already holds, so running
    them while the note is being written costs one regex build, no extra file
    read and no model call. What deliberately does *not* run at write time is
    the embedding index and the model tie-break: the note is not in the index
    yet, and nothing typed into a capture box should wait on Ollama.

    `max_links_on_write` is under `connect.MAX_LINKS` on purpose, so the
    periodic pass still has room to add what the index finds.
    """

    link_on_write: bool = True
    max_links_on_write: int = 4


class Config(BaseModel):
    vault: Path = Path.home() / "Obsidian" / "SecondBrain"
    #: Set by `load()` when an explicit `vault:` did not exist on this
    #: machine and the config file's own directory was used instead — see
    #: `load()`. Empty when no fallback happened. Not itself config; carried
    #: on the instance so `doctor` can say what it did instead of silently
    #: reporting on an empty vault.
    vault_note: str = ""
    host: str = "127.0.0.1"
    port: int = 8787
    llm: LLMConfig = Field(default_factory=LLMConfig)
    planner: PlannerConfig = Field(default_factory=PlannerConfig)
    calendar: CalendarConfig = Field(default_factory=CalendarConfig)
    areas: AreasConfig = Field(default_factory=AreasConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    study: StudyConfig = Field(default_factory=StudyConfig)
    intake: IntakeConfig = Field(default_factory=IntakeConfig)
    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    connect: ConnectConfig = Field(default_factory=ConnectConfig)

    @property
    def system_dir(self) -> Path:
        return self.vault / "_system"

    @property
    def drop_dir(self) -> Path:
        """Where lj puts notes they already wrote. Not disposable and not
        machine state, so it sits beside the PARA folders rather than under
        `_system/` — it is a place a person opens."""
        return self.vault / (self.intake.folder or "Drop")

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


def _looks_like_vault(path: Path) -> bool:
    """The PARA skeleton, present even if lj never set `vault:` — the common
    case, since the app usually lives inside the vault it manages."""
    markers = ("00-Inbox", "10-Areas", "20-Projects", "30-Resources")
    return path.is_dir() and all((path / m).is_dir() for m in markers)


def load(path: Optional[Path] = None) -> Config:
    path = Path(path) if path else REPO_ROOT / "config.yaml"
    # Resolve before taking `.parent`: a relative --config would otherwise
    # anchor the fallback vault to the current working directory instead of
    # to where config.yaml actually is.
    config_dir = path.resolve().parent
    data: Dict[str, Any] = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data = _apply_env(data)

    raw_vault = str(data.get("vault") or "").strip()
    if not raw_vault:
        # No `vault:` anywhere (config.yaml omits it, or the key is blank).
        # The portable default is "wherever config.yaml lives" — right on
        # every machine, Windows or this Linux bridge, with no per-machine
        # setting to keep in sync. Pydantic's own class default
        # (~/Obsidian/SecondBrain) only exists so a bare `Config()` still
        # works for callers who build one directly, e.g. tests.
        data["vault"] = str(config_dir)

    cfg = Config(**data)
    cfg.vault = Path(os.path.expandvars(str(cfg.vault))).expanduser()

    if raw_vault and not cfg.vault.exists() and _looks_like_vault(config_dir):
        # An explicit path was given and it does not exist here — a Windows
        # drive-letter path read from a Linux bridge, a laptop this vault
        # never lived on, whatever. The folder config.yaml sits in looks
        # like the vault, so use it instead of quietly reporting on an
        # empty directory (see `doctor`'s vault_ok check in sb/engine.py).
        cfg.vault_note = (
            f"vault \"{raw_vault}\" not found on this machine — using "
            f"{config_dir} (where config.yaml lives), which looks like the vault"
        )
        cfg.vault = config_dir

    return cfg
