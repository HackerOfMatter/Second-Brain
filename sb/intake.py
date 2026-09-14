"""The Drop folder — filing notes lj already wrote.

Capture (blueprint §2) assumes the thought is being typed *now*, into a box,
with three buttons under it. A note that already exists as a file has no such
moment: it was written in Word, or exported from somewhere, or typed into a
text editor at 1am. Asking lj to re-type it into the capture box to get it
classified is the kind of friction that ends with a folder of loose notes
nobody ever files.

So: a `Drop/` folder in the vault. Put a file in it, and the system reads it,
decides where it belongs, and files it there with the same treatment a capture
of the same text would have got — a Project gets parsed, planned and pushed to
the calendar; an Area gets a habit and a recurring block; a Resource gets a
review date.

## Rules first, model second — and a floor under the whole thing

The same shape as `parser.py` and `connect.py`, for the same reason: the
deterministic signals here are strong. A note that names a date and says
"submit" is a Project; one that says "every morning" is an Area; one that is
eight paragraphs of prose with two links and no verb aimed at lj is a
Resource. `score()` is that reasoning, written down, with every hit recorded
so the dashboard can show *why* — a classification you cannot inspect is one
you cannot correct.

The model is asked only when the rules are genuinely torn, which on real notes
is the minority case, and it is asked for one small judgement rather than a
free hand.

## Nothing is filed on a coin flip

`AUTO_FLOOR` is the line between "file it" and "ask". Below the floor the note
still lands in the vault — in `00-Inbox`, carrying its suggestion — and the
dashboard shows it with three buttons, which is exactly the blueprint's manual
classification with the guessing already done. The failure mode of an
automatic filer is a note in the wrong folder that lj never sees again; this
is the cheapest possible insurance against it, and it is why the confidence
number is computed from the *margin* between the top two buckets rather than
from the winner's score alone. A note with strong evidence for two buckets is
not a confident call, however strong the evidence.
"""

from __future__ import annotations

import datetime as dt
import html
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import atomic, extract, frontmatter

#: Everything lj drops lives here; the two subfolders are ours.
DROP_DIR = "Drop"
FILED_DIR = "_filed"      # originals, after a successful filing
PROBLEM_DIR = "_problem"  # things we could not read at all

TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".text"}
DOCX_SUFFIX = ".docx"
SUPPORTED = TEXT_SUFFIXES | {DOCX_SUFFIX}

BUCKETS = ("project", "area", "resource")

#: Below this, the note goes to the Inbox with its suggestion instead of being
#: filed. Tunable in config (`intake.auto_floor`). Deliberately not 0.5: a
#: bare majority is not a decision worth making on someone's behalf.
AUTO_FLOOR = 0.6

#: A file still being written by Word or a sync client is not ready to read.
#: Wait until it has been still for this long before touching it.
SETTLE_SECONDS = 5.0

#: A browser upload is held to the same scale a dropped note is. Far past any
#: note anyone writes, far short of anything that would stall the box while it
#: is base64'd through a JSON body. Enforced server-side as well as in the UI,
#: because the UI is not the only thing that can POST.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_UPLOAD_FILES = 50


# --------------------------------------------------------------------------
# reading what was dropped
# --------------------------------------------------------------------------


@dataclass
class Dropped:
    """One file from the Drop folder, turned into text we can classify."""

    path: Path
    title: str = ""
    text: str = ""
    error: str = ""
    already_ours: bool = False  # carried Second Brain frontmatter

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.text.strip())


def read_file(path: Path) -> Dropped:
    path = Path(path)
    suffix = path.suffix.lower()
    try:
        if suffix in TEXT_SUFFIXES:
            return _read_text(path)
        if suffix == DOCX_SUFFIX:
            return _read_docx(path)
    except Exception as exc:  # unreadable is a report, never a crash
        return Dropped(path, error=f"{type(exc).__name__}: {exc}")
    return Dropped(path, error=f"unsupported file type {suffix or '(none)'}")


def _read_text(path: Path) -> Dropped:
    raw = path.read_text(encoding="utf-8", errors="replace")
    meta, body = frontmatter.parse(raw)
    # A file exported from this vault comes back with our own frontmatter on
    # it. Its title is worth keeping; its id is not — re-importing under the
    # old id would mean two files claiming to be the same note.
    title = str(meta.get("title") or "").strip()
    return Dropped(
        path,
        title=title or _title_from(body, path),
        text=(body if meta else raw).strip(),
        already_ours=bool(meta.get("id")),
    )


def _read_docx(path: Path) -> Dropped:
    text = _docx_text(path)
    if not text.strip():
        return Dropped(path, error="the document had no readable text")
    return Dropped(path, title=_title_from(text, path), text=text.strip())


def _docx_text(path: Path) -> str:
    """Paragraph text out of a .docx, with headings and bullets preserved.

    A .docx is a zip with an XML document inside it, and the structure that
    matters to the classifier — headings, bullets, paragraph breaks — is in
    tags simple enough to read directly. So the zip reader goes first and
    python-docx is the fallback, which is the opposite of the usual ordering
    and deliberate:

      * python-docx is not installed in lj's venv, so the zip reader is the
        path that will actually run. Making the tested path the shipped path
        is the whole lesson of the roadmap's Tier 0.
      * It needs no install, so dropping a Word file never fails with a pip
        command lj had no reason to expect.
      * It is more forgiving. python-docx insists on parts a .docx is
        supposed to have; a file exported by some other tool that it refuses
        is usually one the zip reader can still get the words out of.

    The library still earns its place as the fallback: it reads tables, which
    the zip reader skips, and it copes with documents whose body is nested in
    ways a regex would miss.
    """
    return _docx_zip_text(path) or _docx_via_library(path)


def _docx_zip_text(path: Path) -> str:
    """The no-library reader: paragraphs straight out of word/document.xml.

    Returns "" if the archive is not shaped like a Word document at all, so
    the caller can fall back rather than treating a failure as an empty note.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
    except (KeyError, zipfile.BadZipFile):
        return ""

    lines: List[str] = []
    for para in re.findall(r"<w:p[ >].*?</w:p>|<w:p/>", xml, flags=re.S):
        style = ""
        m = re.search(r'<w:pStyle w:val="([^"]+)"', para)
        if m:
            style = m.group(1)
        if "<w:numPr" in para:
            style = "ListParagraph"
        chunks: List[str] = []
        for token in re.findall(r"<w:t[^>]*>(.*?)</w:t>|<w:tab/>|<w:br/>", para, flags=re.S):
            chunks.append(html.unescape(token) if token else " ")
        text = re.sub(r"\s+", " ", "".join(chunks)).strip()
        lines.append(_docx_prefix(style) + text if text else "")
    return _tidy_lines(lines)


def _docx_via_library(path: Path) -> str:
    """python-docx, if it is there and can open this file. "" means fall back."""
    try:
        import docx  # type: ignore

        document = docx.Document(str(path))
        out: List[str] = []
        for para in document.paragraphs:
            style = para.style.name if para.style is not None else ""
            out.append(_docx_prefix(style) + para.text.strip())
        for table in document.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    out.append("- " + " · ".join(cells))
        return _tidy_lines(out)
    except Exception:
        return ""


def _docx_prefix(style: str) -> str:
    """Word's style names, mapped onto the markdown the rest of the system
    already reads. A heading that survives as a heading keeps the note's shape,
    and a bullet that survives as `- ` is a step the parser can find."""
    s = (style or "").lower().replace(" ", "")
    if s.startswith("heading"):
        level = "".join(ch for ch in s if ch.isdigit()) or "2"
        return "#" * min(4, max(2, int(level) + 1)) + " "
    if s in ("listparagraph", "listbullet", "listnumber"):
        return "- "
    return ""


def _tidy_lines(lines: List[str]) -> str:
    out: List[str] = []
    for line in lines:
        line = line.rstrip()
        if not line and out and not out[-1]:
            continue  # collapse runs of blank lines
        out.append(line)
    return "\n".join(out).strip()


def _title_from(text: str, path: Path) -> str:
    """The note's own first heading if it has one, else the filename.

    The filename is usually the better title for a dropped file — someone
    named it deliberately — but a document whose first line is a real heading
    named itself twice, and the heading is the version with capitals and
    spaces in the right places.
    """
    for line in text.strip().splitlines():
        line = line.strip()
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            if 3 <= len(heading) <= 120:
                return heading[:80]
        if line:
            break
    stem = re.sub(r"[_\-]+", " ", path.stem).strip()
    stem = re.sub(r"\s+", " ", stem)
    if stem:
        return (stem[:1].upper() + stem[1:])[:80]
    return extract.derive_title(text)


# --------------------------------------------------------------------------
# what the folder is offering
# --------------------------------------------------------------------------


def candidates(drop: Path, settle_seconds: float = SETTLE_SECONDS) -> List[Path]:
    """Files ready to be filed: top level of Drop/, still for a moment.

    Only the top level. `_filed/` and `_problem/` are ours, and a subfolder lj
    made is a subfolder lj is still organising — recursing into it would file
    a half-sorted pile the moment it appeared.
    """
    drop = Path(drop)
    if not drop.is_dir():
        return []
    now = dt.datetime.now().timestamp()
    out: List[Path] = []
    for path in sorted(drop.iterdir()):
        if not path.is_file() or path.name.startswith((".", "~$")):
            continue
        if path.name.lower() in ("readme.md", "read me.md"):
            continue  # the instructions we put there
        if path.suffix.lower() not in SUPPORTED:
            continue
        try:
            if now - path.stat().st_mtime < settle_seconds:
                continue  # still being written
        except OSError:
            continue
        out.append(path)
    return out


#: Everything Windows forbids in a filename, plus the Obsidian-hostile few.
_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]')


def safe_drop_name(name: str) -> str:
    """A filename from a browser, made safe to write into Drop/.

    The name arrives from a JSON body, which means it arrives from whatever
    the caller felt like sending. `../../config.yaml` must not become a path,
    and a name Windows refuses must not turn a dropped file into a 500. So:
    the basename only, separators stripped rather than interpreted, and a
    fallback name when nothing usable is left — dropping a file with an
    unprintable name should cost the name, not the file.
    """
    base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = _UNSAFE_NAME.sub("", base).strip().strip(".")
    if not base:
        base = "dropped"
    suffix = Path(base).suffix.lower()
    stem = Path(base).stem[:80] or "dropped"
    return f"{stem}{suffix}"


def accept_upload(drop: Path, name: str, data: bytes) -> Path:
    """Write one uploaded file into Drop/, as if it had been copied there.

    This is the whole of what the browser adds: the folder remains the
    interface, the classifier is untouched, and a file that arrives this way
    is indistinguishable afterwards from one lj dragged into the folder in
    Explorer. Nothing here decides anything about the note — that is still
    `classify()`'s job on the next intake pass.

    Unsupported types are refused *here*, before the bytes land, rather than
    being written and then swept into `_problem/`: a file the system will
    never read should not end up sitting in the vault as litter, and the
    browser can say so while lj is still looking at the drop zone.
    """
    drop = Path(drop)
    safe = safe_drop_name(name)
    suffix = Path(safe).suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError(
            f"{safe}: unsupported file type {suffix or '(none)'} — "
            f"Drop reads {', '.join(sorted(SUPPORTED))}"
        )
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(
            f"{safe}: {len(data) // (1024 * 1024)} MB is past the "
            f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit"
        )
    if not data.strip():
        raise ValueError(f"{safe}: the file is empty")
    drop.mkdir(parents=True, exist_ok=True)
    target = drop / safe
    n = 2
    while target.exists():
        target = drop / f"{Path(safe).stem} ({n}){suffix}"
        n += 1
    target.write_bytes(data)
    return target


def file_away(path: Path, drop: Path, sub: str) -> Path:
    """Move an original into `_filed/` (or `_problem/`), never over another.

    Originals are kept rather than deleted because this is the one step that
    is not reversible from inside the vault: if a classification is wrong lj
    can move the note, but if the source file is gone and the read was bad,
    the note is gone with it.
    """
    target_dir = Path(drop) / sub
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    target = target_dir / f"{path.stem}--{stamp}{path.suffix}"
    n = 2
    while target.exists():
        target = target_dir / f"{path.stem}--{stamp}-{n}{path.suffix}"
        n += 1
    # Windows can still be holding the file we finished reading a moment
    # ago — the scanner opens every file that is touched. See sb/atomic.py.
    atomic.move(path, target)
    return target


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------


@dataclass
class Verdict:
    bucket: str
    confidence: float
    reason: str = ""
    signals: List[str] = field(default_factory=list)
    scores: Dict[str, float] = field(default_factory=dict)
    decided_by: str = "rules"  # rules | model | rules+model
    model_calls: int = 0

    @property
    def auto(self) -> bool:
        return self.confidence >= AUTO_FLOOR

    def as_dict(self) -> Dict[str, Any]:
        # `suggested`, not `bucket`: a row in the intake report carries both
        # where the note actually went and what was suggested, and those two
        # differ for exactly the notes that matter — the ones held back for
        # confirmation. One key for both would hide that.
        return {
            "suggested": self.bucket,
            "confidence": round(self.confidence, 2),
            "reason": self.reason,
            "signals": list(self.signals),
            "scores": {k: round(v, 2) for k, v in self.scores.items()},
            "decided_by": self.decided_by,
        }


#: (weight, label, pattern). Weights are small integers on purpose — this is a
#: tally of evidence, not a probability, and the confidence that comes out of
#: it is derived from how *separated* the buckets are, not from the raw total.
_PROJECT_RULES: List[Tuple[float, str, str]] = [
    (2.0, "a task verb aimed at you", r"\b(submit|turn in|hand in|finish|complete|"
     r"apply|register|sign up|book|buy|order|email|call|schedule|pack|clean|fix|"
     r"draft|write up|send|renew|file|pay|print|return)\b"),
    (2.0, "names an assignment or exam", r"\b(assignment|homework|hw|essay|lab|"
     r"problem set|pset|exam|midterm|final|quiz|test|presentation|deliverable)\b"),
    (1.5, "a checklist", r"^\s*[-*]\s*\[[ xX]\]"),
    (1.5, "says it is due", r"\b(due|deadline|by end of|before)\b"),
    (1.0, "a goal with an end state", r"\b(build|make|create|launch|ship|"
     r"learn|study for|prepare|plan|set up|install|migrate|organi[sz]e)\b"),
    (1.0, "numbered or bulleted steps", r"^\s*(?:\d+[.)]\s+\S|[-*]\s+\S)"),
]

_AREA_RULES: List[Tuple[float, str, str]] = [
    (3.0, "recurs on a cycle", r"\b(every ?day|everyday|daily|nightly|weekly|monthly|"
     r"each (?:day|morning|night|week|month)|every (?:morning|night|evening|week|"
     r"month|monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b"),
    (2.5, "a count per period", r"\b\d+\s*(?:x|times?)\s*(?:a|per|each)\s*"
     r"(?:day|week|month)\b"),
    (2.5, "calls itself a habit or routine", r"\b(habit|routine|ritual|practice "
     r"regularly|keep up|stay on top of|maintain|upkeep|ongoing|recurring)\b"),
    (1.5, "no end state", r"\b(never ends|no deadline|forever|always|"
     r"as long as|indefinitely)\b"),
    (1.0, "weekday shorthand", r"\b(m-f|mon-fri|weekdays|weekends|mwf)\b"),
]

_RESOURCE_RULES: List[Tuple[float, str, str]] = [
    (2.0, "reads as reference material", r"\b(notes? on|cheat ?sheet|reference|"
     r"summary of|glossary|definitions?|overview of|"
     r"documentation|docs|syllabus|reading|excerpt|transcript)\b"),
    (2.0, "explains rather than instructs", r"\b(is defined as|refers to|means that|"
     r"consists of|is a (?:type|kind|form) of|in other words|for example|"
     r"e\.g\.|i\.e\.)\b"),
    (1.5, "links out", r"https?://"),
    (1.0, "cites a source", r"\b(chapter|ch\.|lecture|article|paper|book|video|"
     r"course|module|section)\s*\d*\b"),
    (1.0, "a recipe or procedure to keep", r"\b(ingredients|steps to|how it works|"
     r"formula|equation|theorem|syntax|api|command)\b"),
]

#: Text this system itself renders into note bodies. A file exported from the
#: vault and dropped back in would otherwise be classified by *our* wording —
#: the exact failure phase 1 hit when the colour table matched its own
#: boilerplate. Strip it before scoring.
_OWN_BOILERPLATE = re.compile(
    r"^#{1,3} (?:Steps|Materials|Hardware|Software|Related|Capture|Check-in log)\s*$"
    r"|recurs on the calendar|No due date|·\s*due\s*\d{4}-\d{2}-\d{2}\s*·"
    r"|<!-- sb:.*?-->",
    re.M | re.S,
)


def _scrub(text: str) -> str:
    return _OWN_BOILERPLATE.sub(" ", text or "")


def score(text: str, title: str = "", today: Optional[dt.date] = None) -> Dict[str, Any]:
    """Tally the evidence for each bucket. Pure, deterministic, inspectable."""
    today = today or dt.date.today()
    blob = _scrub(f"{title}\n{text}")
    low = blob.lower()

    scores: Dict[str, float] = {b: 0.0 for b in BUCKETS}
    signals: Dict[str, List[str]] = {b: [] for b in BUCKETS}

    for bucket, rules in (
        ("project", _PROJECT_RULES),
        ("area", _AREA_RULES),
        ("resource", _RESOURCE_RULES),
    ):
        for weight, label, pattern in rules:
            if re.search(pattern, low, flags=re.M):
                scores[bucket] += weight
                signals[bucket].append(label)

    # A date is the strongest single signal there is, and the one piece of
    # evidence the system already parses better than any keyword list. A date
    # it had to *interpret* ("sometime next week") is weaker evidence of a
    # deadline than one the text spelled out, so it counts for less.
    guess = extract.parse_deadline_guess(blob, today)
    if guess.date:
        weight = 3.0 if guess.confirmed else 1.5
        scores["project"] += weight
        signals["project"].append(
            f"a deadline{' (' + guess.phrase.strip() + ')' if guess.phrase else ''}"
        )
        # A recurring commitment with a date in it is usually a Project *about*
        # an Area ("sign up for the gym by Friday"), so the date does not
        # cancel the recurrence signal — but a one-off date does argue against
        # "this has no end", which is what an Area is.
        if scores["area"]:
            scores["area"] -= 0.5

    # Shape, not vocabulary. Long expository prose with no imperative and no
    # date is reference material almost regardless of what it is about.
    words = len(re.findall(r"\w+", blob))
    if words >= 150 and not guess.date:
        scores["resource"] += 1.5
        signals["resource"].append("long-form, no date")
    if words <= 25 and not guess.date and scores["area"] == 0:
        # A one-liner with no date is a thought, not a filed thing. Nudge
        # nothing; let the margin stay low so it goes to the Inbox and gets
        # asked about.
        signals["project"].append("very short — hard to place")

    if re.search(r"^#{1,6} \S", blob, flags=re.M) and words >= 80:
        scores["resource"] += 1.0
        signals["resource"].append("structured with headings")

    return {"scores": scores, "signals": signals, "words": words}


def _confidence(scores: Dict[str, float]) -> Tuple[str, float, float]:
    """(winner, confidence, margin).

    Confidence is `strength × separation`, and both halves earn their place:

      * **strength** — a note that tripped one weak rule is a guess however
        lonely that rule was.
      * **separation** — a note that tripped five Project rules and five Area
        rules is *not* a confident Project, and a scheme that looked only at
        the winner's total would file it as one.

    The product is what makes the Inbox catch exactly the notes a person would
    also have hesitated over.
    """
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top, runner = ranked[0], ranked[1]
    if top[1] <= 0:
        return top[0], 0.0, 0.0
    margin = (top[1] - max(0.0, runner[1])) / top[1]
    strength = min(1.0, top[1] / 4.5)
    return top[0], round(strength * (0.45 + 0.55 * margin), 3), round(margin, 3)


def classify(
    text: str,
    title: str = "",
    cfg: Any = None,
    today: Optional[dt.date] = None,
    use_model: bool = True,
) -> Verdict:
    """Where does this note go? Rules decide unless they are torn."""
    detail = score(text, title, today)
    scores = detail["scores"]
    bucket, confidence, _margin = _confidence(scores)
    signals = list(detail["signals"].get(bucket, []))

    verdict = Verdict(
        bucket=bucket,
        confidence=confidence,
        reason=_reason(bucket, signals),
        signals=signals,
        scores=scores,
    )

    floor = _floor(cfg)
    if verdict.confidence >= floor or not use_model or cfg is None:
        return verdict
    return _ask_model(text, title, verdict, cfg, floor)


def _floor(cfg: Any) -> float:
    intake = getattr(cfg, "intake", None)
    return float(getattr(intake, "auto_floor", AUTO_FLOOR)) if intake else AUTO_FLOOR


def _reason(bucket: str, signals: List[str]) -> str:
    if not signals:
        return "nothing in the text pointed anywhere in particular"
    lead = ", ".join(signals[:3])
    return f"{lead} → {bucket}"


_MODEL_SYSTEM = """You file notes into a PARA system. Answer with JSON only.

The three buckets, and the only thing that separates them:
- "project": has an end. Something to finish, usually with or implying a date.
- "area": has no end. An ongoing responsibility, habit or routine that repeats.
- "resource": reference material. Something to keep and consult, not to do.

Judge what the note IS, not what it is about. Notes *about* a habit are still
resources if they are reference material. Be honest about uncertainty: a
confidence below 0.6 means "a person would have hesitated here too"."""

_MODEL_SCHEMA = """{"bucket": "project|area|resource", "confidence": 0.0, \
"reason": "one short clause, under 12 words"}"""


def _ask_model(text: str, title: str, rules: Verdict, cfg: Any, floor: float) -> Verdict:
    """One small question, asked only about the notes the rules could not call.

    The model is a tie-breaker, not an authority. Agreement raises the rules'
    confidence to whichever of the two is higher; disagreement is taken (the
    rules already said they were unsure) but discounted, so a model that is
    itself hedging cannot push a note past the floor on its own.
    """
    try:
        from .llm import resolve_provider

        provider = resolve_provider(cfg.llm, "parse")
        if not getattr(provider, "is_llm", False):
            return rules
        raw = provider.complete_json(
            _model_prompt(text, title, rules),
            system=_MODEL_SYSTEM,
            schema_hint=_MODEL_SCHEMA,
        )
    except Exception:
        return rules  # a model that is down must never cost lj a filing

    bucket = str(raw.get("bucket") or "").strip().lower()
    if bucket not in BUCKETS:
        return rules
    try:
        conf = max(0.0, min(1.0, float(raw.get("confidence", 0.0))))
    except (TypeError, ValueError):
        conf = 0.0
    why = str(raw.get("reason") or "").strip()[:120]

    rules.model_calls = 1
    if bucket == rules.bucket:
        rules.decided_by = "rules+model"
        rules.confidence = round(max(rules.confidence, conf), 3)
        if why:
            rules.reason = f"{rules.reason}; model agrees — {why}"
        return rules

    return Verdict(
        bucket=bucket,
        confidence=round(conf * 0.9, 3),
        reason=why or f"the model read this as a {bucket}",
        signals=[f"rules leaned {rules.bucket}", "model disagreed"],
        scores=rules.scores,
        decided_by="model",
        model_calls=1,
    )


def _model_prompt(text: str, title: str, rules: Verdict) -> str:
    tally = ", ".join(f"{k}={v:g}" for k, v in sorted(rules.scores.items()))
    body = (text or "").strip()
    if len(body) > 4000:  # a long note is decided by its opening and its shape
        body = body[:3000] + "\n…\n" + body[-800:]
    return (
        f"Title: {title or '(none)'}\n"
        f"A rule-based scorer was undecided (tally: {tally}).\n\n"
        f'Note:\n"""\n{body}\n"""\n\nWhich bucket, and how sure are you?'
    )


# --------------------------------------------------------------------------
# the note that comes out
# --------------------------------------------------------------------------


def stamp_note(note: Any, dropped: Dropped, verdict: Verdict, filed: bool) -> None:
    """Record where this note came from and why it landed where it did.

    Written into the frontmatter rather than only into a log, because the
    question "why is this a Project?" is asked while looking at the note, and
    six months later the log has rotated.
    """
    from .models import IntakeMeta

    note.source = "drop"
    note.intake = IntakeMeta(
        file=dropped.path.name,
        dropped_at=dt.datetime.now().astimezone().replace(microsecond=0),
        suggested=verdict.bucket,
        confidence=round(verdict.confidence, 2),
        reason=verdict.reason,
        signals=list(verdict.signals)[:6],
        decided_by=verdict.decided_by,
        filed_automatically=filed,
    )
    note.log(
        "intake",
        f"{dropped.path.name} → {verdict.bucket} "
        f"({verdict.confidence:.2f}, {verdict.decided_by})"
        + ("" if filed else " — held for confirmation"),
    )


README = """# Drop

Put notes you already wrote in this folder — `.md`, `.txt` or `.docx`.

The system reads each one, works out whether it is a **Project** (has an end),
an **Area** (repeats, no end) or a **Resource** (reference material), and files
it into the matching PARA folder with the full treatment: a Project gets
parsed, planned and put on the calendar, an Area gets a recurring block, a
Resource gets a review date.

When it is not sure, it does not guess. The note lands in `00-Inbox` with its
suggestion attached, and the dashboard asks you — one click.

Your original file is never deleted. It moves to `_filed/` once the note
exists, or to `_problem/` if it could not be read at all.

Filing runs when you press **File dropped notes** on the dashboard, and by
itself while the app is open.
"""
