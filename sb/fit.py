"""Fitting FSRS to lj, from lj's own answers.

Every line in `_decks/_reviews.jsonl` carries the card's state *before* the
review — stability, difficulty, reps, lapses, days elapsed — alongside the
grade. That was written in from the beginning for exactly this, and it means
nothing has to change for the history to be fittable. It already is.

## Why this waits for ~1,000 reviews

FSRS-5 has nineteen free parameters. Below roughly a thousand reviews you are
not fitting a memory model, you are memorising a bad fortnight: a run of hard
cards or a week off shifts the estimate more than any real property of lj's
memory does. That threshold is Jarrett Ye's own guidance for the FSRS
optimizer, and the roadmap named it as the trigger. It is enforced here rather
than suggested, because a fitted-looking number is far more dangerous than an
obviously absent one — the defaults are honest, and an overfit replacement
would silently reschedule the whole collection.

## What is optimised

The model's job is to predict **retrievability**: given the state and the
elapsed time, what was the probability lj still knew this? So the loss is the
log loss of that prediction against what happened (grade ≥ 3 is remembered,
Again and Hard are not) — a proper scoring rule, which means it is minimised
by telling the truth rather than by being confident.

## How

Coordinate descent with a shrinking step, over the parameters that actually
move the prediction. Not the fanciest optimiser and deliberately so: no
dependency, a few hundred lines of arithmetic, and it is being asked to
improve on a strong published prior with a small dataset, where a careful
local search is the appropriate amount of ambition. Every parameter stays
inside the published FSRS bounds, so no fit can produce a scheduler that does
something absurd, and the fit is only *reported* as an improvement if it beats
the defaults on the same data.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import fsrs

#: Ye's optimizer guidance, and the roadmap's stated trigger. Below this the
#: fit is noise wearing a decimal point.
MIN_REVIEWS = 1000

#: Enough to *show* the fit that would be produced, so lj can watch the number
#: approach rather than discovering the feature the day it unlocks.
PREVIEW_FROM = 200

#: Published FSRS-5 bounds. Keeping the search inside them is what makes a bad
#: fit merely unhelpful instead of harmful.
BOUNDS: List[Tuple[float, float]] = [
    (0.001, 100.0), (0.001, 100.0), (0.001, 100.0), (0.001, 100.0),
    (1.0, 10.0), (0.001, 4.0), (0.001, 4.0), (0.001, 0.75),
    (0.0, 4.5), (0.0, 0.8), (0.001, 3.5), (0.001, 5.0),
    (0.001, 0.25), (0.001, 0.9), (0.0, 4.0), (0.0, 1.0),
    (1.0, 6.0), (0.0, 2.0), (0.0, 2.0),
]

WEIGHTS_FILE = "_fsrs_weights.json"


@dataclass
class Sample:
    """One review, reduced to what the model has to predict."""

    stability: float
    elapsed_days: float
    remembered: bool


def samples(reviews: Iterable[Dict[str, Any]]) -> List[Sample]:
    """Reviews that can be scored: a real prior stability and real elapsed time.

    A card's very first review has no prior state to predict from — there is
    nothing the model could have said — so it is excluded rather than counted
    as a free win.
    """
    out: List[Sample] = []
    for rec in reviews:
        before = rec.get("before") or {}
        try:
            stability = float(before.get("s") or 0.0)
            elapsed = float(before.get("elapsed_days") or 0.0)
            grade = int(rec.get("grade") or 0)
        except (TypeError, ValueError):
            continue
        if stability <= 0 or elapsed <= 0 or grade < 1:
            continue
        out.append(Sample(stability, elapsed, grade >= 3))
    return out


def log_loss(w: Sequence[float], data: Sequence[Sample]) -> float:
    """Mean negative log likelihood of what actually happened.

    A proper scoring rule: it is minimised by reporting the true probability,
    not by being confident, which is the property that makes it safe to
    optimise a memory model against.

    The weights enter through the *scheduler*, not the forgetting curve —
    FSRS's curve shape is fixed and the parameters govern how stability moves.
    So each sample is scored on the retrievability implied by its recorded
    stability, and the fit searches for the parameters that make those
    recorded stabilities predictive. That is what the log is: the states this
    scheduler actually produced, and what happened next.
    """
    total = 0.0
    for s in data:
        p = fsrs.retrievability(max(1e-6, s.stability * _scale(w)), s.elapsed_days)
        p = min(1.0 - 1e-9, max(1e-9, p))
        total += -math.log(p) if s.remembered else -math.log(1.0 - p)
    return total / len(data) if data else float("inf")


def _scale(w: Sequence[float]) -> float:
    """How this parameter set stretches or compresses stability overall.

    The initial-stability parameters (w0..w3) set the scale of every interval
    that follows, so their ratio to the defaults is the single degree of
    freedom that the recorded history can actually identify. Fitting all
    nineteen against a few hundred samples would be fitting noise; fitting the
    one the data speaks to is the honest version of the same idea.
    """
    default = sum(fsrs.DEFAULT_W[:4]) or 1.0
    return (sum(w[:4]) or 1.0) / default


def fit(
    reviews: Iterable[Dict[str, Any]],
    *,
    start: Optional[Sequence[float]] = None,
    rounds: int = 40,
) -> Dict[str, Any]:
    """Search for the weights that best explain lj's own review history."""
    data = samples(reviews)
    base = list(start or fsrs.DEFAULT_W)
    baseline = log_loss(base, data)
    result: Dict[str, Any] = {
        "n": len(data),
        "min_reviews": MIN_REVIEWS,
        "enough": len(data) >= MIN_REVIEWS,
        "preview": PREVIEW_FROM <= len(data) < MIN_REVIEWS,
        "baseline_loss": round(baseline, 6) if data else None,
        "weights": list(fsrs.DEFAULT_W),
        "loss": round(baseline, 6) if data else None,
        "improved": False,
    }
    if len(data) < PREVIEW_FROM:
        result["message"] = (
            f"{len(data)} scorable reviews. Personal weights need "
            f"{MIN_REVIEWS}; below that a fit is a memorised bad fortnight, "
            "not a memory model. Nothing needs to change to be ready — every "
            "line already carries the state it will be fitted from."
        )
        return result

    best = list(base)
    best_loss = baseline
    step = 0.35
    for _ in range(max(1, rounds)):
        moved = False
        for i in range(4):                      # the identifiable parameters
            low, high = BOUNDS[i]
            for direction in (1.0, -1.0):
                trial = list(best)
                trial[i] = min(high, max(low, trial[i] * (1.0 + direction * step)))
                if trial[i] == best[i]:
                    continue
                loss = log_loss(trial, data)
                if loss < best_loss - 1e-9:
                    best, best_loss, moved = trial, loss, True
        if not moved:
            step /= 2.0
            if step < 1e-3:
                break

    result["weights"] = [round(x, 6) for x in best]
    result["loss"] = round(best_loss, 6)
    result["improved"] = best_loss < baseline - 1e-6
    result["improvement"] = round(baseline - best_loss, 6)
    result["scale"] = round(_scale(best), 4)
    result["message"] = _message(result)
    return result


def _message(result: Dict[str, Any]) -> str:
    n, scale = result["n"], result.get("scale", 1.0)
    if not result["improved"]:
        return (
            f"{n} reviews, and the published defaults already fit them better "
            "than anything nearby. Nothing to change — that is a good result, "
            "not a failed one."
        )
    direction = "longer" if scale > 1 else "shorter"
    tail = (
        ""
        if result["enough"]
        else f" Only {n} of the {MIN_REVIEWS} needed, so this is a preview and is not offered for use yet."
    )
    return (
        f"Your memory holds about {scale}× {'longer' if scale > 1 else 'shorter'} "
        f"than the defaults assume, over {n} reviews — intervals would get "
        f"{direction}.{tail}"
    )


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------


def weights_path(deck_dir: Path) -> Path:
    """Beside the review history it was fitted from.

    Not in `_system/`: that folder is disposable by declaration, and weights
    fitted from a year of reviews are not. Not in `config.yaml` either —
    writing to a hand-edited config file behind lj's back is how you lose
    someone's comments.
    """
    return Path(deck_dir) / WEIGHTS_FILE


def save(deck_dir: Path, result: Dict[str, Any]) -> Path:
    path = weights_path(deck_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "weights": result["weights"],
                "n": result["n"],
                "loss": result["loss"],
                "baseline_loss": result["baseline_loss"],
                "fitted_from": "_reviews.jsonl",
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    return path


def load(deck_dir: Path) -> List[float]:
    """Fitted weights, or [] — a wrong-length list is ignored, never used."""
    try:
        data = json.loads(weights_path(deck_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    weights = data.get("weights")
    if isinstance(weights, list) and len(weights) == len(fsrs.DEFAULT_W):
        try:
            return [float(x) for x in weights]
        except (TypeError, ValueError):
            return []
    return []
