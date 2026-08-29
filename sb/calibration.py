"""Did you know that you knew it?

The scheduler knows whether lj was right. It has never known whether lj
*thought* they would be right, and the gap between those two is the only
signal in the whole system that points at a specific, fixable belief.

Koriat & Bjork (2005) named the mechanism: the **illusion of competence**.
While the answer is in front of you, or while the question feels familiar,
retrieval feels easy — and that feeling is what people use to decide they have
studied enough. It is systematically wrong in one direction. Kornell & Bjork's
work on judgments of learning shows the same: the judgment is made on fluency,
not on retrievability, and fluency is exactly what re-reading inflates.

Dunlosky & Rawson (2012) is why this is worth building rather than merely
knowing: training calibration improved retention **more than the same minutes
spent on extra review did**. The cheapest intervention here is not more cards.
It is showing lj the four cards a week they were sure about and wrong on.

## What is recorded

One number, before the answer is revealed: how likely lj thinks they are to
get it. Stored on the review-log line beside the grade, so nothing new has to
be kept in sync and every historical line simply has no confidence on it.

## What is computed

* a **calibration curve** — predicted probability, bucketed, against the
  accuracy actually observed in each bucket. Perfect calibration is the
  diagonal; the usual human shape is above it at the top end.
* a **Brier score** — mean squared error of the prediction, one number,
  lower is better. 0.25 is what you get by answering 50% to everything, so it
  is the line worth beating.
* the **overconfident band** — high confidence, wrong answer. Those cards are
  the priority queue: not the hardest cards, the ones lj does not yet know
  are hard.

Underconfidence is reported but not acted on. Being wrong about knowing
something is a study problem; being right but unsure is not, and re-drilling
it would spend real minutes on nothing.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set

#: A grade of 3 (Good) or 4 (Easy) counts as recall; 1 (Again) and 2 (Hard)
#: do not. Hard is a miss on purpose — Anki's Hard means "I got there, but
#: barely", and a confident prediction followed by a barely is exactly the
#: overestimate this module exists to surface.
CORRECT_FROM = 3

#: Confidence at or above this, on a card that was then missed, is the band
#: worth interrupting lj about.
OVERCONFIDENT_AT = 0.8

#: Ten-point buckets are too fine for a few hundred reviews and too coarse to
#: be useless; five buckets is what a person can read off a chart.
BINS = ((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0001))

#: The three-point tap the study page offers, and what each is worth. Numbers
#: rather than labels because the Brier score needs a probability, and asking
#: a person for a probability mid-session is a good way to stop the session.
TAPS = {"sure": 0.9, "think-so": 0.6, "no-idea": 0.2}

#: Below this many judged reviews, a Brier score is noise and the curve is a
#: handful of dots. Reported, but flagged as not yet meaning anything.
MIN_FOR_SCORE = 20


def clamp(value: Any) -> Optional[float]:
    """Coerce whatever arrived to a probability, or None.

    Accepts the tap names, 0..1 floats, and 0..100 integers, because all three
    are things a caller might reasonably send and none of them should become a
    500. Anything else is None — an unanswerable prediction is better recorded
    as absent than as a guess about a guess.
    """
    if value is None or value == "":
        return None
    if isinstance(value, str):
        key = value.strip().lower()
        if key in TAPS:
            return TAPS[key]
        try:
            value = float(key)
        except ValueError:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 1.0:
        number = number / 100.0
    return max(0.0, min(1.0, round(number, 4)))


def is_correct(grade: Any) -> bool:
    try:
        return int(grade) >= CORRECT_FROM
    except (TypeError, ValueError):
        return False


@dataclass
class Judged:
    """One review that carried a prediction."""

    at: Optional[dt.datetime]
    note_id: str
    subject: str
    card: str
    confidence: float
    grade: int

    @property
    def correct(self) -> bool:
        return is_correct(self.grade)

    @property
    def overconfident(self) -> bool:
        return self.confidence >= OVERCONFIDENT_AT and not self.correct

    @property
    def underconfident(self) -> bool:
        return self.confidence <= (1.0 - OVERCONFIDENT_AT) and self.correct


@dataclass
class Calibration:
    n: int = 0
    brier: Optional[float] = None
    mean_confidence: Optional[float] = None
    mean_accuracy: Optional[float] = None
    overconfidence: Optional[float] = None   # confidence minus accuracy
    bins: List[Dict[str, Any]] = field(default_factory=list)
    overconfident: int = 0
    underconfident: int = 0
    enough: bool = False
    message: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "n": self.n,
            "brier": self.brier,
            "mean_confidence": self.mean_confidence,
            "mean_accuracy": self.mean_accuracy,
            "overconfidence": self.overconfidence,
            "bins": self.bins,
            "overconfident": self.overconfident,
            "underconfident": self.underconfident,
            "enough": self.enough,
            "message": self.message,
        }


def judged(reviews: Iterable[Dict[str, Any]]) -> List[Judged]:
    """The reviews that carried a prediction. Everything before this shipped
    has none, and that is not a gap to fill in — it is simply not data."""
    out: List[Judged] = []
    for rec in reviews:
        confidence = clamp(rec.get("confidence"))
        if confidence is None:
            continue
        at = rec.get("at")
        parsed: Optional[dt.datetime] = None
        if isinstance(at, str):
            try:
                parsed = dt.datetime.fromisoformat(at)
            except ValueError:
                parsed = None
        out.append(
            Judged(
                at=parsed,
                note_id=str(rec.get("note_id") or ""),
                subject=str(rec.get("subject") or ""),
                card=str(rec.get("card") or ""),
                confidence=confidence,
                grade=int(rec.get("grade") or 0),
            )
        )
    return out


def curve(records: Iterable[Dict[str, Any]]) -> Calibration:
    """Predicted versus actual, plus the one number that summarises it."""
    items = judged(records)
    result = Calibration(n=len(items))
    if not items:
        result.message = (
            "No predictions yet. Tap how sure you are before revealing an "
            "answer and this fills in."
        )
        return result

    result.brier = round(
        sum((j.confidence - (1.0 if j.correct else 0.0)) ** 2 for j in items) / len(items), 4
    )
    result.mean_confidence = round(sum(j.confidence for j in items) / len(items), 4)
    result.mean_accuracy = round(sum(1 for j in items if j.correct) / len(items), 4)
    result.overconfidence = round(result.mean_confidence - result.mean_accuracy, 4)
    result.overconfident = sum(1 for j in items if j.overconfident)
    result.underconfident = sum(1 for j in items if j.underconfident)
    result.enough = len(items) >= MIN_FOR_SCORE

    for low, high in BINS:
        inside = [j for j in items if low <= j.confidence < high]
        result.bins.append(
            {
                "low": low,
                "high": min(high, 1.0),
                "label": f"{int(low * 100)}–{int(min(high, 1.0) * 100)}%",
                "n": len(inside),
                "predicted": (
                    round(sum(j.confidence for j in inside) / len(inside), 4) if inside else None
                ),
                "actual": (
                    round(sum(1 for j in inside if j.correct) / len(inside), 4) if inside else None
                ),
            }
        )

    result.message = _message(result)
    return result


def _message(result: Calibration) -> str:
    if not result.enough:
        return (
            f"{result.n} prediction{'s' if result.n != 1 else ''} so far — "
            f"the score starts meaning something around {MIN_FOR_SCORE}."
        )
    gap = result.overconfidence or 0.0
    if gap > 0.1:
        return (
            f"You are {round(gap * 100)} points more confident than correct. "
            "The cards below are the ones to look at."
        )
    if gap < -0.1:
        return (
            f"You are {round(-gap * 100)} points harder on yourself than the "
            "grades are. Worth trusting the first answer more."
        )
    return "Well calibrated — what you expect to know is what you know."


def overconfident_cards(
    records: Iterable[Dict[str, Any]], *, since: Optional[dt.date] = None, limit: int = 20
) -> List[Dict[str, Any]]:
    """High confidence, wrong answer — most recent first.

    Deliberately keyed on `(note_id, card)` and de-duplicated, because the
    point is a list of *cards to look at*, not a list of incidents. A card
    missed confidently three times is one entry with a count of three, which
    is also the more alarming way to read it.
    """
    seen: Dict[str, Dict[str, Any]] = {}
    for j in judged(records):
        if not j.overconfident:
            continue
        if since and j.at and j.at.date() < since:
            continue
        key = f"{j.note_id}/{j.card}"
        entry = seen.get(key)
        if entry is None:
            seen[key] = {
                "note_id": j.note_id,
                "subject": j.subject,
                "card": j.card,
                "times": 1,
                "last": j.at.isoformat() if j.at else None,
                "confidence": j.confidence,
            }
        else:
            entry["times"] += 1
            if j.at:
                entry["last"] = j.at.isoformat()
            entry["confidence"] = max(entry["confidence"], j.confidence)
    ranked = sorted(seen.values(), key=lambda e: (-e["times"], e["last"] or ""), reverse=False)
    ranked.sort(key=lambda e: (-e["times"], e["last"] or ""))
    return ranked[:limit]


def priority_card_ids(
    records: Iterable[Dict[str, Any]], *, since: Optional[dt.date] = None
) -> Set[str]:
    """`note_id/card_id` keys for the overconfident band, for session ordering."""
    return {f"{e['note_id']}/{e['card']}" for e in overconfident_cards(records, since=since, limit=200)}
