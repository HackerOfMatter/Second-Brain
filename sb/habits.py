"""Habits, done the way the evidence says they work.

Blueprint §8 asked for a weekly continue/change-**count** check-in, and phase 3
built exactly that. Occurrence count is the weakest lever in the literature. It
measures the habit; it does not cause it. Everything here is the causal half,
in descending order of effect size.

**1. Implementation intentions** — *"When [cue], I will [behaviour] at
[place]."* Gollwitzer (1999), meta-analysed across 94 studies: d ≈ 0.65, the
single largest effect in the field, and it comes from writing one sentence.
The mechanism is delegation: the behaviour stops depending on remembering and
starts depending on noticing, because the cue is already specified. A goal
("exercise more") has no cue. This is why the field is three separate strings
rather than one — a sentence with a blank in it is a sentence that gets left
blank.

**2. An anchor.** Fogg's *Tiny Habits*: B = MAP, behaviour needs motivation,
ability and a prompt, and the reliable prompt is an existing routine. James
Clear's habit stacking is the same idea in practitioner form. The anchor is
the answer to "after what?".

**3. Friction.** Wendy Wood's central finding: context and friction outweigh
motivation, and the effect is bidirectional. One thing made easier for the
habit, one thing made harder for what it competes with. Both matter; only
recording one is half a lever.

**4. Never miss twice.** Lally et al. (2010) followed habit formation for 84
days and found automaticity took a **median 66 days** — and, critically, that
a single missed day *did not measurably impair* formation. Two in a row is
where the curve bends. So the check-in reports **consecutive misses**, not
streaks. A streak counter punishes the first miss with the loss of the whole
number, which is precisely the moment the evidence says nothing has gone
wrong; watching people abandon habits at a broken streak is watching a metric
cause the failure it claims to measure.

**5. Context stability.** Wood again: a behaviour performed at the same time
in the same place automates; the same behaviour scattered across the week does
not, at any frequency. So an occurrence records *when and where*, and the
check-in reports how consistent that has been — a number the old count could
never contain.

Nothing here schedules or writes. It reads a `HabitMeta` and answers questions
about it, which keeps the evidence in one file that can be argued with.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .models import Cadence, HabitEvent, HabitMeta

#: Lally et al.: one miss does not impair formation, two in a row is the
#: signal. This is the whole of "never miss twice" as a number.
MISS_ALERT_AT = 2

#: Lally's median. Used only to report progress honestly — "day 31 of a median
#: 66" is a truer thing to show someone than a streak.
MEDIAN_DAYS_TO_AUTOMATICITY = 66

#: Two occurrences within this many minutes of each other count as "the same
#: time of day" for the stability score.
SAME_TIME_MINUTES = 90

#: Below this many occurrences, a stability score is noise.
MIN_FOR_STABILITY = 4


def period_start(on: dt.date, cadence: Cadence) -> dt.date:
    if cadence == Cadence.DAILY:
        return on
    if cadence == Cadence.MONTHLY:
        return on.replace(day=1)
    return on - dt.timedelta(days=on.weekday())


def period_length_days(cadence: Cadence) -> int:
    return {Cadence.DAILY: 1, Cadence.WEEKLY: 7, Cadence.MONTHLY: 30}[cadence]


def periods_between(start: dt.date, end: dt.date, cadence: Cadence) -> List[dt.date]:
    """Every period start from `start`'s period through `end`'s, inclusive."""
    out: List[dt.date] = []
    cursor = period_start(start, cadence)
    last = period_start(end, cadence)
    guard = 0
    while cursor <= last and guard < 5000:
        out.append(cursor)
        guard += 1
        if cadence == Cadence.MONTHLY:
            cursor = (cursor.replace(day=28) + dt.timedelta(days=7)).replace(day=1)
        else:
            cursor = cursor + dt.timedelta(days=period_length_days(cadence))
    return out


# --------------------------------------------------------------------------
# the implementation intention
# --------------------------------------------------------------------------


def intention_sentence(habit: Optional[HabitMeta], fallback: str = "") -> str:
    """Gollwitzer's sentence, or "" when it has not been written.

    Rendered rather than stored, so editing any one field cannot leave a stale
    sentence behind. `fallback` supplies the behaviour when only the cue was
    filled in — the Area's own title is almost always the behaviour.
    """
    if habit is None:
        return ""
    cue = (habit.cue or "").strip().rstrip(",")
    behaviour = (habit.behaviour or fallback or "").strip()
    place = (habit.place or "").strip()
    if not cue or not behaviour:
        return ""
    tail = f" at {place}" if place else ""
    return f"When {cue}, I will {behaviour}{tail}."


def reminder_line(habit: Optional[HabitMeta], fallback: str = "") -> str:
    """The one sentence a *reminder* should carry, or "" if there is none.

    Every channel this system speaks through — an .ics VALARM, a Google
    Calendar event body, the notes on a Google Task, the `today` digest —
    reaches lj at the moment the cue is supposed to fire, which is exactly
    when Gollwitzer's if-then plan does its work. A reminder that says only
    the Area's title has thrown that away and kept the weakest part.

    Degradation is ordered by effect size, and it never invents:

      1. the full implementation intention, when cue and behaviour exist;
      2. the anchor alone ("Right after X: Y") — Fogg's prompt, which is the
         next-strongest thing a reminder can carry;
      3. the cue alone, when the behaviour was never written;
      4. nothing at all.

    Case 4 is a real answer, not a failure to handle: an Area with no cue and
    no anchor has not had this written yet, and printing a manufactured
    sentence would tell lj the strongest lever is in place when it is not.
    `workout-m-f` is that Area today, and `doctor` flagging it is the correct
    outcome — see run.py's habits line.
    """
    if habit is None:
        return ""
    full = intention_sentence(habit, fallback)
    if full:
        return full
    behaviour = (habit.behaviour or fallback or "").strip().rstrip(".")
    anchor = (habit.anchor or "").strip().rstrip(",.")
    cue = (habit.cue or "").strip().rstrip(",.")
    place = (habit.place or "").strip()
    tail = f" at {place}" if place else ""
    if anchor and behaviour:
        return f"Right after {anchor}, I will {behaviour}{tail}."
    if cue and not behaviour:
        return f"When {cue} — this is the cue."
    if anchor and not behaviour:
        return f"Right after {anchor} — this is the cue."
    return ""


def intention_missing(habit: Optional[HabitMeta], fallback: str = "") -> List[str]:
    """Which halves of the strongest lever are still blank."""
    if habit is None:
        return ["cue", "behaviour", "place"]
    gaps = []
    if not (habit.cue or "").strip():
        gaps.append("cue")
    if not ((habit.behaviour or fallback or "").strip()):
        gaps.append("behaviour")
    if not (habit.place or "").strip():
        gaps.append("place")
    return gaps


# --------------------------------------------------------------------------
# never miss twice
# --------------------------------------------------------------------------


@dataclass
class MissReport:
    """Consecutive misses, and the honest version of a streak."""

    consecutive_misses: int = 0
    alert: bool = False           # two in a row — the point the evidence marks
    periods_checked: int = 0
    hit_periods: int = 0
    current_period_count: int = 0
    target: int = 0
    days_since_first: int = 0
    message: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "consecutive_misses": self.consecutive_misses,
            "alert": self.alert,
            "periods_checked": self.periods_checked,
            "hit_periods": self.hit_periods,
            "current_period_count": self.current_period_count,
            "target": self.target,
            "days_since_first": self.days_since_first,
            "median_days_to_automaticity": MEDIAN_DAYS_TO_AUTOMATICITY,
            "message": self.message,
        }


def misses(habit: Optional[HabitMeta], on: Optional[dt.date] = None, lookback: int = 12) -> MissReport:
    """How many *completed* periods in a row fell short of the target.

    The current period is excluded from the miss count on purpose: it is not
    over, and counting an unfinished week as a failure on Tuesday is the
    streak error wearing different clothes.
    """
    on = on or dt.date.today()
    report = MissReport()
    if habit is None:
        return report

    report.target = max(1, int(habit.target_count or 1))
    dates = sorted(e.on for e in habit.log if e.on)
    if dates:
        report.days_since_first = (on - dates[0]).days

    cadence = habit.cadence or Cadence.WEEKLY
    by_period: Counter = Counter(period_start(d, cadence) for d in dates)
    this_period = period_start(on, cadence)
    report.current_period_count = by_period.get(this_period, 0)

    first = period_start(dates[0], cadence) if dates else this_period
    completed = [p for p in periods_between(first, on, cadence) if p < this_period]
    completed = completed[-lookback:]
    report.periods_checked = len(completed)
    report.hit_periods = sum(1 for p in completed if by_period.get(p, 0) >= report.target)

    run = 0
    for p in reversed(completed):
        if by_period.get(p, 0) >= report.target:
            break
        run += 1
    report.consecutive_misses = run
    report.alert = run >= MISS_ALERT_AT
    report.message = _miss_message(report, cadence)
    return report


def _miss_message(report: MissReport, cadence: Cadence) -> str:
    unit = {Cadence.DAILY: "day", Cadence.WEEKLY: "week", Cadence.MONTHLY: "month"}[cadence]
    if not report.periods_checked:
        return "Just started — nothing to judge yet."
    if report.consecutive_misses == 0:
        return f"On track. {report.hit_periods} of the last {report.periods_checked} {unit}s hit."
    if report.consecutive_misses == 1:
        return (
            f"Missed last {unit}. One miss does not undo anything — the "
            f"rule is never miss twice, so this {unit} is the one that counts."
        )
    return (
        f"Missed {report.consecutive_misses} {unit}s in a row. Two is where it "
        "starts to come apart — shrink the target rather than restart it."
    )


# --------------------------------------------------------------------------
# context stability
# --------------------------------------------------------------------------


@dataclass
class Stability:
    """Same time, same place — the thing that automates a behaviour."""

    n: int = 0
    time_stability: Optional[float] = None    # 0..1
    place_stability: Optional[float] = None   # 0..1
    usual_time: str = ""
    usual_place: str = ""
    enough: bool = False
    message: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "n": self.n,
            "time_stability": self.time_stability,
            "place_stability": self.place_stability,
            "usual_time": self.usual_time,
            "usual_place": self.usual_place,
            "enough": self.enough,
            "message": self.message,
        }


def _minutes(hhmm: str) -> Optional[int]:
    try:
        hour, _, minute = (hhmm or "").partition(":")
        return int(hour) * 60 + int(minute or 0)
    except (TypeError, ValueError):
        return None


def stability(habit: Optional[HabitMeta]) -> Stability:
    """How consistently this happened at the same time and place.

    Time stability is the share of occurrences within `SAME_TIME_MINUTES` of
    the modal hour, not a variance: a person reads "4 of my 6 sessions were
    around 7pm" and cannot read a standard deviation of 83 minutes.
    """
    out = Stability()
    if habit is None or not habit.log:
        out.message = "No occurrences logged yet."
        return out

    events = list(habit.log)
    out.n = len(events)
    out.enough = out.n >= MIN_FOR_STABILITY

    times = [t for t in (_minutes(e.at) for e in events) if t is not None]
    if times:
        modal = Counter(t // 60 for t in times).most_common(1)[0][0]
        centre = modal * 60 + 30
        near = sum(1 for t in times if abs(t - centre) <= SAME_TIME_MINUTES)
        out.time_stability = round(near / len(times), 3)
        out.usual_time = f"{modal:02d}:00"

    places = [(e.place or "").strip().lower() for e in events if (e.place or "").strip()]
    if places:
        common, count = Counter(places).most_common(1)[0]
        out.place_stability = round(count / len(places), 3)
        out.usual_place = common

    out.message = _stability_message(out)
    return out


def _stability_message(out: Stability) -> str:
    if not out.enough:
        return f"{out.n} logged — context needs a few more before it means anything."
    parts = []
    if out.time_stability is not None:
        parts.append(f"{round(out.time_stability * 100)}% around {out.usual_time}")
    if out.place_stability is not None:
        parts.append(f"{round(out.place_stability * 100)}% at {out.usual_place}")
    if not parts:
        return "Occurrences carry no time or place, so context cannot be read."
    weakest = min([v for v in (out.time_stability, out.place_stability) if v is not None])
    tail = (
        " — same time, same place is what automates it."
        if weakest < 0.6
        else " — stable, which is the point."
    )
    return " · ".join(parts) + tail


# --------------------------------------------------------------------------
# the whole picture, for the check-in
# --------------------------------------------------------------------------


def report(habit: Optional[HabitMeta], title: str = "", on: Optional[dt.date] = None) -> Dict[str, Any]:
    """Everything the weekly check-in should ask about, in one payload."""
    miss = misses(habit, on=on)
    stab = stability(habit)
    gaps = intention_missing(habit, fallback=title)
    return {
        "intention": intention_sentence(habit, fallback=title),
        "intention_missing": gaps,
        "anchor": (habit.anchor if habit else "") or "",
        "easier": (habit.easier if habit else "") or "",
        "harder": (habit.harder if habit else "") or "",
        "misses": miss.as_dict(),
        "stability": stab.as_dict(),
        # Ordered by effect size, so the prompt asks the strongest question
        # first rather than the easiest one.
        "suggestions": _suggestions(gaps, habit, miss, stab),
    }


def _suggestions(
    gaps: Sequence[str], habit: Optional[HabitMeta], miss: MissReport, stab: Stability
) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if gaps:
        out.append({
            "field": "intention",
            "ask": "When, exactly, and where?",
            "why": "Gollwitzer 1999 — an if-then plan is d≈0.65, the largest single effect there is.",
        })
    if habit is not None and not (habit.anchor or "").strip():
        out.append({
            "field": "anchor",
            "ask": "What already-daily thing does this come straight after?",
            "why": "Fogg, B=MAP — an existing routine is the only prompt that never needs remembering.",
        })
    if habit is not None and not (habit.easier or "").strip():
        out.append({
            "field": "easier",
            "ask": "What one thing would make starting easier?",
            "why": "Wood — friction beats motivation, and it is the half you control.",
        })
    if miss.alert:
        out.append({
            "field": "target_count",
            "ask": f"Two {'periods'} missed. Shrink the target instead of restarting?",
            "why": "Lally et al. — one miss is noise, two in a row is the point to change the plan.",
        })
    if stab.enough and (stab.time_stability or 1) < 0.6:
        out.append({
            "field": "schedule",
            "ask": "Pin this to one time instead of whenever it fits?",
            "why": "Wood — context stability, not frequency, is what turns it automatic.",
        })
    return out
