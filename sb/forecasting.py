"""Estimates, scored against what actually happened.

`ProjectMeta.estimate_minutes` drives every calendar block this system makes,
and until now nothing ever checked it against reality. That is not a small
gap. It is the one place in the whole design where a number is produced,
acted on, and never audited — which is the exact shape of the failure the
planning-fallacy literature is about.

**Buehler, Griffin & Ross (1994)** is the finding: people underestimate how
long their own tasks will take, they do it even when they *know* they have
done it before, and — the part that matters here — the bias does not appear
when the same people estimate someone else's task. The error is not ignorance
about durations. It is that planning from the inside means imagining the
successful path, and the successful path is the fast one.

**Kahneman & Tversky's outside view** is the correction: stop reconstructing
the task and go look at how long tasks *like this one* actually took.
**Flyvbjerg** turned that into a working method — reference-class forecasting,
now mandatory for UK public capital projects: take the class, take its
observed distribution, apply its observed uplift.

So this module does exactly that and nothing cleverer:

  * every finished step records the minutes it really took, in the note, in
    the vault, where nothing can silently lose it;
  * finished steps are grouped into **reference classes** — by difficulty
    level first, then all of them — and each class carries the ratio of actual
    to estimated;
  * a new estimate is multiplied by its class's ratio, if that class has
    enough history to mean anything, and otherwise by the global one, and
    otherwise not at all.

The multiplier is reported alongside every estimate it touches. An estimate
silently doubled is a system lj stops trusting; an estimate that says "90 min
(45 × 2.0, from your last 14 steps)" is one they can argue with.
"""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .models import Note, Step

#: Below this many finished steps a class ratio is one person's bad week.
#: Flyvbjerg's own guidance is that a reference class needs enough members to
#: have a distribution; five is the floor at which a median stops being noise.
MIN_CLASS_SAMPLES = 5

#: How far a personal multiplier is allowed to move an estimate. Not because
#: the data would be wrong, but because a 6x multiplier on one catastrophic
#: afternoon would put a week of calendar blocks on a two-hour job and lj
#: would turn the whole feature off.
MIN_MULTIPLIER = 0.5
MAX_MULTIPLIER = 3.0

#: Steps this far from their estimate are almost always a mis-log — a timer
#: left running overnight, a step ticked weeks later. Excluded from the fit
#: and reported separately rather than quietly dropped.
OUTLIER_RATIO = 12.0


@dataclass
class Observation:
    note_id: str
    title: str
    step: str
    estimated: int
    actual: int
    level: int = 3
    at: Optional[dt.datetime] = None

    @property
    def ratio(self) -> float:
        return self.actual / self.estimated if self.estimated > 0 else 0.0


def observations(notes: Iterable[Note]) -> List[Observation]:
    """Every finished step that recorded how long it really took."""
    out: List[Observation] = []
    for note in notes:
        project = note.project
        if not project:
            continue
        for step in project.steps:
            actual = getattr(step, "actual_minutes", None)
            if not step.done or not actual or actual <= 0 or step.minutes <= 0:
                continue
            out.append(
                Observation(
                    note_id=note.id,
                    title=note.title,
                    step=step.text,
                    estimated=int(step.minutes),
                    actual=int(actual),
                    level=int(project.level or 3),
                    at=step.done_at,
                )
            )
    return out


@dataclass
class Class:
    """One reference class and what it says."""

    name: str
    n: int = 0
    multiplier: float = 1.0
    median_ratio: float = 1.0
    estimated_total: int = 0
    actual_total: int = 0
    enough: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "n": self.n,
            "multiplier": self.multiplier,
            "median_ratio": self.median_ratio,
            "estimated_total": self.estimated_total,
            "actual_total": self.actual_total,
            "enough": self.enough,
        }


def _clamp(value: float) -> float:
    return round(max(MIN_MULTIPLIER, min(MAX_MULTIPLIER, value)), 3)


def build_class(name: str, items: Sequence[Observation]) -> Class:
    """The median ratio, not the mean.

    One step that took eight hours instead of one would drag a mean far enough
    to make every future estimate absurd. The median is what the reference-class
    method actually calls for, and it is robust to precisely that.
    """
    usable = [o for o in items if 0 < o.ratio < OUTLIER_RATIO]
    result = Class(name=name, n=len(usable))
    if not usable:
        return result
    result.median_ratio = round(statistics.median(o.ratio for o in usable), 3)
    result.estimated_total = sum(o.estimated for o in usable)
    result.actual_total = sum(o.actual for o in usable)
    result.enough = len(usable) >= MIN_CLASS_SAMPLES
    result.multiplier = _clamp(result.median_ratio) if result.enough else 1.0
    return result


def classes(items: Sequence[Observation]) -> Dict[str, Class]:
    """The global class, plus one per difficulty level.

    Level is the only reference class the schema already carries that plausibly
    predicts overrun — a level-5 project is level 5 *because* lj expected it to
    be hard. Skills would be a finer class and there will never be enough of
    any one of them to fit.
    """
    out: Dict[str, Class] = {"all": build_class("all", items)}
    for level in sorted({o.level for o in items}):
        key = f"level-{level}"
        out[key] = build_class(key, [o for o in items if o.level == level])
    return out


@dataclass
class Forecast:
    minutes: int
    multiplier: float = 1.0
    basis: str = "none"        # which class was used
    n: int = 0
    explanation: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "minutes": self.minutes,
            "multiplier": self.multiplier,
            "basis": self.basis,
            "n": self.n,
            "explanation": self.explanation,
        }


def adjust(estimate_minutes: int, level: int, table: Dict[str, Class]) -> Forecast:
    """Apply the outside view to one estimate.

    Class first, global second, unchanged third. Never silently: the
    explanation is the whole reason this is honest rather than paternalistic.
    """
    estimate = max(0, int(estimate_minutes or 0))
    level_class = table.get(f"level-{level}")
    chosen: Optional[Class] = None
    if level_class and level_class.enough:
        chosen = level_class
    elif table.get("all") and table["all"].enough:
        chosen = table["all"]

    if chosen is None or estimate == 0:
        return Forecast(
            minutes=estimate,
            multiplier=1.0,
            basis="none",
            n=table.get("all").n if table.get("all") else 0,
            explanation=(
                "No adjustment yet — finish a few steps with a recorded "
                f"duration and this starts using them (needs {MIN_CLASS_SAMPLES})."
            ),
        )

    adjusted = int(round(estimate * chosen.multiplier))
    return Forecast(
        minutes=adjusted,
        multiplier=chosen.multiplier,
        basis=chosen.name,
        n=chosen.n,
        explanation=(
            f"{adjusted} min = {estimate} × {chosen.multiplier} "
            f"(your last {chosen.n} "
            f"{'steps at this level' if chosen.name != 'all' else 'finished steps'})"
        ),
    )


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def project_accuracy(note: Note) -> Optional[Dict[str, Any]]:
    """Estimated versus actual for one Project, or None if nothing is timed."""
    if not note.project:
        return None
    done = [
        s for s in note.project.steps
        if s.done and getattr(s, "actual_minutes", None) and s.minutes > 0
    ]
    if not done:
        return None
    estimated = sum(s.minutes for s in done)
    actual = sum(int(s.actual_minutes or 0) for s in done)
    return {
        "steps": len(done),
        "estimated_minutes": estimated,
        "actual_minutes": actual,
        "ratio": round(actual / estimated, 3) if estimated else None,
        "remaining_estimate": sum(s.minutes for s in note.project.remaining_steps),
    }


def summary(notes: Iterable[Note]) -> Dict[str, Any]:
    """The whole picture: the multiplier, the classes, and the worst misses."""
    items = observations(notes)
    table = classes(items)
    overall = table["all"]
    worst = sorted(
        (o for o in items if 0 < o.ratio < OUTLIER_RATIO),
        key=lambda o: -abs(o.ratio - 1.0),
    )[:5]
    return {
        "n": overall.n,
        "enough": overall.enough,
        "multiplier": overall.multiplier,
        "median_ratio": overall.median_ratio,
        "estimated_total": overall.estimated_total,
        "actual_total": overall.actual_total,
        "classes": [c.as_dict() for c in table.values()],
        "outliers": len([o for o in items if o.ratio >= OUTLIER_RATIO]),
        "worst": [
            {
                "note_id": o.note_id,
                "title": o.title,
                "step": o.step,
                "estimated": o.estimated,
                "actual": o.actual,
                "ratio": round(o.ratio, 2),
            }
            for o in worst
        ],
        "message": _summary_message(overall),
    }


def _summary_message(overall: Class) -> str:
    if not overall.n:
        return (
            "Nothing timed yet. Start a step and tick it off and this begins "
            "scoring your estimates against what actually happened."
        )
    if not overall.enough:
        return (
            f"{overall.n} timed step{'s' if overall.n != 1 else ''} — "
            f"{MIN_CLASS_SAMPLES} before the multiplier starts being applied."
        )
    if overall.multiplier > 1.15:
        return (
            f"Your steps take about {overall.multiplier}× as long as you plan. "
            "New estimates are scaled by that — the outside view, not a guess."
        )
    if overall.multiplier < 0.9:
        return (
            f"You finish in about {overall.multiplier}× your estimate — you are "
            "budgeting more than you need."
        )
    return "Your estimates hold up. Nothing is being adjusted."
