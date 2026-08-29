"""What `desired_retention: 0.9` actually costs, in minutes a day.

0.9 is the FSRS default. It is in `config.yaml` because it had to be *some*
number, and it has never been a decision — which matters, because the choice
is not "how well do I want to remember things" but a trade, and the trade is
steep and invisible at the point of choosing.

Raising retention shortens every interval. Shortening every interval multiplies
the number of reviews. The relationship is superlinear at the top end: the
jump from 0.90 to 0.95 costs far more than the jump from 0.85 to 0.90 buys,
because interval length falls away sharply as the target approaches certainty.

Jarrett Ye's FSRS simulator work is the reference, and his result is the
counter-intuitive one worth surfacing: for most people **optimal retention is
below 0.9**, often nearer 0.85. Optimal meaning: the most material actually
retained per minute spent, rather than the highest per-card probability. A
higher target spends your minutes re-drilling things you already know.

## How the projection is computed

Honestly, and with its assumptions stated, because a made-up number here would
be worse than no number:

  * A card with stability S, under target retention r, comes back every
    `interval_for(S, r)` days. That is FSRS's own inverted forgetting curve —
    the same function the scheduler uses, not a model of it.
  * So it contributes `1 / interval` reviews per day. Summing that over every
    scheduled card is the steady-state review load.
  * Times the measured seconds-per-review from lj's own log, which is the only
    part of this that is not arithmetic.

What it deliberately does **not** model: new cards entering, lapses resetting
stability, or the fact that a lower retention causes more lapses and therefore
more short intervals. All three make low retention slightly worse than shown,
so the curve here is a mild *under*statement of the cost of going low — which
is the safe direction for a number that is arguing for going low.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

from . import fsrs
from .cards import Card, Deck

#: The dial's range. Below 0.7 the scheduler is barely scheduling; above 0.97
#: intervals collapse and the load runs away.
GRID = (0.70, 0.75, 0.80, 0.85, 0.87, 0.90, 0.92, 0.95, 0.97)

#: Used until lj has enough reviews to measure their own pace.
DEFAULT_SECONDS_PER_REVIEW = 12.0
MIN_REVIEWS_TO_MEASURE = 30

#: Knowledge is only worth what it costs to keep. This is the figure the
#: "optimal" row maximises: expected cards still known, per minute per day.
#: It is Ye's framing, and it is the reason the answer is usually under 0.9.
def _knowledge_per_minute(cards_known: float, minutes: float) -> float:
    return cards_known / minutes if minutes > 0 else 0.0


def seconds_per_review(reviews: Iterable[Dict[str, Any]]) -> float:
    """lj's own pace, median, or the default when there is not enough log.

    Median rather than mean: one review left open while making tea would
    otherwise put every projection out by a factor of five.
    """
    times = []
    for rec in reviews:
        try:
            value = float(rec.get("seconds") or 0)
        except (TypeError, ValueError):
            continue
        if 1.0 <= value <= 120.0:
            times.append(value)
    if len(times) < MIN_REVIEWS_TO_MEASURE:
        return DEFAULT_SECONDS_PER_REVIEW
    return round(statistics.median(times), 2)


@dataclass
class Point:
    retention: float
    reviews_per_day: float
    minutes_per_day: float
    cards_known: float
    knowledge_per_minute: float
    is_current: bool = False
    is_optimal: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "retention": self.retention,
            "reviews_per_day": round(self.reviews_per_day, 2),
            "minutes_per_day": round(self.minutes_per_day, 1),
            "cards_known": round(self.cards_known, 1),
            "knowledge_per_minute": round(self.knowledge_per_minute, 4),
            "is_current": self.is_current,
            "is_optimal": self.is_optimal,
        }


def scheduled_cards(decks: Sequence[Deck]) -> List[Card]:
    """Cards with real stability. New and draft cards have no interval to
    shorten, so including them would flatten the whole curve."""
    out: List[Card] = []
    for deck in decks:
        for card in deck.cards:
            if card.status == "active" and card.stability > 0:
                out.append(card)
    return out


def curve(
    decks: Sequence[Deck],
    *,
    current: float = 0.9,
    seconds: float = DEFAULT_SECONDS_PER_REVIEW,
    grid: Sequence[float] = GRID,
) -> Dict[str, Any]:
    """Daily minutes, and cards retained, across the range of the dial."""
    cards = scheduled_cards(decks)
    points: List[Point] = []
    for r in grid:
        reviews_per_day = 0.0
        for card in cards:
            interval = fsrs.interval_for(card.stability, r)
            if interval > 0:
                reviews_per_day += 1.0 / interval
        minutes = reviews_per_day * seconds / 60.0
        # Expected cards still known at any moment is just the target applied
        # to the collection — that is what the target *means*.
        known = len(cards) * r
        points.append(
            Point(
                retention=r,
                reviews_per_day=reviews_per_day,
                minutes_per_day=minutes,
                cards_known=known,
                knowledge_per_minute=_knowledge_per_minute(known, minutes),
            )
        )

    best: Optional[Point] = None
    for point in points:
        if abs(point.retention - current) < 1e-9:
            point.is_current = True
        if point.minutes_per_day > 0 and (
            best is None or point.knowledge_per_minute > best.knowledge_per_minute
        ):
            best = point
    if best is not None:
        best.is_optimal = True

    now = next((p for p in points if p.is_current), None)
    return {
        "cards": len(cards),
        "seconds_per_review": seconds,
        "current": current,
        "points": [p.as_dict() for p in points],
        "optimal": best.retention if best else None,
        "message": _message(cards, now, best, current),
    }


def _message(cards: Sequence[Card], now: Optional[Point], best: Optional[Point], current: float) -> str:
    if not cards:
        return (
            "Nothing scheduled yet — this fills in once cards have been "
            "reviewed a few times and have a real interval."
        )
    if now is None or best is None:
        return f"{len(cards)} scheduled cards."
    if abs(best.retention - current) < 1e-9:
        return (
            f"{round(now.minutes_per_day)} min/day at {current:.2f}. "
            "That is already the best return on your time."
        )
    delta = now.minutes_per_day - best.minutes_per_day
    direction = "lower" if best.retention < current else "higher"
    return (
        f"{round(now.minutes_per_day)} min/day at {current:.2f}. "
        f"Going {direction}, to {best.retention:.2f}, would be "
        f"{abs(round(delta))} min/day {'less' if delta > 0 else 'more'} for the "
        "most retained per minute — Ye's result, that the best target is "
        "usually under 0.9."
    )
