"""The collector's fallacy — telling collecting apart from learning.

Saving a good note produces the feeling of having learned the thing. It is a
reliable feeling and it is not evidence of anything: the note is on disk, the
reading was fluent, and fluency during input is close to uncorrelated with
whether the material can be produced later (Bjork's "desirable difficulties";
Karpicke & Blunt on retrieval practice beating elaborative study). A vault
that grows faster than it is used is a vault that is getting *worse* at
answering questions, because every new note dilutes the ones that mean
something, and the system will happily help lj do that faster than ever.

So the system has to be able to say which it is, and the only honest way to
do that is from evidence already in the vault. **Two things count as having
used a note, and lj chose both:**

  * **A restatement.** Something written under `## In my own words` that is
    not the template's empty heading. This is the generation effect, and it
    is the one signal that cannot be produced by any amount of reading: you
    cannot restate a thing you did not follow.
  * **A card that has actually been answered.** A deck with at least one
    `active` card whose `reps` is above zero. Generating cards and never
    reviewing them is collecting with extra steps, so a drafted or never-seen
    card does not count.

Three things deliberately do **not** count, and each was considered:

  * *Links.* An automatic `## Related` section is written on every capture —
    counting it would mark the entire vault as processed on the day it was
    written, which is precisely the illusion being measured.
  * *Having been edited since.* `updated` is bumped by re-renders the system
    does on its own; a signal a machine can forge is not a signal.
  * *Having cards at all.* See above.

**What is even eligible to be debt** matters as much as the test itself, or
the report is noise nobody reads. A note is only counted when it is a
Resource (the reference bucket — a Project is measured by being finished),
long enough to be worth processing at all (the same forty-word floor
`generate_folder` uses to decide a note is too thin to card), past a grace
period (a note written on Tuesday is not a debt on Wednesday), and not a term
(terms are carded by construction, and the undefined ones have their own
list). Everything else is left alone.

The report never deletes anything and never nags about a specific note twice
in one place. It says what is there and offers the three things that would
discharge it: restate it, card it, or retire it — retiring being a real
answer, and the reason the archive exists.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

#: The same floor `generate_folder` uses for "too thin to make cards from".
#: A twelve-word reference line is not a collector's-fallacy problem; a
#: nine-hundred-word clipping is.
MIN_WORDS = 40

#: A note is not a debt the day after it was written. Long enough to have
#: come round to it, short enough that a term's worth of notes is visible
#: well before the exam.
GRACE_DAYS = 14

#: Headings that mean "this is the part I wrote". Matched loosely because
#: they are typed by hand as often as they come from a template.
_RESTATEMENT = re.compile(
    r"^\s*#{1,6}\s*(in\s+my\s+own\s+words|my\s+own\s+words|own\s+words"
    r"|in\s+my\s+words|restatement|my\s+summary)\s*:?\s*$",
    re.I,
)

_HEADING = re.compile(r"^\s*#{1,6}\s+\S")

#: Template leftovers that are not a restatement. A heading followed by the
#: prompt that asked for one is still an empty heading.
_PLACEHOLDER = re.compile(r"^[\s\-*_>|]*$|^\s*\*.*\*\s*$")


def sections(body: str) -> List[Tuple[str, str]]:
    """(heading line, content) for each `#`-headed block, plus a leading
    block under "" for anything before the first heading."""
    out: List[Tuple[str, str]] = []
    heading, lines = "", []
    for line in (body or "").splitlines():
        if _HEADING.match(line):
            out.append((heading, "\n".join(lines)))
            heading, lines = line, []
        else:
            lines.append(line)
    out.append((heading, "\n".join(lines)))
    return out


def restatement(body: str) -> str:
    """What lj wrote in their own words, or "".

    Empty when the heading is absent, and equally empty when the heading is
    there with nothing under it — which is the normal state of a note filed
    from the Term template and never returned to, and exactly the case this
    has to catch.
    """
    for heading, content in sections(body):
        if not _RESTATEMENT.match(heading):
            continue
        kept = [ln for ln in content.splitlines() if not _PLACEHOLDER.match(ln)]
        said = "\n".join(kept).strip()
        if said:
            return said
        # Keep looking rather than answering "empty" on the first match. Every
        # note now ships with this heading already on it (that is the point of
        # it), so a second one further down — appended by hand, or by an
        # import — would otherwise be invisible behind the empty first.
    return ""


def content_words(body: str) -> int:
    """How much a note actually says, not how much is printed on it.

    Headings and HTML comments are the template's, not lj's. Once every note
    is rendered from a template, counting them would mean a 35-word capture
    arriving as a 43-word note and crossing the "worth processing" floor on
    the strength of the scaffolding around it — the measure would be counting
    its own furniture.
    """
    lines = [
        ln for ln in _strip_comments(body or "").splitlines()
        if not ln.lstrip().startswith("#")
    ]
    return len(" ".join(lines).split())


def _strip_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text or "", flags=re.S)


def reviewed_cards(deck: Any) -> int:
    """Active cards that have actually been answered at least once.

    `reps` rather than existence: a deck of twenty drafts is twenty questions
    nobody has tried to answer, which is collecting with extra steps.
    """
    if deck is None:
        return 0
    return sum(
        1 for card in getattr(deck, "cards", [])
        if getattr(card, "status", "") == "active" and getattr(card, "reps", 0) > 0
    )


def assess(
    note: Any,
    deck: Any = None,
    *,
    today: Optional[dt.date] = None,
    min_words: int = MIN_WORDS,
    grace_days: int = GRACE_DAYS,
) -> Dict[str, Any]:
    """One note: is this collected, used, or not in scope at all?

    Returns `eligible` separately from `used` on purpose. "Not in scope" and
    "processed" are different answers, and a report that conflates them
    inflates its own success rate — which would make this module an instance
    of the thing it exists to measure.
    """
    today = today or dt.date.today()
    words = content_words(getattr(note, "body", "") or "")
    created = getattr(note, "created", None)
    age = (today - created.date()).days if isinstance(created, dt.datetime) else None
    tags = [str(t).lower() for t in (getattr(note, "tags", None) or [])]

    bucket = getattr(getattr(note, "bucket", None), "value", "")
    reasons_out = []
    if bucket != "resource":
        reasons_out.append("not a Resource")
    if words < min_words:
        reasons_out.append(f"only {words} words")
    if age is not None and age < grace_days:
        reasons_out.append(f"written {age} day{'' if age == 1 else 's'} ago")
    if "term" in tags:
        reasons_out.append("a term — carded by construction")

    said = restatement(getattr(note, "body", "") or "")
    answered = reviewed_cards(deck)
    used = bool(said) or answered > 0

    why = []
    if said:
        why.append("restated in your own words")
    if answered:
        why.append(f"{answered} card{'' if answered == 1 else 's'} reviewed")

    return {
        "note_id": getattr(note, "id", ""),
        "title": getattr(note, "title", ""),
        "words": words,
        "age_days": age,
        "eligible": not reasons_out,
        "out_of_scope": reasons_out,
        "used": used,
        "why": " · ".join(why),
        "restated": bool(said),
        "reviewed_cards": answered,
        "has_cards": bool(getattr(deck, "cards", []) if deck else []),
    }


def summarise(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Counts over the eligible rows only, and the ratio that matters.

    `ratio` is used over eligible, not used over everything. A vault can
    always improve the second number by capturing more short notes, and a
    measure that rewards capturing more is worse than no measure.
    """
    eligible = [r for r in rows if r["eligible"]]
    used = [r for r in eligible if r["used"]]
    collected = [r for r in eligible if not r["used"]]
    return {
        "eligible": len(eligible),
        "used": len(used),
        "collected": len(collected),
        "ratio": round(len(used) / len(eligible), 3) if eligible else None,
        "oldest_days": max((r["age_days"] or 0 for r in collected), default=0),
        "carded_but_unreviewed": sum(
            1 for r in collected if r["has_cards"] and not r["reviewed_cards"]
        ),
    }


def sentence(stats: Dict[str, Any]) -> str:
    """One line, for the weekly review and the dashboard header.

    States the number and nothing else. A measure that editorialises gets
    argued with instead of acted on.
    """
    if not stats["eligible"]:
        return "Nothing is old enough or long enough to count yet."
    if not stats["collected"]:
        return f"All {stats['used']} notes in scope have been restated or reviewed."
    bits = [
        f"{stats['collected']} of {stats['eligible']} notes have been kept but not used"
    ]
    if stats["oldest_days"]:
        bits.append(f"the oldest for {stats['oldest_days']} days")
    if stats["carded_but_unreviewed"]:
        bits.append(f"{stats['carded_but_unreviewed']} have cards nobody has answered")
    return " · ".join(bits) + "."
