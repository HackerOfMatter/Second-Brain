"""The study session: what to show, how to grade it, and how to report it.

Three things live here, and they are separate on purpose.

**Assembly** decides which cards you see and in what order. The interesting
decision is interleaving. Anki, by default, hands you one deck at a time, and
studying that way feels more productive than it is — blocked practice inflates
your sense of mastery because the context never changes, so you are retrieving
from working memory rather than from storage. Mixing subjects within a session
is harder in the moment and measurably better afterwards. So a session here
pulls from every deck that has something due and spreads them out.

**Grading** takes either a self-assessed Anki button or a typed answer the
model marks. Both end in the same place: a 1-4 grade handed to FSRS. The
typed path exists because self-grading quietly drifts generous — you recognise
the answer, feel that you knew it, and press Good. Having to produce the answer
first, in your own words, is a different and more honest test.

**Reporting** is the Anki half lj asked for: how often you actually study.
Everything comes out of the append-only review log, which means the numbers
survive a deck being edited, a card being deleted, or a note being graduated
to Resource.

Mastery and graduation also live here, because "am I done with this?" is a
question about review history, not about the note. Blueprint §4 wants the human
to confirm the Project → Resource move; this module decides when to *ask*.
"""

from __future__ import annotations

import datetime as dt
import random
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import fsrs
from .cards import Card, Deck, DeckStore
from .config import Config
from .llm import resolve_provider

# --------------------------------------------------------------------------
# session assembly
# --------------------------------------------------------------------------


@dataclass
class QueuedCard:
    deck: Deck
    card: Card
    reason: str  # "due" | "new"

    @property
    def overdue_days(self) -> int:
        if not self.card.due:
            return 0
        return max(0, (dt.date.today() - self.card.due).days)


@dataclass
class Session:
    queue: List[QueuedCard] = field(default_factory=list)
    due_available: int = 0
    new_available: int = 0
    capped: bool = False
    subjects: List[str] = field(default_factory=list)
    message: str = ""


def build_session(
    decks: Sequence[Deck],
    cfg: Config,
    *,
    subjects: Optional[Sequence[str]] = None,
    limit: Optional[int] = None,
    on: Optional[dt.date] = None,
    reviewed_today: int = 0,
    introduced_today: int = 0,
    seed: Optional[int] = None,
) -> Session:
    """Pick and order this session's cards.

    Due cards come first in priority — the whole point of a scheduler is that
    a card due today is the one worth your minute — but "first in priority" is
    not "first in the list", because a session that front-loads every overdue
    card and trails off into new material is a session you abandon halfway.
    Due and new are interleaved together, spread across subjects.
    """
    on = on or dt.date.today()
    study = cfg.study
    wanted = set(subjects or [])
    pool = [d for d in decks if not wanted or d.note_id in wanted]

    due_budget = max(0, study.max_reviews_per_day - max(0, reviewed_today))
    new_budget = max(0, study.new_cards_per_day - max(0, introduced_today))

    due_all: List[QueuedCard] = []
    new_all: List[QueuedCard] = []
    for deck in pool:
        for card in deck.due(on):
            due_all.append(QueuedCard(deck, card, "due"))
        for card in deck.new():
            new_all.append(QueuedCard(deck, card, "new"))

    session = Session(due_available=len(due_all), new_available=len(new_all))
    session.subjects = sorted({d.subject or d.note_id for d in pool})

    # Most overdue first — a card three weeks late has decayed furthest and
    # gains the most from being seen.
    due_all.sort(key=lambda q: (q.card.due or on, q.deck.subject))
    rng = random.Random(seed if seed is not None else _daily_seed(on))
    rng.shuffle(new_all)

    picked = due_all[:due_budget] + new_all[:new_budget]
    cap = limit or study.session_size
    session.capped = len(picked) > cap

    ordered = _interleave(picked, rng) if study.interleave else picked
    session.queue = ordered[:cap]

    session.message = _session_message(session, due_budget, new_budget)
    return session


def _interleave(items: List[QueuedCard], rng: random.Random) -> List[QueuedCard]:
    """Spread cards so consecutive ones rarely share a deck.

    Each deck's queue is laid out on the unit interval — a deck with four
    cards puts them at .125, .375, .625, .875 — and everything is then sorted
    by position. Proportional by construction: a deck with twenty cards due
    and a deck with two both stay evenly distributed across the whole session
    instead of the small one being over in the first minute.
    """
    by_deck: Dict[str, List[QueuedCard]] = {}
    for item in items:
        by_deck.setdefault(item.deck.note_id, []).append(item)

    spread: List[Tuple[float, int, QueuedCard]] = []
    for order, (_, queue) in enumerate(sorted(by_deck.items())):
        n = len(queue)
        for i, item in enumerate(queue):
            # jitter breaks ties between decks of equal size without letting
            # any card drift far from its slot
            position = (i + 0.5) / n + rng.uniform(-0.02, 0.02)
            spread.append((position, order, item))
    spread.sort(key=lambda t: (t[0], t[1]))
    return [item for _, _, item in spread]


def _session_message(session: Session, due_budget: int, new_budget: int) -> str:
    if not session.queue:
        if session.due_available or session.new_available:
            return "Daily cap reached — come back tomorrow, or raise the cap in config.yaml."
        return "Nothing due. Everything you know is still known."
    bits = []
    due = sum(1 for q in session.queue if q.reason == "due")
    new = len(session.queue) - due
    if due:
        bits.append(f"{due} due")
    if new:
        bits.append(f"{new} new")
    subjects = len({q.deck.note_id for q in session.queue})
    tail = f" across {subjects} subjects" if subjects > 1 else ""
    return " · ".join(bits) + tail


def _daily_seed(on: dt.date) -> int:
    """Shuffles that are stable within a day, so reloading the page does not
    reshuffle a session you are halfway through."""
    return on.toordinal()


# --------------------------------------------------------------------------
# answering
# --------------------------------------------------------------------------


@dataclass
class AnswerResult:
    card: Card
    grade: int
    interval_days: int
    due: dt.date
    retrievability_before: float
    again: bool
    intervals: Dict[str, int]
    feedback: str = ""
    score: Optional[float] = None
    correct: Optional[bool] = None


def answer(
    store: DeckStore,
    deck: Deck,
    card: Card,
    grade: int,
    cfg: Config,
    *,
    mode: str = "self",
    typed: str = "",
    seconds: float = 0.0,
    feedback: str = "",
    score: Optional[float] = None,
) -> AnswerResult:
    """Apply one answer: schedule it, persist it, log it.

    The log entry records the state *before* the review as well as the grade.
    That is what makes the history re-analysable later — you can recompute what
    the scheduler would have done differently, or fit personal FSRS weights,
    without having preserved anything else.
    """
    study = cfg.study
    before = {
        "s": round(card.stability, 4),
        "d": round(card.difficulty, 4),
        "reps": card.reps,
        "lapses": card.lapses,
        "elapsed_days": round(card.elapsed_days(), 3),
    }
    scheduled = fsrs.review(
        card.memory,
        grade,
        last_review=card.last_review,
        desired_retention=study.desired_retention,
        maximum_interval=study.maximum_interval_days,
        w=weights(cfg),
    )
    card.apply(scheduled)
    if card.status == "draft":
        card.status = "active"
    store.save(deck)
    store.log_review(
        {
            "at": card.last_review.isoformat() if card.last_review else None,
            "note_id": deck.note_id,
            "subject": deck.subject,
            "card": card.id,
            "grade": int(grade),
            "mode": mode,
            "seconds": round(float(seconds or 0), 1),
            "typed": (typed or "")[:500],
            "score": score,
            "before": before,
            "after": {
                "s": round(card.stability, 4),
                "d": round(card.difficulty, 4),
                "interval": scheduled.interval_days,
                "due": card.due.isoformat() if card.due else None,
            },
            "r_before": round(scheduled.retrievability_before, 4),
        }
    )
    return AnswerResult(
        card=card,
        grade=int(grade),
        interval_days=scheduled.interval_days,
        due=scheduled.due,
        retrievability_before=scheduled.retrievability_before,
        again=scheduled.again,
        intervals=fsrs.preview_intervals(
            card.memory,
            last_review=card.last_review,
            desired_retention=study.desired_retention,
            maximum_interval=study.maximum_interval_days,
            w=weights(cfg),
        ),
        feedback=feedback,
        score=score,
        correct=None if score is None else score >= 0.6,
    )


def weights(cfg: Config):
    """Personal FSRS weights if lj has ever fitted them, else the published
    defaults. A wrong-length list is ignored rather than crashing a session."""
    custom = list(cfg.study.weights or [])
    return custom if len(custom) == len(fsrs.DEFAULT_W) else fsrs.DEFAULT_W


def button_intervals(card: Card, cfg: Config) -> Dict[str, int]:
    """What each button would schedule, for the buttons themselves."""
    return fsrs.preview_intervals(
        card.memory,
        last_review=card.last_review,
        desired_retention=cfg.study.desired_retention,
        maximum_interval=cfg.study.maximum_interval_days,
        w=weights(cfg),
    )


# --------------------------------------------------------------------------
# free recall — the model as marker
# --------------------------------------------------------------------------

GRADER_SYSTEM = """You mark a student's recall of a flashcard. You are given \
the reference answer and what they wrote.

Rules:
- Output JSON only.
- Mark meaning, not wording. A correct answer in different words is correct.
- Mark only against the reference answer. Do not add requirements it does not \
contain, and do not penalise extra correct detail.
- An answer that is right but incomplete scores in the middle, and "missed" \
names what is absent.
- An empty or off-topic answer scores 0.
- "feedback" is one short sentence addressed to the student. Say what was \
missing or wrong. If they were right, say so and stop — do not pad."""

GRADER_SCHEMA = """{"score": 0.0, "missed": "", "feedback": ""}"""


@dataclass
class Grading:
    score: float
    grade: int
    feedback: str
    missed: str = ""
    graded_by: str = "model"


def grade_recall(question: str, reference: str, typed: str, cfg: Config) -> Grading:
    """Mark a typed answer and turn the score into an FSRS grade.

    The score→grade mapping is deliberately harsher than self-grading at the
    top end: producing the answer from scratch and getting it merely mostly
    right is a Good, not an Easy. Easy is reserved for a complete answer,
    because Easy triples the interval and a half-remembered card should not
    disappear for a season.
    """
    typed = (typed or "").strip()
    if not typed:
        return Grading(0.0, fsrs.AGAIN, "Nothing entered.", graded_by="rule")

    provider = resolve_provider(cfg.llm, "grade")
    if not getattr(provider, "is_llm", False):
        return _overlap_grading(reference, typed)

    try:
        raw = provider.complete_json(
            f"Question:\n{question}\n\nReference answer:\n{reference}\n\n"
            f"Student wrote:\n{typed}\n\nMark it.",
            system=GRADER_SYSTEM,
            schema_hint=GRADER_SCHEMA,
        )
    except Exception:
        return _overlap_grading(reference, typed)

    try:
        score = float(raw.get("score", 0))
    except (TypeError, ValueError):
        score = 0.0
    score = max(0.0, min(1.0, score))
    return Grading(
        score=score,
        grade=score_to_grade(score),
        feedback=str(raw.get("feedback") or "").strip()[:400],
        missed=str(raw.get("missed") or "").strip()[:300],
    )


def score_to_grade(score: float) -> int:
    if score >= 0.95:
        return fsrs.EASY
    if score >= 0.7:
        return fsrs.GOOD
    if score >= 0.45:
        return fsrs.HARD
    return fsrs.AGAIN


def _overlap_grading(reference: str, typed: str) -> Grading:
    """The no-model marker: content-word overlap.

    Crude, and it says so. It will not recognise a right answer phrased
    entirely differently, so it never awards Easy — the worst it can do is
    make you re-see a card you actually knew, which costs a minute. Marking a
    wrong answer correct would cost you the fact.
    """
    ref_words = _content_words(reference)
    got_words = _content_words(typed)
    if not ref_words:
        return Grading(0.5, fsrs.HARD, "No reference answer to mark against.", graded_by="rule")
    hit = len(ref_words & got_words) / len(ref_words)
    grade = fsrs.GOOD if hit >= 0.7 else fsrs.HARD if hit >= 0.4 else fsrs.AGAIN
    return Grading(
        score=round(hit, 2),
        grade=grade,
        feedback=(
            f"Marked offline by word overlap ({int(hit * 100)}% of the key words). "
            "Start Ollama for a real marker."
        ),
        missed=", ".join(sorted(ref_words - got_words)[:8]),
        graded_by="rule",
    )


STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "is", "are", "it", "that",
    "this", "for", "on", "with", "as", "by", "be", "was", "were", "at", "from",
    "its", "into", "than", "then", "so", "if", "not", "no", "you", "your",
}


def _content_words(text: str) -> set:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) > 2 and w not in STOPWORDS}


# --------------------------------------------------------------------------
# the tutor's explain button
# --------------------------------------------------------------------------

EXPLAIN_SYSTEM = """You are a tutor helping someone with their own notes. \
You are given a flashcard and the note it came from.

Rules:
- Answer from the note. If the note does not cover it, say so in one sentence, \
then give the general answer clearly labelled as outside their notes.
- Be brief: a short paragraph, or a few lines. No headings, no bullet lists \
unless you are genuinely enumerating.
- Explain the idea, do not restate the flashcard.
- Plain language. Define a term the first time you use it."""


def explain(card: Card, note_body: str, question: str, cfg: Config) -> str:
    """Answer "but why?" without leaving the review screen.

    Scoped to the card's own source note rather than the whole vault. That is
    the smaller half of the RAG tutor the blueprint asks for (§7), and it is
    the half that works with no index at all — the retrieval step is trivial
    when you already know which note the card came from.
    """
    provider = resolve_provider(cfg.llm, "explain")
    if not getattr(provider, "is_llm", False):
        return (
            "No model is running, so there is nothing to ask. "
            "Start Ollama and try again — the source note is below.\n\n"
            + (card.source or note_body[:600])
        )
    prompt = (
        f"Flashcard:\nQ: {card.front}\nA: {card.back}\n\n"
        f"The note it came from:\n\"\"\"\n{note_body.strip()[:6000]}\n\"\"\"\n\n"
        f"{question.strip() or 'Explain this so I actually understand it.'}"
    )
    try:
        return provider.complete_text(prompt, system=EXPLAIN_SYSTEM).strip()
    except Exception as exc:
        return f"Could not reach the model ({type(exc).__name__})."


# --------------------------------------------------------------------------
# progress
# --------------------------------------------------------------------------


def deck_progress(deck: Deck, cfg: Config, on: Optional[dt.date] = None) -> Dict[str, Any]:
    """One deck's numbers, including the mastery the graduation prompt reads."""
    on = on or dt.date.today()
    active = deck.active
    mature = [c for c in active if _is_mature(c, cfg)]
    studied = [c for c in active if not c.is_new]
    retention = (
        sum(c.retrievability() for c in studied) / len(studied) if studied else 0.0
    )
    return {
        "note_id": deck.note_id,
        "subject": deck.subject,
        "category": deck.category,
        "bucket": deck.bucket,
        "cards": len(deck.cards),
        "active": len(active),
        "drafts": len(deck.drafts),
        "suspended": sum(1 for c in deck.cards if c.status == "suspended"),
        "new": len(deck.new()),
        "due": len(deck.due(on)),
        "mature": len(mature),
        "mastery": round(len(mature) / len(active), 3) if active else 0.0,
        "retention": round(retention, 3),
        "next_due": min((c.due for c in active if c.due), default=None),
        "ready_to_graduate": is_ready_to_graduate(deck, cfg),
    }


def _is_mature(card: Card, cfg: Config) -> bool:
    """A card is mature when it has survived enough spaced attempts *and* the
    model thinks it will still be there in a few weeks. Both halves matter:
    reps alone can be four answers in one evening, and stability alone can be
    one lucky Easy."""
    return (
        card.reps >= cfg.review.graduation_min_reps
        and card.stability >= cfg.study.mature_stability_days
    )


def is_ready_to_graduate(deck: Deck, cfg: Config) -> bool:
    """Blueprint §4: when to *ask* whether this Project has become a Resource.

    Three gates, because any one of them alone gives a false positive. A deck
    of two cards can hit 100% mastery in a week and mean nothing; a deck where
    most cards are mature but three are still lapsing weekly is not learned.
    """
    active = deck.active
    if len(active) < cfg.study.min_cards_to_graduate:
        return False
    mature = sum(1 for c in active if _is_mature(c, cfg))
    return (mature / len(active)) >= cfg.review.graduation_mastery_threshold


def stats(store: DeckStore, decks: Sequence[Deck], cfg: Config, days: int = 365) -> Dict[str, Any]:
    """The Anki-style progress view: how often you study, and how it is going."""
    today = dt.date.today()
    since = today - dt.timedelta(days=days)
    per_day: Dict[str, int] = {}
    grades: List[int] = []
    minutes = 0.0
    recent_correct: List[bool] = []
    thirty = today - dt.timedelta(days=30)

    for rec in store.reviews(since=since):
        at = rec.get("at")
        day = str(at)[:10] if at else ""
        if not day:
            continue
        per_day[day] = per_day.get(day, 0) + 1
        grade = int(rec.get("grade") or 0)
        grades.append(grade)
        minutes += float(rec.get("seconds") or 0) / 60.0
        try:
            when = dt.date.fromisoformat(day)
        except ValueError:
            continue
        if when >= thirty and grade:
            recent_correct.append(grade >= fsrs.HARD)

    active = [c for d in decks for c in d.active]
    studied = [c for c in active if not c.is_new]
    forecast = _forecast(decks, today, 14)

    return {
        "today": per_day.get(today.isoformat(), 0),
        "streak": _streak(per_day, today),
        "longest_streak": _longest_streak(per_day),
        "days_studied": len(per_day),
        "reviews_total": sum(per_day.values()),
        "minutes_total": round(minutes),
        "per_day": per_day,
        "heatmap": _heatmap(per_day, today, 182),
        "accuracy_30d": (
            round(sum(recent_correct) / len(recent_correct), 3) if recent_correct else None
        ),
        "cards": {
            "total": sum(len(d.cards) for d in decks),
            "active": len(active),
            "new": sum(1 for c in active if c.is_new),
            "mature": sum(1 for c in active if _is_mature(c, cfg)),
            "drafts": sum(len(d.drafts) for d in decks),
            "due_today": sum(len(d.due(today)) for d in decks),
        },
        "predicted_retention": (
            round(sum(c.retrievability() for c in studied) / len(studied), 3)
            if studied
            else None
        ),
        "forecast": forecast,
        "grade_mix": {
            fsrs.GRADE_NAMES[g]: sum(1 for x in grades if x == g) for g in fsrs.GRADES
        },
    }


def _forecast(decks: Sequence[Deck], today: dt.date, days: int) -> List[Dict[str, Any]]:
    counts = {(today + dt.timedelta(days=i)).isoformat(): 0 for i in range(days)}
    horizon = today + dt.timedelta(days=days - 1)
    for deck in decks:
        for card in deck.active:
            if not card.due:
                continue
            when = max(card.due, today)  # everything overdue lands today
            if when <= horizon:
                counts[when.isoformat()] += 1
    return [{"date": k, "count": v} for k, v in sorted(counts.items())]


def _streak(per_day: Dict[str, int], today: dt.date) -> int:
    """Days in a row ending today.

    Today not being studied yet does not break the streak — it is 9am, the
    day is not over. Yesterday being empty does.
    """
    day = today if per_day.get(today.isoformat()) else today - dt.timedelta(days=1)
    count = 0
    while per_day.get(day.isoformat()):
        count += 1
        day -= dt.timedelta(days=1)
    return count


def _longest_streak(per_day: Dict[str, int]) -> int:
    days = sorted(dt.date.fromisoformat(d) for d in per_day if per_day[d])
    best = run = 0
    previous: Optional[dt.date] = None
    for day in days:
        run = run + 1 if previous and (day - previous).days == 1 else 1
        best = max(best, run)
        previous = day
    return best


def _heatmap(per_day: Dict[str, int], today: dt.date, days: int) -> List[Dict[str, Any]]:
    start = today - dt.timedelta(days=days - 1)
    return [
        {
            "date": (start + dt.timedelta(days=i)).isoformat(),
            "count": per_day.get((start + dt.timedelta(days=i)).isoformat(), 0),
        }
        for i in range(days)
    ]


def counted_today(store: DeckStore, on: Optional[dt.date] = None) -> Tuple[int, int]:
    """(reviews done today, new cards introduced today) — the daily caps."""
    on = on or dt.date.today()
    reviews = introduced = 0
    for rec in store.reviews(since=on):
        if str(rec.get("at"))[:10] != on.isoformat():
            continue
        reviews += 1
        before = rec.get("before") or {}
        if not before.get("reps"):
            introduced += 1
    return reviews, introduced
