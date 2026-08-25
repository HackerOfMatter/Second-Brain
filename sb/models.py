"""The vault schema.

This module is the answer to blueprint §9's first open question: the exact
frontmatter contract for each PARA bucket. Everything else in the system
(dashboard, RAG index, calendar, spaced repetition) reads and writes through
these models, so the schema lives in exactly one place.

Design rules:
  * Every note carries the *common* block: id, title, bucket, created,
    updated, tags, source.
  * Bucket-specific state lives under a single namespaced key -- `project:`,
    `habit:`, `review:`, `srs:` -- so a note can carry more than one without
    key collisions, and so a human reading the frontmatter in Obsidian can
    tell at a glance which subsystem owns which field.
  * Nothing is ever deleted. Bucket changes are file moves plus a `history`
    entry, which is what makes Resource<->Archive reversible (§2).
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# --------------------------------------------------------------------------
# enums
# --------------------------------------------------------------------------


class Bucket(str, Enum):
    INBOX = "inbox"
    AREA = "area"
    PROJECT = "project"
    RESOURCE = "resource"
    ARCHIVE = "archive"


class ProjectStatus(str, Enum):
    ACTIVE = "active"
    BLOCKED = "blocked"
    GRADUATING = "graduating"  # SR engine says mastered; awaiting confirmation (§4)
    DONE = "done"


class Cadence(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class MaterialKind(str, Enum):
    """What sort of thing a Project needs before it can start.

    Hardware and Software began as hand-written body headings on the Project
    template, which meant the engine could not see them, could not check them
    off, and regenerated them away on every re-render. They are one list now,
    separated by this.
    """

    MATERIAL = "material"   # reference material, docs, links
    HARDWARE = "hardware"   # physical equipment
    SOFTWARE = "software"   # apps, accounts, licences


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def now() -> dt.datetime:
    """Timezone-aware local now, second resolution (keeps frontmatter tidy)."""
    return dt.datetime.now().astimezone().replace(microsecond=0)


def slugify(text: str, max_len: int = 60) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:max_len].strip("-") or "untitled"


def new_id(title: str) -> str:
    """Stable, sortable, human-readable id: 20260822T093012-learn-rust-generics."""
    return f"{now().strftime('%Y%m%dT%H%M%S')}-{slugify(title, 40)}"


# --------------------------------------------------------------------------
# bucket-specific blocks
# --------------------------------------------------------------------------


class Step(BaseModel):
    """One unit of Project work. The work-flow engine sequences these."""

    model_config = ConfigDict(extra="ignore")

    id: str
    text: str
    minutes: int = 30
    done: bool = False
    done_at: Optional[dt.datetime] = None
    scheduled: Optional[dt.datetime] = None  # set by the planner, pushed to calendar


class Material(BaseModel):
    """One thing a Project needs. Checkable, like a Step.

    `done` is round-tripped through the rendered body, so ticking the box in
    Obsidian is a real state change rather than a note the app will overwrite.
    """

    model_config = ConfigDict(extra="ignore")

    text: str
    kind: MaterialKind = MaterialKind.MATERIAL
    done: bool = False


class ProjectMeta(BaseModel):
    """Blueprint §3: the six fields a Project capture unpacks into.

    `time` is stored as `estimate_minutes` (integers survive round-tripping;
    "2h" does not). `learning` marks the Projects eligible for the §4
    graduate-to-Resource lifecycle.
    """

    model_config = ConfigDict(extra="ignore")

    status: ProjectStatus = ProjectStatus.ACTIVE
    deadline: Optional[dt.date] = None
    #: How much to trust `deadline`, and why. A date the text *named* arrives
    #: confirmed; one the parser had to interpret ("end of the week") does not,
    #: and waits in the dashboard's approval queue quoting the words it came
    #: from. An unconfirmed date still reaches the calendar — a forgotten
    #: confirmation should not mean no reminder at all.
    deadline_confirmed: bool = False
    deadline_source: str = ""  # explicit | exact | ambiguous | llm | manual
    deadline_phrase: str = ""  # the words it was read from, for the prompt
    estimate_minutes: int = 60
    level: int = Field(default=3, ge=1, le=5)
    skills: List[str] = Field(default_factory=list)
    materials: List[Material] = Field(default_factory=list)
    steps: List[Step] = Field(default_factory=list)
    learning: bool = False
    ideal_end: Optional[str] = None  # the "defined ideal end" (§2)

    @field_validator("materials", mode="before")
    @classmethod
    def _coerce_materials(cls, value: Any) -> Any:
        """Accept the old shape.

        Every material written before this field grew a `kind` is a bare
        string on disk. Reading one has to keep working — the vault is the
        source of truth and nobody is going to migrate it by hand.
        """
        if not isinstance(value, list):
            return value
        out: List[Any] = []
        for item in value:
            if isinstance(item, str):
                out.append({"text": item})
            else:
                out.append(item)
        return out

    def materials_of(self, kind: MaterialKind) -> List[Material]:
        return [m for m in self.materials if m.kind == kind]

    @property
    def remaining_steps(self) -> List[Step]:
        return [s for s in self.steps if not s.done]

    @property
    def progress(self) -> float:
        if not self.steps:
            return 1.0 if self.status == ProjectStatus.DONE else 0.0
        return sum(1 for s in self.steps if s.done) / len(self.steps)


class SrsState(BaseModel):
    """Spaced-repetition state (blueprint §4).

    SM-2 shaped, with an explicit `mastery` scalar so the graduation prompt has
    a single number to threshold on. Signal comes from quiz answers and
    time-since-last-review -- both of which live here.
    """

    model_config = ConfigDict(extra="ignore")

    reps: int = 0
    lapses: int = 0
    ease: float = 2.5
    interval_days: float = 0.0
    due: Optional[dt.date] = None
    last_review: Optional[dt.datetime] = None
    mastery: float = 0.0  # 0..1; graduation prompt fires above config threshold
    history: List[Dict[str, Any]] = Field(default_factory=list)


class HabitMeta(BaseModel):
    """Area habit tracking (§8): fixed schedule + weekly continue/change check-in."""

    model_config = ConfigDict(extra="ignore")

    cadence: Cadence = Cadence.WEEKLY
    target_count: int = 3  # occurrences per cadence period
    checkins: List[Dict[str, Any]] = Field(default_factory=list)
    last_checkin: Optional[dt.date] = None
    log: List[dt.date] = Field(default_factory=list)


#: Which weekdays a habit lands on for a given "n times per period" target.
#: Spread rather than clumped — 3× a week is Mon/Wed/Fri, not Mon/Tue/Wed.
DEFAULT_HABIT_DAYS: Dict[int, List[int]] = {
    1: [2],                       # Wed
    2: [1, 4],                    # Tue, Fri
    3: [0, 2, 4],                 # Mon, Wed, Fri
    4: [0, 1, 3, 5],              # Mon, Tue, Thu, Sat
    5: [0, 1, 2, 3, 4],           # weekdays
    6: [0, 1, 2, 3, 4, 5],        # all but Sunday
    7: [0, 1, 2, 3, 4, 5, 6],     # every day
}


class AreaSchedule(BaseModel):
    """When an Area actually happens.

    An Area has no end state, so it gets no deadline and no task (§2) — it
    gets a *recurring event*: a real block of time on the calendar that
    repeats on the habit's cadence. This block is the editable half. Change
    the time, the length or the days here and the next sync moves the whole
    series; the weekly schedule review exists to make that a habit in itself.

    `days` empty means "derive from the habit target" — set 3× per week and
    you get Mon/Wed/Fri without choosing them. Setting `days` explicitly pins
    the series, and changing the target no longer moves it.
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    time: str = "18:00"                             # local start, HH:MM
    duration_minutes: int = Field(default=30, ge=5, le=720)
    days: List[int] = Field(default_factory=list)   # 0=Mon .. 6=Sun
    monthday: int = Field(default=1, ge=1, le=28)   # monthly cadence
    start: Optional[dt.date] = None                 # first occurrence; None = today
    until: Optional[dt.date] = None                 # None = open-ended

    def start_time(self) -> dt.time:
        try:
            return dt.time.fromisoformat(self.time)
        except ValueError:
            return dt.time(18, 0)

    def effective_days(self, habit: Optional["HabitMeta"] = None) -> List[int]:
        if self.days:
            return sorted({int(d) % 7 for d in self.days})
        target = max(1, min(7, habit.target_count if habit else 1))
        return list(DEFAULT_HABIT_DAYS[target])


class ReviewMeta(BaseModel):
    """Resource review cycle (§2): 'not needed anymore?' on a timer."""

    model_config = ConfigDict(extra="ignore")

    cycle_days: int = 90
    next: Optional[dt.date] = None
    last: Optional[dt.date] = None


class HistoryEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    at: dt.datetime
    event: str  # captured | classified | graduated | archived | restored | ...
    detail: Optional[str] = None


# --------------------------------------------------------------------------
# the note
# --------------------------------------------------------------------------


class Note(BaseModel):
    """One Obsidian file. Frontmatter is this model minus `body`."""

    model_config = ConfigDict(extra="allow")  # never clobber a human's own keys

    id: str
    title: str
    bucket: Bucket = Bucket.INBOX
    created: dt.datetime = Field(default_factory=now)
    updated: dt.datetime = Field(default_factory=now)
    tags: List[str] = Field(default_factory=list)
    source: str = "capture"  # capture | graduation | restore | import
    history: List[HistoryEntry] = Field(default_factory=list)

    # Colour keyword (fun / health / chore / hw / study / quiz / ...). Left
    # unset the system detects one from the text; set by hand it is never
    # overwritten, so a correction sticks. See sb/taxonomy.py.
    category: Optional[str] = None

    project: Optional[ProjectMeta] = None
    srs: Optional[SrsState] = None
    habit: Optional[HabitMeta] = None
    schedule: Optional[AreaSchedule] = None
    review: Optional[ReviewMeta] = None

    body: str = ""

    # -- construction -------------------------------------------------------

    @classmethod
    def capture(cls, text: str, bucket: Bucket = Bucket.INBOX, title: str = "") -> "Note":
        title = (title or first_line(text)).strip()
        note = cls(id=new_id(title), title=title, bucket=bucket, body=text.strip())
        note.log("captured", f"bucket={bucket.value}")
        return note

    # -- mutation -----------------------------------------------------------

    def log(self, event: str, detail: Optional[str] = None) -> None:
        self.history.append(HistoryEntry(at=now(), event=event, detail=detail))
        self.updated = now()

    def touch(self) -> None:
        self.updated = now()

    # -- serialization ------------------------------------------------------

    def frontmatter(self) -> Dict[str, Any]:
        data = self.model_dump(mode="json", exclude_none=True, exclude={"body"})
        # drop empty containers so the frontmatter stays readable in Obsidian
        return {k: v for k, v in data.items() if v not in ([], {}, "")}

    @classmethod
    def from_frontmatter(cls, meta: Dict[str, Any], body: str) -> "Note":
        return cls(**{**meta, "body": body})


def first_line(text: str, max_len: int = 80) -> str:
    """Derive a title from a raw capture: first non-empty line, trimmed."""
    for line in text.strip().split("\n"):
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:max_len]
    return "Untitled"
