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
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import calibration, fit, fsrs
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
    reason: str  # "due" | "new" | "overconfident"

    @property
    def key(self) -> str:
        return f"{self.deck.note_id}/{self.card.id}"

    @property
    def overdue_days(self) -> int:
        if not self.card.due:
            return 0
        return max(0, (dt.date.today() - self.card.due).days)


def normalize_folder(value: str) -> str:
    """One spelling for a vault folder, whoever typed it.

    Obsidian shows backslashes on Windows, the API carries whatever the UI
    sent, and a person typing a filter adds a trailing slash as often as not.
    All of it collapses to "30-Resources/Statics".
    """
    return (value or "").replace("\\", "/").strip("/").strip()


def in_folders(folder: str, scopes: Sequence[str]) -> bool:
    """Is `folder` one of `scopes`, or inside one?

    Picking a folder means picking its subject, and a subject with sub-topics
    is still that subject: 30-Resources selects 30-Resources/Statics/Ch4 too.
    The empty scope is the vault root and therefore matches everything, which
    is also what "no folders given" means.
    """
    if not scopes:
        return True
    folder = normalize_folder(folder)
    for scope in scopes:
        scope = normalize_folder(scope)
        if not scope or folder == scope or folder.startswith(scope + "/"):
            return True
    return False


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
    folders: Optional[Sequence[str]] = None,
    folder_of: Optional[Mapping[str, str]] = None,
    limit: Optional[int] = None,
    on: Optional[dt.date] = None,
    reviewed_today: int = 0,
    introduced_today: int = 0,
    seed: Optional[int] = None,
    priority: Optional[Sequence[str]] = None,
) -> Session:
    """Pick and order this session's cards.

    Due cards come first in priority — the whole point of a scheduler is that
    a card due today is the one worth your minute — but "first in priority" is
    not "first in the list", because a session that front-loads every overdue
    card and trails off into new material is a session you abandon halfway.
    Due and new are interleaved together, spread across subjects.

    `subjects` and `folders` both narrow the pool and stack: pass a folder to
    revise one course, a subject to revise one note, or both. `folder_of` maps
    a deck's note id to the folder that note lives in — see
    `Vault.folders_by_id`.
    """
    on = on or dt.date.today()
    study = cfg.study
    wanted = set(subjects or [])
    scopes = [normalize_folder(f) for f in (folders or [])]
    where = folder_of or {}
    pool = [
        d for d in decks
        if (not wanted or d.note_id in wanted)
        and in_folders(where.get(d.note_id, ""), scopes)
    ]

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
    # gains the most from being seen. Ahead of even that sit the cards lj was
    # *sure* about and got wrong: not the hardest cards, the ones they do not
    # yet know are hard. Koriat & Bjork's illusion of competence is invisible
    # to the scheduler, because FSRS only ever sees the grade.
    hot = set(priority or ())
    for q in due_all:
        if q.key in hot:
            q.reason = "overconfident"
    due_all.sort(key=lambda q: (q.key not in hot, q.card.due or on, q.deck.subject))
    rng = random.Random(seed if seed is not None else _daily_seed(on))
    rng.shuffle(new_all)

    picked = due_all[:due_budget] + new_all[:new_budget]
    cap = limit or study.session_size
    session.capped = len(picked) > cap

    ordered = _interleave(picked, rng) if study.interleave else picked
    session.queue = ordered[:cap]

    session.message = _session_message(session, due_budget, new_budget)
    return session


def _lane(item: QueuedCard) -> str:
    """What this card is interleaved *against*.

    A deck alone was the easy half. Rohrer & Taylor (2007, 2015) measured
    interleaving on **problem types within a subject** — students who
    practised mixed problem kinds outperformed blocked practice by a wide
    margin on a delayed test, and the mechanism they identified is
    discrimination: blocked practice never requires you to work out *which*
    method applies, because the last problem already told you. Mixing decks
    gives you that across subjects and not at all inside one, which is where
    the effect was actually found.

    A card with no `Type` label falls back to its deck, so a deck that has
    never been labelled behaves exactly as it did before.
    """
    topic = (item.card.topic or "").strip().lower()
    return f"{item.deck.note_id}::{topic}" if topic else item.deck.note_id


def _interleave(items: List[QueuedCard], rng: random.Random) -> List[QueuedCard]:
    """Spread cards so consecutive ones rarely share a deck *or a problem type*.

    Each lane's queue is laid out on the unit interval — a lane with four
    cards puts them at .125, .375, .625, .875 — and everything is then sorted
    by position. Proportional by construction: a lane with twenty cards due
    and a lane with two both stay evenly distributed across the whole session
    instead of the small one being over in the first minute.
    """
    by_lane: Dict[str, List[QueuedCard]] = {}
    for item in items:
        by_lane.setdefault(_lane(item), []).append(item)

    spread: List[Tuple[float, int, QueuedCard]] = []
    for order, (_, queue) in enumerate(sorted(by_lane.items())):
        n = len(queue)
        for i, item in enumerate(queue):
            # jitter breaks ties between lanes of equal size without letting
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
    due = sum(1 for q in session.queue if q.reason in ("due", "overconfident"))
    new = len(session.queue) - due
    if due:
        bits.append(f"{due} due")
    hot = sum(1 for q in session.queue if q.reason == "overconfident")
    if hot:
        bits.append(f"{hot} you were sure about")
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
    confidence: Optional[float] = None
    #: Set when lj predicted >= calibration.OVERCONFIDENT_AT and then missed.
    overconfident: bool = False


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
    confidence: Optional[float] = None,
) -> AnswerResult:
    """Apply one answer: schedule it, persist it, log it.

    `confidence` is what lj predicted *before* the answer was revealed. It
    rides on the same log line as the grade rather than in a store of its own,
    so the two can never disagree about which review they describe, and every
    line written before this existed is simply a review with no prediction on
    it. See sb/calibration.py.

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
            "confidence": confidence,
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
        confidence=confidence,
        overconfident=(
            confidence is not None
            and confidence >= calibration.OVERCONFIDENT_AT
            and not calibration.is_correct(grade)
        ),
    )


def weights(cfg: Config):
    """Personal FSRS weights if lj has ever fitted them, else the defaults.

    Three sources, in order of how deliberate they are: `config.yaml` beats a
    fitted file, because a number lj typed is a decision and a number a fit
    produced is a suggestion. A wrong-length list from either is ignored
    rather than crashing a session mid-review. See sb/fit.py.
    """
    custom = list(cfg.study.weights or [])
    if len(custom) == len(fsrs.DEFAULT_W):
        return custom
    if cfg.study.use_fitted_weights:
        fitted = fit.load(cfg.deck_dir)
        if fitted:
            return fitted
    return fsrs.DEFAULT_W


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


# --------------------------------------------------------------------------
# self-explanation — the generation effect, prompted
# --------------------------------------------------------------------------

SELF_EXPLAIN_SYSTEM = """You are marking a student's own explanation of why an \
answer is what it is. You are given the flashcard, the reference answer, the \
sentence from their notes it came from, and what they said.

Rules:
- Output JSON only.
- Mark the *reasoning*, not the wording, and not whether they recalled the \
answer — they have already been told the answer.
- "sound" means their account would let them derive the answer again. \
"partial" means it is right as far as it goes but leaves out something \
load-bearing. "off" means it would lead them somewhere wrong.
- `missing` names the one thing most worth adding. One clause. Empty if sound.
- `followup` is two sentences at most, addressed to them, filling exactly that \
gap. Do not restate their explanation back to them.
- An empty or nonsense explanation is "off" with score 0."""

SELF_EXPLAIN_SCHEMA = """{"score": 0.0, "verdict": "sound|partial|off", \
"missing": "", "followup": ""}"""


@dataclass
class SelfExplanation:
    """What lj said, and what the model made of it. Nothing is scheduled."""

    said: str = ""
    score: float = 0.0
    verdict: str = "off"          # sound | partial | off
    missing: str = ""
    followup: str = ""
    graded_by: str = "model"      # model | rule

    def as_dict(self) -> Dict[str, Any]:
        return {
            "said": self.said,
            "score": round(float(self.score), 3),
            "verdict": self.verdict,
            "missing": self.missing,
            "followup": self.followup,
            "graded_by": self.graded_by,
        }


#: Grades that earn the prompt. Again and Hard are where the explanation is
#: worth having; a card graded Good or Easy does not need interrupting.
SELF_EXPLAIN_GRADES = (1, 2)


def wants_self_explanation(grade: int) -> bool:
    try:
        return int(grade) in SELF_EXPLAIN_GRADES
    except (TypeError, ValueError):
        return False


def mark_self_explanation(
    card: Card, note_body: str, said: str, cfg: Config
) -> SelfExplanation:
    """Mark "say why in one line", written before the explanation is shown.

    The direction is the point. `explain()` has the model explain *to* lj;
    this has lj explain first. Slamecka & Graf (1978) is the generation
    effect — material you produce is remembered better than the same material
    read — and Chi et al. (1989, 1994) is the specific finding that
    self-explanation while studying predicts transfer, and that **prompted**
    self-explanation beats waiting for it to happen spontaneously. Almost
    nobody does it spontaneously; the prompt is the intervention.

    Marked the same way a typed recall is marked, and for the same reason:
    the model proposes and lj disposes. Nothing here touches the schedule, the
    deck or the review log, so a model that misreads a good explanation costs
    a sentence of bad advice rather than a card.
    """
    said = (said or "").strip()
    if not said:
        return SelfExplanation(
            said="", score=0.0, verdict="off",
            missing="nothing was written", followup="", graded_by="rule",
        )

    reference = card.answer()
    provider = resolve_provider(cfg.llm, "grade")
    if not getattr(provider, "is_llm", False):
        return _overlap_self_explanation(said, reference, card.source or note_body)

    prompt = (
        f"Card: {card.question()}\n"
        f"Reference answer: {reference}\n"
        f"From their notes: {(card.source or '')[:400]}\n"
        f"They said: {said}\n\n"
        "Mark their reasoning. Return JSON."
    )
    try:
        raw = provider.complete_json(
            prompt, system=SELF_EXPLAIN_SYSTEM, schema_hint=SELF_EXPLAIN_SCHEMA
        )
    except Exception:
        return _overlap_self_explanation(said, reference, card.source or note_body)
    if not isinstance(raw, dict):
        return _overlap_self_explanation(said, reference, card.source or note_body)

    verdict = str(raw.get("verdict") or "").strip().lower()
    if verdict not in ("sound", "partial", "off"):
        verdict = "partial"
    try:
        score = max(0.0, min(1.0, float(raw.get("score", 0.0))))
    except (TypeError, ValueError):
        score = {"sound": 0.9, "partial": 0.55, "off": 0.1}[verdict]
    return SelfExplanation(
        said=said,
        score=score,
        verdict=verdict,
        missing=str(raw.get("missing") or "").strip(),
        followup=str(raw.get("followup") or "").strip(),
        graded_by="model",
    )


def _overlap_self_explanation(said: str, reference: str, source: str) -> SelfExplanation:
    """No model: score on how much of the reference the explanation touches.

    Blunt, and it says so. It exists so that closing Ollama turns the prompt
    into a private note-to-self rather than an error — writing the explanation
    is where most of the effect lives, and that half needs no model at all.
    """
    wanted = _content_words(reference) | _content_words(source[:300])
    got = _content_words(said)
    hit = len(wanted & got) / len(wanted) if wanted else 0.0
    verdict = "sound" if hit >= 0.5 else "partial" if hit >= 0.2 else "off"
    return SelfExplanation(
        said=said,
        score=round(hit, 3),
        verdict=verdict,
        missing="" if verdict == "sound" else "no model running — this is a word overlap, not a judgement",
        followup="",
        graded_by="rule",
    )


# --------------------------------------------------------------------------
# worked examples, and getting rid of them
# --------------------------------------------------------------------------


def worked_example_for(card: Card, deck: Deck, cfg: Config) -> Dict[str, Any]:
    """Should this card show its worked example, and how much of it?

    Sweller's worked-example effect: for material a learner cannot yet do,
    studying a full solution beats attempting the problem, because attempting
    it spends all available working memory on search rather than on learning
    the schema. A bare recall card handed to someone on their first encounter
    with a topic is exactly that wasted search.

    And the reason this fades rather than staying on: the **expertise-reversal
    effect**, also Sweller's. The same worked example that helped a novice
    *hurts* someone competent, because they now have to reconcile the guidance
    with the solution they were already producing. A scaffold that never comes
    down is not a scaffold.

    So: shown in full on the first attempt, shown as an opening fragment while
    the card is still being learned, and withdrawn entirely once either the
    card has been answered enough times or the deck as a whole is mature.
    Nothing is shown if lj never wrote one, which is the ordinary case.
    """
    text = (card.worked or "").strip()
    if not text:
        return {"show": "none", "text": ""}

    study = cfg.study
    mature = [c for c in deck.cards if c.status == "active" and _is_mature(c, cfg)]
    active = [c for c in deck.cards if c.status == "active"]
    competence = len(mature) / len(active) if active else 0.0
    if competence >= study.expertise_reversal_at:
        # Expertise reversal: past this, the example is interference.
        return {"show": "none", "text": "", "reason": "you know this deck"}
    if card.reps == 0:
        return {"show": "full", "text": text}
    if card.reps < study.worked_example_fade_reps:
        return {"show": "partial", "text": _fade(text)}
    return {"show": "none", "text": "", "reason": "faded"}


def _fade(text: str) -> str:
    """The opening of a worked example — enough to start, not enough to copy.

    Sweller's completion-problem format: the scaffold is withdrawn from the
    end backwards, so the learner always performs the final step themselves.
    """
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    keep = max(1, len(sentences) // 2)
    head = " ".join(sentences[:keep]).strip()
    return head + ("  …finish it from here." if keep < len(sentences) else "")


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
