"""Turning a guessed cutoff into a measured one.

Two numbers in this system decide things on lj's behalf, and both were picked
by someone thinking about it for a minute:

  * `connect.AUTO_FLOOR = 0.62` — above this a suggested link is written into
    the note without asking.
  * `intake.AUTO_FLOOR = 0.6` — above this a dropped file is filed without
    asking.

Manning, Raghavan & Schütze put it plainly (*Introduction to Information
Retrieval*, ch. 8): a retrieval threshold without a labelled evaluation set is
a preference, not a parameter. You cannot say a cutoff is right or wrong until
you have examples with known answers, and once you do, the cutoff stops being
an opinion and becomes arithmetic.

## The labelled set

This module does not need lj to sit down and label sixty pairs, because the
system is already generating labelled examples every time it asks a question
and gets an answer:

  * every note held below the intake floor and then confirmed is a
    `(score, correct bucket)` pair;
  * every suggested link lj keeps, and every one lj deletes, is a
    `(score, right or wrong)` pair.

Those go to `sb/labels.py`, which stores them where they will survive — not in
`_system/`, which phase 1 declared disposable. A labelled set that a cache
clear destroys is a labelled set you will never accumulate.

## What is computed

The standard sweep. For each candidate cutoff: precision (of the things we
would auto-accept, how many were right), recall (of the things that were
right, how many we would have caught), and F1. Then the recommendation:
**the lowest cutoff whose precision clears the target**, because among cutoffs
that are accurate enough, the lowest one asks lj the fewest questions.

Precision is the constraint rather than F1 for a reason specific to what these
thresholds do. A false positive here is a note filed in the wrong folder, or a
wrong link written into a note — errors that persist silently and that lj may
never look at again. A false negative is a question on the dashboard. Those
are not symmetric costs, so optimising a metric that treats them as equal
would be the wrong thing to optimise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: Precision the roadmap asks for: "set the floor where precision ≥ 0.9".
TARGET_PRECISION = 0.9

#: Below this many labels the sweep is arithmetic on noise. The roadmap says
#: ~60 pairs; 40 is where the numbers stop swinging wildly, and both are
#: reported so the recommendation can be read with the right scepticism.
MIN_LABELS = 40
USABLE_LABELS = 25

#: Cutoffs to try. Two decimal places is finer than any of this data can
#: justify resolving, which is the point — a recommendation of 0.63 versus
#: 0.64 should look as arbitrary as it is.
def _grid(step: float = 0.02) -> List[float]:
    return [round(0.30 + i * step, 2) for i in range(int(0.68 / step) + 1)]


@dataclass
class Point:
    cutoff: float
    accepted: int = 0
    correct: int = 0
    precision: Optional[float] = None
    recall: Optional[float] = None
    f1: Optional[float] = None
    asks: int = 0            # how many lj would be asked about

    def as_dict(self) -> Dict[str, Any]:
        return {
            "cutoff": self.cutoff,
            "accepted": self.accepted,
            "correct": self.correct,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
            "asks": self.asks,
        }


@dataclass
class Sweep:
    name: str = ""
    n: int = 0
    positives: int = 0
    current: Optional[float] = None
    recommended: Optional[float] = None
    enough: bool = False
    usable: bool = False
    points: List[Point] = field(default_factory=list)
    message: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "n": self.n,
            "positives": self.positives,
            "current": self.current,
            "recommended": self.recommended,
            "enough": self.enough,
            "usable": self.usable,
            "target_precision": TARGET_PRECISION,
            "min_labels": MIN_LABELS,
            "points": [p.as_dict() for p in self.points],
            "message": self.message,
        }


def sweep(
    labels: Iterable[Tuple[float, bool]],
    *,
    name: str = "",
    current: Optional[float] = None,
    target: float = TARGET_PRECISION,
) -> Sweep:
    """Precision/recall against every cutoff, and the one to use."""
    pairs: List[Tuple[float, bool]] = [
        (float(score), bool(correct))
        for score, correct in labels
        if score is not None
    ]
    result = Sweep(name=name, n=len(pairs), current=current)
    result.positives = sum(1 for _, ok in pairs if ok)
    result.enough = result.n >= MIN_LABELS
    result.usable = result.n >= USABLE_LABELS
    if not pairs:
        result.message = (
            "No labelled examples yet. Every note you confirm and every link "
            "you keep or delete becomes one — this fills in as you use it."
        )
        return result

    for cutoff in _grid():
        accepted = [(s, ok) for s, ok in pairs if s >= cutoff]
        point = Point(cutoff=cutoff, accepted=len(accepted), asks=len(pairs) - len(accepted))
        point.correct = sum(1 for _, ok in accepted if ok)
        if accepted:
            point.precision = round(point.correct / len(accepted), 4)
        if result.positives:
            point.recall = round(point.correct / result.positives, 4)
        if point.precision and point.recall:
            denom = point.precision + point.recall
            point.f1 = round(2 * point.precision * point.recall / denom, 4) if denom else 0.0
        result.points.append(point)

    # The lowest cutoff that clears the precision target: among cutoffs that
    # are accurate enough, the one that asks lj the fewest questions.
    for point in result.points:
        if point.accepted and point.precision is not None and point.precision >= target:
            result.recommended = point.cutoff
            break

    result.message = _message(result, target)
    return result


def _message(result: Sweep, target: float) -> str:
    if not result.usable:
        return (
            f"{result.n} labelled example{'s' if result.n != 1 else ''} — "
            f"about {MIN_LABELS} before this means much. Nothing has been changed."
        )
    if result.recommended is None:
        return (
            f"No cutoff in range reaches {int(target * 100)}% precision on "
            f"{result.n} examples. The scoring rules are the problem, not the "
            "threshold — a cutoff cannot fix a signal that is not there."
        )
    tail = "" if result.enough else f" Still only {result.n} examples, so treat it as provisional."
    if result.current is None:
        return f"Set the floor to {result.recommended}.{tail}"
    if abs(result.recommended - result.current) < 0.015:
        return (
            f"{result.current} is right — it is where precision reaches "
            f"{int(target * 100)}% on your own {result.n} examples.{tail}"
        )
    direction = "lower" if result.recommended < result.current else "higher"
    return (
        f"{result.current} should be {direction}: {result.recommended} is where "
        f"precision reaches {int(target * 100)}% on your {result.n} examples. "
        f"{'You are being asked more often than you need to be.' if result.recommended < result.current else 'Things are being filed on a guess that is not good enough.'}{tail}"
    )


def report(
    label_sets: Dict[str, Tuple[Sequence[Tuple[float, bool]], Optional[float]]]
) -> Dict[str, Any]:
    """Sweep several thresholds at once — intake and connect, side by side."""
    out = {}
    for name, (labels, current) in label_sets.items():
        out[name] = sweep(labels, name=name, current=current).as_dict()
    return out
