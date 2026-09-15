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
import threading
import unicodedata
from enum import Enum
from functools import lru_cache
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


_SLUG_DROP = re.compile(r"[^\w\s-]")
_SLUG_JOIN = re.compile(r"[\s_-]+")


@lru_cache(maxsize=8192)
def slugify(text: str, max_len: int = 60) -> str:
    # Pure, and called for every title in the vault by the link and filename
    # indexes — memoised.
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = _SLUG_DROP.sub("", text).strip().lower()
    text = _SLUG_JOIN.sub("-", text)
    return text[:max_len].strip("-") or "untitled"


#: slug -> the last second stamped onto an id with that slug. See `new_id`.
#: Bounded: only titles captured in this process are in it, and the oldest
#: are dropped past the cap, which cannot reintroduce a collision — an
#: evicted entry is by then far enough in the past that the wall clock has
#: moved on anyway.
_issued: "Dict[str, dt.datetime]" = {}
_ISSUED_MAX = 512
_id_lock = threading.Lock()


def new_id(title: str) -> str:
    """Stable, sortable, human-readable id: 20260822T093012-learn-rust-generics.

    The stamp is second-resolution, and the id is the stamp plus the title's
    slug — so two notes with the same title, captured in the same second, used
    to be *the same id*. That is not a cosmetic clash: `path_for` builds the
    filename from the same two parts, so the second note overwrote the first
    on disk, and `find` could not tell them apart afterwards even if it
    hadn't. Two terms typed quickly into a lecture is exactly that shape of
    capture, and the loss was silent — no error, anywhere.

    The format is load-bearing (sortable, and `path_for` slices the first 15
    characters as the filename stamp), so the fix is not to change it but to
    refuse to issue the same one twice: a repeat advances the stamp by a
    second. Ids stay unique, sortable and the same shape, and a capture can
    at worst be stamped a second or two late — which is a price nothing in
    this system can notice.
    """
    slug = slugify(title, 40)
    with _id_lock:
        at = now()
        last = _issued.get(slug)
        if last is not None and at <= last:
            # Per slug, not globally: two *different* titles in one second are
            # already two different ids, and should keep the time they really
            # happened. Only a repeat of the same title has to move.
            at = last + dt.timedelta(seconds=1)
        _issued[slug] = at
        if len(_issued) > _ISSUED_MAX:
            for stale in list(_issued)[: len(_issued) - _ISSUED_MAX]:
                del _issued[stale]
        return f"{at.strftime('%Y%m%dT%H%M%S')}-{slug}"


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
    #: When the clock was started on this step, and how long it really took.
    #: `minutes` is the plan and is never overwritten by reality — keeping both
    #: is the entire point, because the pair is the training data for the
    #: reference-class correction in sb/forecasting.py.
    started_at: Optional[dt.datetime] = None
    actual_minutes: Optional[int] = None

    @property
    def overrun(self) -> Optional[float]:
        """Actual over estimated, or None if this step was never timed."""
        if not self.actual_minutes or self.minutes <= 0:
            return None
        return round(self.actual_minutes / self.minutes, 3)


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


class HabitEvent(BaseModel):
    """One occurrence: when, and — if known — what time and where.

    Time and place are not decoration. Wendy Wood's finding is that context
    stability, not frequency, is what turns a behaviour automatic, and a bare
    date cannot express that. Both are optional, because a habit logged with
    a tap and no context is still a habit logged.
    """

    model_config = ConfigDict(extra="ignore")

    on: dt.date
    at: str = ""      # HH:MM, when it actually happened
    place: str = ""


class HabitMeta(BaseModel):
    """Area habit tracking (§8), rebuilt around the causal levers.

    `target_count` was the whole of phase 3's habit model and is the weakest
    thing here: it measures the habit rather than causing it. The fields above
    it are ordered by effect size in the literature — see sb/habits.py, which
    holds the reasoning and the citations. They are separate strings rather
    than one free-text plan because a sentence with a blank in it is a
    sentence that gets left blank.
    """

    model_config = ConfigDict(extra="ignore")

    #: Gollwitzer's implementation intention: "When [cue], I will [behaviour]
    #: at [place]." d ≈ 0.65 — the largest single effect in the field.
    cue: str = ""
    behaviour: str = ""
    place: str = ""
    #: Fogg / Clear: the existing routine this attaches to. "After what?"
    anchor: str = ""
    #: Wood: one thing made easier for this, one made harder for its rival.
    easier: str = ""
    harder: str = ""

    cadence: Cadence = Cadence.WEEKLY
    target_count: int = 3  # occurrences per cadence period
    checkins: List[Dict[str, Any]] = Field(default_factory=list)
    last_checkin: Optional[dt.date] = None
    log: List[HabitEvent] = Field(default_factory=list)

    @field_validator("log", mode="before")
    @classmethod
    def _coerce_log(cls, value: Any) -> Any:
        """Accept the old shape: a bare list of dates.

        Every habit logged before occurrences carried a time and a place is a
        list of `date` on disk, and the vault is the source of truth — nobody
        is going to migrate it by hand.
        """
        if not isinstance(value, list):
            return value
        out: List[Any] = []
        for item in value:
            if isinstance(item, (dt.date, str)):
                out.append({"on": item})
            else:
                out.append(item)
        return out

    @property
    def dates(self) -> List[dt.date]:
        """The occurrence dates alone — what the old `log` used to be."""
        return [e.on for e in self.log if e.on]


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


class IntakeMeta(BaseModel):
    """Where a note came from when it arrived as a file (see sb/intake.py).

    Kept on the note rather than only in a log, because the question this
    answers — "why is this filed here?" — gets asked while looking at the
    note, and a log has rotated by then. `filed_automatically` false means the
    note is waiting in the Inbox for lj to confirm the suggestion.
    """

    model_config = ConfigDict(extra="ignore")

    file: str = ""                        # the dropped filename
    dropped_at: Optional[dt.datetime] = None
    suggested: str = ""                   # area | project | resource
    confidence: float = 0.0               # 0..1; below the floor means "ask"
    reason: str = ""
    signals: List[str] = Field(default_factory=list)
    decided_by: str = "rules"             # rules | rules+model | model | manual
    filed_automatically: bool = False


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
    source: str = "capture"  # capture | drop | graduation | restore | import
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
    intake: Optional[IntakeMeta] = None

    body: str = ""

    # -- construction -------------------------------------------------------

    @classmethod
    def capture(cls, text: str, bucket: Bucket = Bucket.INBOX, title: str = "") -> "Note":
        if "\r" in text:
            # Windows clipboard text is CRLF; written back through a text-mode
            # file on Windows it would become CR CR LF.
            text = text.replace("\r\n", "\n").replace("\r", "\n")
        title = (title or first_line(text)).strip()
        note = cls(id=new_id(title), title=title, bucket=bucket, body=text.strip())
        note.log("captured", f"bucket={bucket.value}")
        return note

    # -- mutation -----------------------------------------------------------

    def log(self, event: str, detail: Optional[str] = None) -> None:
        at = now()
        self.history.append(HistoryEntry(at=at, event=event, detail=detail))
        self.updated = at

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
