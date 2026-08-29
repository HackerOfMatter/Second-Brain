"""Where the labelled examples live.

`sb/threshold.py` can only turn a guessed cutoff into a measured one if there
are examples with known answers. This is the file that keeps them.

It is a separate module for one reason, and it is a storage reason rather than
a code-organisation one: **`_system/` is disposable.** Phase 1 declared it so,
`connect.json` and the index both live there on that basis, and the doctor is
allowed to suggest deleting it. Labelled data is the opposite of disposable —
it accrues slowly, over months, one confirmation at a time, and it cannot be
regenerated from anything. Losing it means starting the sixty pairs again.

So labels go to `_labels/`, beside `_decks/`, and the README written there says
plainly that it is not safe to delete. Append-only JSONL, one line per answer,
skipping anything corrupt on read — the same shape and the same reasoning as
the review log.

Nothing here judges. It records what was suggested, what the score was, and
what lj decided.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

LABEL_DIR = "_labels"
INTAKE_LOG = "intake.jsonl"
LINK_LOG = "links.jsonl"

_README = """# Labels

Not disposable. Unlike `_system/`, nothing here can be regenerated.

Each line records a decision you made about something the system suggested,
together with the confidence it had at the time. That pairing is the only way
to tell whether a threshold is set right — see `docs/thresholds.md`.

`intake.jsonl` — a dropped file the system was unsure about, and the bucket
you chose.
`links.jsonl`  — a suggested link, and whether you kept it.
"""


class LabelStore:
    def __init__(self, vault_root: Path):
        self.root = Path(vault_root) / LABEL_DIR

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        readme = self.root / "README.md"
        if not readme.exists():
            readme.write_text(_README, encoding="utf-8")

    # -- write --------------------------------------------------------------

    def record_intake(
        self, note_id: str, suggested: str, chosen: str, confidence: float, **extra: Any
    ) -> None:
        """A note the intake floor held back, and the bucket lj picked.

        `correct` is the label: did the rules' suggestion match the decision?
        Storing the decision rather than only the verdict means a later change
        to what counts as correct can be recomputed from the same lines.
        """
        self._append(INTAKE_LOG, {
            "kind": "intake",
            "note_id": note_id,
            "suggested": suggested,
            "chosen": chosen,
            "confidence": round(float(confidence or 0.0), 4),
            "correct": bool(suggested and chosen and suggested == chosen),
            **extra,
        })

    def record_link(
        self, note_id: str, target: str, score: float, kept: bool, **extra: Any
    ) -> None:
        self._append(LINK_LOG, {
            "kind": "link",
            "note_id": note_id,
            "target": target,
            "score": round(float(score or 0.0), 4),
            "correct": bool(kept),
            **extra,
        })

    def _append(self, name: str, entry: Dict[str, Any]) -> None:
        self.ensure()
        with (self.root / name).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")

    # -- read ---------------------------------------------------------------

    def _read(self, name: str) -> Iterator[Dict[str, Any]]:
        path = self.root / name
        if not path.exists():
            return
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                yield rec

    def original_intake(self, note_id: str) -> Optional[Dict[str, Any]]:
        """The first answer ever recorded about this note, if there is one.

        What makes it worth reading back: filing a note by hand rewrites its
        `intake.suggested` and `intake.confidence` to the answer lj gave, so
        by the time a *second* answer is given — after story G2's undo — the
        note no longer remembers what the rules had guessed. The log does.
        Without this, a correction is recorded as "the system suggested what
        you first picked, at 100% confidence, and you disagreed", which is a
        labelled example of nothing.
        """
        for rec in self._read(INTAKE_LOG):
            if rec.get("note_id") == note_id:
                return rec
        return None

    def intake_pairs(self) -> List[Tuple[float, bool]]:
        """One pair per note, and it is the *last* answer given about it.

        The log stays append-only — nothing is rewritten, and the earlier
        lines are still there to read. But story G2's triage screen has an
        undo, and undo plus re-file writes two lines about the same note, one
        of which lj has explicitly retracted. Counting both would let a
        keystroke lj took back move a threshold, and would count 60 pairs
        when there were only 55 notes. Last answer wins; anything with no
        `note_id` is kept as its own pair, because there is nothing to
        supersede it with.
        """
        latest: Dict[str, Tuple[float, bool]] = {}
        loose: List[Tuple[float, bool]] = []
        for r in self._read(INTAKE_LOG):
            if r.get("confidence") is None:
                continue
            pair = (float(r.get("confidence") or 0.0), bool(r.get("correct")))
            note_id = r.get("note_id")
            if note_id:
                latest[str(note_id)] = pair
            else:
                loose.append(pair)
        return list(latest.values()) + loose

    def link_pairs(self) -> List[Tuple[float, bool]]:
        return [
            (float(r.get("score") or 0.0), bool(r.get("correct")))
            for r in self._read(LINK_LOG)
            if r.get("score") is not None
        ]

    def counts(self) -> Dict[str, int]:
        return {"intake": len(self.intake_pairs()), "link": len(self.link_pairs())}
