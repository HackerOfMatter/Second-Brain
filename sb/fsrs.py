"""FSRS — the scheduler behind the tutor.

This is the algorithm Anki ships today (Free Spaced Repetition Scheduler, v5),
and it replaces the SM-2 sketch that `models.SrsState` was originally shaped
for. The reason for the swap is what the two algorithms actually model.

SM-2 tracks one number per card — an "ease factor" — and multiplies the
interval by it. It cannot tell the difference between a card you find hard and
a card you simply have not seen in a long time, so it punishes both the same
way and drifts into ease hell: miss a card three times and its ease bottoms
out permanently, even after you learn it.

FSRS separates three quantities, which is the whole trick:

  * **Stability (S)** — days until recall probability falls to 90%. This is
    memory strength, and it only ever grows when you succeed.
  * **Difficulty (D)** — 1..10, an intrinsic property of the card, not of your
    recent luck with it. It reverts toward the mean over time, so one bad day
    does not brand a card forever.
  * **Retrievability (R)** — probability you would recall it *right now*, a
    function of S and days elapsed. This is what makes a review's value
    depend on when it happens: reviewing at R≈0.9 teaches the model far more
    than reviewing something you saw an hour ago.

Everything here is pure arithmetic on those three numbers. No I/O, no clock
reads except the ones passed in — which is what makes the properties in the
test suite (a Good review always raises stability, Again always lowers it,
intervals grow monotonically with stability) checkable without a vault.

The default weights are the FSRS-5 defaults, fitted by the FSRS project across
millions of real reviews. They are a good starting point for anyone; if lj
ever accumulates a few thousand reviews, `_decks/_reviews.jsonl` holds exactly
the data an optimizer would need to refit them, which is why the log stores
the pre-review state alongside the grade.

Ratings follow Anki's four buttons:

    1 Again   forgot it
    2 Hard    recalled, but it hurt
    3 Good    recalled
    4 Easy    instant
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import List, Optional, Sequence

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

#: Grade values. Named so call sites read as English rather than as magic ints.
AGAIN, HARD, GOOD, EASY = 1, 2, 3, 4
GRADES = (AGAIN, HARD, GOOD, EASY)
GRADE_NAMES = {AGAIN: "again", HARD: "hard", GOOD: "good", EASY: "easy"}

#: The forgetting curve's shape. R(t) = (1 + FACTOR·t/S)^DECAY is a power law,
#: not an exponential — real forgetting has a long tail, and an exponential
#: curve schedules mature cards far too often.
DECAY = -0.5
FACTOR = 0.9 ** (1 / DECAY) - 1  # = 19/81; chosen so R(S, S) == 0.9 exactly

#: FSRS-5 default parameters.
#:  w0..w3   initial stability per grade (Again, Hard, Good, Easy)
#:  w4, w5   initial difficulty curve
#:  w6, w7   difficulty update: step size, and mean reversion
#:  w8..w10  stability growth on success
#:  w11..w14 stability after a lapse
#:  w15, w16 Hard penalty / Easy bonus
#:  w17, w18 same-day (short-term) review
DEFAULT_W: List[float] = [
    0.40255, 1.18385, 3.17300, 15.69105, 7.19490, 0.53450, 1.46040, 0.00460,
    1.54575, 0.11920, 1.01925, 1.93950, 0.11000, 0.29605, 2.26980, 0.23150,
    2.98980, 0.51655, 0.66210,
]

MIN_DIFFICULTY, MAX_DIFFICULTY = 1.0, 10.0
MIN_STABILITY = 0.01


# --------------------------------------------------------------------------
# the memory state
# --------------------------------------------------------------------------


@dataclass
class Memory:
    """A card's scheduling state. `stability` of 0 means never studied."""

    stability: float = 0.0
    difficulty: float = 0.0
    reps: int = 0
    lapses: int = 0

    @property
    def is_new(self) -> bool:
        return self.stability <= 0.0


@dataclass
class Scheduled:
    """The outcome of one review: new memory state plus when to come back."""

    memory: Memory
    interval_days: int
    due: dt.date
    retrievability_before: float
    again: bool  # True when the card should be re-shown later in this session


# --------------------------------------------------------------------------
# the curve
# --------------------------------------------------------------------------


def retrievability(stability: float, elapsed_days: float) -> float:
    """Probability of recalling a card right now, 0..1.

    Undefined for a card never studied; callers get 0.0, which is honest —
    you cannot retrieve what you never encoded.
    """
    if stability <= 0:
        return 0.0
    elapsed = max(0.0, float(elapsed_days))
    return float((1 + FACTOR * elapsed / stability) ** DECAY)


def interval_for(stability: float, desired_retention: float = 0.9) -> float:
    """Days until retrievability decays to `desired_retention`.

    This is the inverse of the curve above, and it is the only place the
    retention target enters the system. Raising it to 0.95 shortens every
    interval; lowering it to 0.85 lengthens them and trades accuracy for
    volume. 0.9 is the flattest part of the workload/retention tradeoff.
    """
    if stability <= 0:
        return 0.0
    r = min(0.99, max(0.70, float(desired_retention)))
    return float(stability / FACTOR * (r ** (1 / DECAY) - 1))


# --------------------------------------------------------------------------
# state transitions
# --------------------------------------------------------------------------


def initial_stability(grade: int, w: Sequence[float] = DEFAULT_W) -> float:
    return max(MIN_STABILITY, float(w[_gi(grade)]))


def initial_difficulty(grade: int, w: Sequence[float] = DEFAULT_W) -> float:
    d = w[4] - math.exp(w[5] * (grade - 1)) + 1
    return _clamp_d(d)


def next_difficulty(difficulty: float, grade: int, w: Sequence[float] = DEFAULT_W) -> float:
    """Difficulty after a review.

    Two moves in one: a step proportional to how far the grade sits from Good,
    damped near the ends of the scale so a card cannot be driven to 10 and
    stranded there; then a pull back toward the difficulty a fresh Easy card
    would have. That second term is the anti-ease-hell mechanism — without it
    a card that gave you trouble in week one stays "hard" forever.
    """
    delta = -w[6] * (grade - GOOD)
    linear = difficulty + delta * (10 - difficulty) / 9  # damped step
    reverted = w[7] * initial_difficulty(EASY, w) + (1 - w[7]) * linear
    return _clamp_d(reverted)


def next_stability_success(
    difficulty: float, stability: float, r: float, grade: int, w: Sequence[float] = DEFAULT_W
) -> float:
    """Stability after recalling the card.

    The growth factor shrinks as difficulty rises, as stability rises (a card
    already stable for a year gains proportionally less), and as R rises —
    that last one is the spacing effect itself: reviewing something you nearly
    forgot is worth several reviews of something still fresh.
    """
    hard_penalty = w[15] if grade == HARD else 1.0
    easy_bonus = w[16] if grade == EASY else 1.0
    growth = (
        math.exp(w[8])
        * (11 - difficulty)
        * (stability ** -w[9])
        * (math.exp(w[10] * (1 - r)) - 1)
        * hard_penalty
        * easy_bonus
    )
    return max(MIN_STABILITY, stability * (1 + growth))


def next_stability_lapse(
    difficulty: float, stability: float, r: float, w: Sequence[float] = DEFAULT_W
) -> float:
    """Stability after forgetting.

    Never larger than the stability you had — a lapse cannot be good news —
    but far from a reset to zero either, which is where SM-2 threw away most
    of what a long-studied card had earned.
    """
    post = (
        w[11]
        * (difficulty ** -w[12])
        * (((stability + 1) ** w[13]) - 1)
        * math.exp(w[14] * (1 - r))
    )
    return max(MIN_STABILITY, min(post, stability))


def next_stability_same_day(
    stability: float, grade: int, w: Sequence[float] = DEFAULT_W
) -> float:
    """Stability after a second look on the same day.

    Re-reviewing within hours barely moves memory, so this deliberately small
    adjustment keeps a card you pressed Again on from being scheduled as if
    the immediate retry were a real, spaced success.
    """
    return max(MIN_STABILITY, stability * math.exp(w[17] * (grade - GOOD + w[18])))


# --------------------------------------------------------------------------
# the one function the rest of the system calls
# --------------------------------------------------------------------------


def review(
    memory: Memory,
    grade: int,
    *,
    last_review: Optional[dt.datetime] = None,
    now: Optional[dt.datetime] = None,
    desired_retention: float = 0.9,
    maximum_interval: int = 3650,
    w: Sequence[float] = DEFAULT_W,
) -> Scheduled:
    """Apply one answer and return the new state plus the next due date.

    `last_review` is what makes this honest about elapsed time: a card
    answered nine days after its last review is a fundamentally different
    event from the same card answered nine minutes after, and the retention
    model needs to see which one happened.
    """
    grade = int(grade)
    if grade not in GRADES:
        raise ValueError(f"grade must be 1..4, got {grade!r}")
    now = now or dt.datetime.now().astimezone()

    if memory.is_new:
        new = Memory(
            stability=initial_stability(grade, w),
            difficulty=initial_difficulty(grade, w),
            reps=memory.reps + 1,
            lapses=memory.lapses + (1 if grade == AGAIN else 0),
        )
        r_before = 0.0
    else:
        elapsed = _elapsed_days(last_review, now)
        r_before = retrievability(memory.stability, elapsed)
        difficulty = next_difficulty(memory.difficulty or initial_difficulty(GOOD, w), grade, w)

        if elapsed < 1.0:
            # Same-day retry: the interval it earns is not evidence of memory.
            stability = next_stability_same_day(memory.stability, grade, w)
        elif grade == AGAIN:
            stability = next_stability_lapse(difficulty, memory.stability, r_before, w)
        else:
            stability = next_stability_success(
                difficulty, memory.stability, r_before, grade, w
            )

        new = Memory(
            stability=stability,
            difficulty=difficulty,
            reps=memory.reps + 1,
            lapses=memory.lapses + (1 if grade == AGAIN else 0),
        )

    days = interval_for(new.stability, desired_retention)
    interval = max(1, min(int(maximum_interval), int(round(days))))
    if grade == AGAIN:
        # A forgotten card comes back tomorrow at the latest, whatever the
        # model says, and again before the session ends.
        interval = 1
    return Scheduled(
        memory=new,
        interval_days=interval,
        due=now.date() + dt.timedelta(days=interval),
        retrievability_before=r_before,
        again=(grade == AGAIN),
    )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _gi(grade: int) -> int:
    return max(0, min(3, int(grade) - 1))


def _clamp_d(value: float) -> float:
    return float(max(MIN_DIFFICULTY, min(MAX_DIFFICULTY, value)))


def _elapsed_days(last_review: Optional[dt.datetime], now: dt.datetime) -> float:
    if not last_review:
        return 0.0
    last = last_review
    if last.tzinfo is None and now.tzinfo is not None:
        last = last.replace(tzinfo=now.tzinfo)
    elif last.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=last.tzinfo)
    return max(0.0, (now - last).total_seconds() / 86400.0)


def preview_intervals(
    memory: Memory,
    *,
    last_review: Optional[dt.datetime] = None,
    now: Optional[dt.datetime] = None,
    desired_retention: float = 0.9,
    maximum_interval: int = 3650,
    w: Sequence[float] = DEFAULT_W,
) -> dict:
    """What each of the four buttons would cost you, in days.

    Shown on the answer buttons the way Anki does it. It is the single
    clearest window into the scheduler: you can see the algorithm's opinion
    before you commit to a grade, which makes an odd interval something you
    notice rather than something that quietly happens.
    """
    out = {}
    for grade in GRADES:
        s = review(
            memory,
            grade,
            last_review=last_review,
            now=now,
            desired_retention=desired_retention,
            maximum_interval=maximum_interval,
            w=w,
        )
        out[GRADE_NAMES[grade]] = s.interval_days
    return out


def humanize(days: int) -> str:
    """`10` -> "10d", `400` -> "1.1y". Button labels, not prose."""
    if days < 1:
        return "<1d"
    if days < 31:
        return f"{days}d"
    if days < 365:
        return f"{days / 30.44:.1f}mo".replace(".0", "")
    return f"{days / 365.25:.1f}y".replace(".0", "")
