"""Obsidian templates — the shape every note takes, whoever writes it.

These exist so a note created by hand in Obsidian is indistinguishable from one
created by the capture UI. The system reads both, and neither path is second
class.

**The templates are the source of truth for a note's shape**, and `sb/render.py`
is what makes that true rather than aspirational: the engine reads
`_templates/<Type>.md`, takes its headings, their order and its preamble, and
fills in the content it has. Editing a template changes what the system writes.
What lives *here* is the same article, so a deleted or corrupted template can be
put back byte for byte, and so there is something to fall back on when a
template is unreadable — a capture must never fail because a template was being
edited.

## The frontmatter rule: no comments, no exceptions

Every template in this vault was silently corrupted between 23 August and 14
September, and the cause is worth writing down because it will happen again the
moment anyone forgets it.

**Obsidian's Properties editor rewrites frontmatter it cannot round-trip.** It
does not warn. On these files it flattened `habit:`/`cadence:` into siblings,
renamed `cycle_days` to `cycle days` and `materials` to `material`, swallowed
inline `#` comments into values (`category: "# blank = detected from the words
above"`), and — where it hit something it could not parse at all — stopped, left
the remaining YAML in the note *body*, and closed the block with a stray `---`.
Six of the nine templates were in that state. A note made from any of them
arrived with no id and no bucket, which means the system refused to read it.

So: **frontmatter carries no comments.** Every word of explanation lives in the
body as an HTML comment, where Obsidian leaves it alone and where the person
filling the note in can actually see it. The frontmatter is plain, valid, boring
YAML that survives a round trip through the Properties editor.

Two further defences, because one rule is not a system: `doctor` parses every
template and builds a `Note` from it, so drift is caught the week it happens;
and `write_templates(vault, overwrite=True)` puts them all back from here.

The rule for keeping these and `_templates/` in step is that the **frontmatter
must match exactly** and the **headings must match exactly** — those two are
what the engine reads. Body prose may be edited freely in either place; it is
for the human, and `render.py` strips the HTML comments out of generated notes
so a term note does not carry a tutorial.
"""

from __future__ import annotations

from typing import Dict, List

from .vault import Vault

#: Which template serves which objective. This is the "templates are the
#: defaults for different objectives" mapping: a capture says what it is for,
#: and the template says what that looks like.
#:
#: `capture` is the plain hotkey press with nothing else said, and it is an
#: Atomic Note on purpose — every captured note then arrives with the
#: `## In my own words` heading already on it, which is the one slot
#: `sb/collected.py` reads to tell a note that was used from a note that was
#: merely kept.
FOR_OBJECTIVE: Dict[str, str] = {
    "capture": "Atomic Note",
    "atomic": "Atomic Note",
    "term": "Term",
    "resource": "Resource",
    "project": "Project",
    "area": "Area",
    "assignment": "Assignment",
    "paper": "Paper",
    "quiz": "Quiz",
    "curiosity": "Curiosity",
    "syllabus": "Syllabus",
    "session": "Session",
    "screenshot": "Screenshot",
}

TEMPLATES: Dict[str, str] = {
    "Project.md": """---
id: "{{date:YYYYMMDD}}T{{time:HHmmss}}-project"
title: "{{title}}"
bucket: project
created: "{{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}"
updated: "{{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}"
tags: []
source: manual
category:
project:
  status: active
  deadline:
  estimate_minutes: 60
  level: 3
  learning: false
  ideal_end:
  skills: []
  materials:
    - text: Reference or supply needed
      kind: material
      done: false
  steps:
    - id: s1
      text: First concrete action
      minutes: 30
      done: false
---

# {{title}}

**Done means:**

<!-- FRONTMATTER NOTES — kept here rather than up there, because Obsidian's
     Properties editor swallows comments inside the YAML block and has
     corrupted every template in this vault once already.

     category      blank = detected from the words you wrote (hw, study, quiz)
     materials     one list, three kinds: material | hardware | software.
                   The ## Materials section below is rendered FROM this,
                   including the Hardware and Software subsections, which used
                   to be hand-written headings the dashboard wiped on every
                   re-render.
     steps         each concrete enough to start with no further planning. -->

## Steps

- [ ] First concrete action (30m)

## Materials

<!-- Rendered from project.materials above. Tick a box here and the state is
     read back into frontmatter on the next render, so ticking in Obsidian
     sticks. Hardware and Software appear as ### subsections when a material
     is given that kind. -->

- [ ] Reference or supply needed

## Capture
""",
    "Assignment.md": """---
id: "{{date:YYYYMMDD}}T{{time:HHmmss}}-assignment"
title: "{{title}}"
bucket: project
created: "{{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}"
updated: "{{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}"
tags:
  - assignment
source: manual
category: hw
project:
  status: active
  deadline:
  estimate_minutes: 60
  level: 3
  learning: true
  ideal_end:
  skills: []
  materials:
    - text: the source guide file, e.g. unit3_study_guide.pdf
      kind: material
      done: false
  steps:
    - id: s1
      text: "Why does [[Key Term]] behave the way it does?"
      minutes: 20
      done: false
---

# {{title}}

**Done means:**

<!-- ONE STEP PER QUESTION OR KEY CONCEPT. Wikilink the key terms inside the
     question text; where a key term IS the atomic note, the question and the
     pointer to that note are the same link. The links start empty and resolve
     during stage 2 below. -->

## Steps

- [ ] Why does [[Key Term]] behave the way it does? (20m)

## Answers

<!-- Your written answers. Each one links out to the atomic note it draws on,
     and key terms in the prose are linked too.

     This section is SAFE: _project_body() preserves any heading it did not
     write itself, so ticking a step or editing the deadline no longer
     regenerates your answers away.

     It is also the only part of this note flashcards come from — `steps` is
     in generate.py's SKIP_HEADINGS, so the bare questions are not chunked,
     and cards get made from what you actually wrote. -->

### Why does [[Key Term]] behave the way it does?

→ [[Key Term]]

## Materials

## Capture

<!-- HOW THIS NOTE GETS FILLED IN — two stages, in order.

  1. SCOPE. Upload the guide: the PDF/text of questions and key concepts.
     Every question and concept becomes one step above. This list is the
     scope contract — anything in a source text that does not serve a line
     up there is out of scope and gets no note.

  2. MATERIAL. Feed in blocks of text (readings, lecture notes, textbook
     excerpts). The fewest atomic notes that cover the questions above get
     written, and the empty links resolve as they appear. Fewest is a real
     constraint: one note covering two tightly related ideas beats two thin
     ones. -->
""",
    "Paper.md": """---
id: "{{date:YYYYMMDD}}T{{time:HHmmss}}-paper"
title: "{{title}}"
bucket: project
created: "{{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}"
updated: "{{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}"
tags:
  - paper
source: manual
category: hw
project:
  status: active
  deadline:
  estimate_minutes: 240
  level: 3
  learning: false
  ideal_end:
  skills: []
  materials:
    - text: the assignment prompt
      kind: material
      done: false
  steps:
    - id: s1
      text: "Intro — frame the question, state the thesis"
      minutes: 30
      done: false
    - id: s2
      text: "Body 1 — [[Supporting concept]] establishes the mechanism"
      minutes: 60
      done: false
---

# {{title}}

**Done means:**

<!-- THE STEPS ARE THE SECTION OUTLINE — structure only, no prose. Each step
     names a section and what it must accomplish, with [[links]] to the atomic
     notes that supply evidence. -->

## Steps

- [ ] Intro — frame the question, state the thesis (30m)
- [ ] Body 1 — [[Supporting concept]] establishes the mechanism (60m)

## Thesis

<!-- Your angle, in your own words. The prompt sets the constraints; this sets
     the direction. Preserved across re-renders. -->

## Draft

<!-- The prose does NOT live here — it lives in its own note, so the plan and
     the writing stay separate. Link it: -->

→ [[{{title}} — draft]]

## Materials

## Capture

<!-- HOW THIS NOTE GETS FILLED IN

  This is a paper you are WRITING, not one you are reading. It builds toward
  an output rather than atomizing from a source, and it creates no atomic
  notes of its own.

  Give it two things: the assignment prompt (constraints — length, sources,
  the question) and your own angle (direction). The existing vault is then
  searched for atomic notes that bear on the thesis, and the relevant ones
  are linked into the outline above as evidence. That search is the payoff
  for everything atomized by past assignments and curiosities. -->
""",
    "Quiz.md": """---
id: "{{date:YYYYMMDD}}T{{time:HHmmss}}-quiz"
title: "{{title}}"
bucket: project
created: "{{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}"
updated: "{{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}"
tags:
  - quiz
source: manual
category: quiz
project:
  status: active
  deadline:
  estimate_minutes: 60
  level: 3
  learning: true
  ideal_end:
  skills: []
  materials: []
  steps:
    - id: s1
      text: Compile key concepts and link their atomic notes
      minutes: 20
      done: false
    - id: s2
      text: Generate flashcards from this note
      minutes: 10
      done: false
    - id: s3
      text: Review until every concept is solid
      minutes: 30
      done: false
---

# {{title}}

**Done means:**

## Steps

- [ ] Compile key concepts and link their atomic notes (20m)
- [ ] Generate flashcards from this note (10m)
- [ ] Review until every concept is solid (30m)

## Key Concepts

<!-- A PURE INDEX. Just the concept and its atomic note(s) — no inline gists.

     The gists used to be required because card generation read only this
     note's own body and ignored links, so the substance had to be duplicated
     here where it could drift out of sync with the real notes. generate.py
     now resolves [[links]] one level deep and reads the linked atomic notes'
     Definition and In my own words directly, so the links ARE the material.

     Preserved across re-renders by _project_body()'s preserve pass. -->

- [ ] [[Concept A]]
- [ ] [[Concept B]]

## Materials

## Capture

<!-- Quiz notes are spawned FROM an existing Assignment or Syllabus rather
     than written blank — picking Quiz in the dashboard asks which one to
     build from, and pulls its concepts across. -->
""",
    "Atomic Note.md": """---
id: "{{date:YYYYMMDD}}T{{time:HHmmss}}-atomic"
title: "{{title}}"
bucket: resource
created: "{{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}"
updated: "{{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}"
tags:
  - atomic
source: manual
review:
  cycle_days: 90
  next:
---

# {{title}}

*From:*

<!-- *From:* takes one link back to the guide this came from — [[parent note
     title]]. Blank when this was captured standalone rather than atomized out
     of an Assignment or a Curiosity.

     This is also the shape a plain capture takes: press the capture hotkey,
     type a line, and it arrives here. That is deliberate. Every captured note
     then has the ## In my own words heading already on it, which is the one
     slot that tells a note you used from a note you merely kept. -->

## Definition

<!-- The minimum needed to answer the question(s) it came from — nothing
     extra. If it wasn't needed for a question or roadmap item in the guide,
     it doesn't belong here. -->

## In my own words

<!-- Empty on purpose, and it stays that way: this slot is never filled in for
     you, not even on request. The retrieval effort of writing it is the whole
     point, which also makes a blank one a reliable signal that you haven't
     processed this note yet — see sb/collected.py, which counts exactly this.

     Both sections above are what flashcards are actually made of. generate.py
     resolves [[links]] one level deep, so when a Quiz or Assignment links
     here, this note is the passage the cards come from. -->
""",
    "Term.md": """---
id: "{{date:YYYYMMDD}}T{{time:HHmmss}}-term"
title: "{{title}}"
bucket: resource
created: "{{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}"
updated: "{{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}"
tags:
  - term
  - atomic
source: manual
category: study
review:
  cycle_days: 90
  next:
---

# {{title}}

## Definition

<!-- One sentence. A term and its definition become a note AND a flashcard —
     the card is written, not generated, so this text is the back of the card
     exactly as you type it. That makes it the one study path an Ollama outage
     cannot touch, and it makes a sloppy definition a sloppy card forever. -->

## In my own words

<!-- The restatement. A term you can only quote is a term you have collected. -->

## Seen in

<!-- Where you met it, one line per sighting. The second and third sightings
     are what turn a definition into a meaning, which is why this is a list in
     the body rather than a single `source:` field up in the frontmatter. -->
""",
    "Curiosity.md": """---
id: "{{date:YYYYMMDD}}T{{time:HHmmss}}-curiosity"
title: "{{title}}"
bucket: resource
created: "{{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}"
updated: "{{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}"
tags:
  - curiosity
source: manual
category: study
review:
  cycle_days: 180
  next:
---

# {{title}}

## Roadmap

<!-- STRICT PREREQUISITE ORDER — each item must be understood before the next
     one makes sense (algebra 1 before algebra 2 before calculus). The order
     IS a dependency chain, unlike a Paper's outline.

     Every link starts EMPTY. Name what is vital here; the atomic note gets
     written when you actually feed in material for that step.

     This lives in the body rather than project.steps because a Resource note
     has no project block. Nothing in the app ever rewrites a Resource body
     (only _project_body() and _area_body() exist), so this outline is safe to
     hand-edit however you like — no wipe risk at all.

     The trade-off, accepted deliberately: no tracked step state, no calendar
     task, and no work-in-progress row on the dashboard. A curiosity is a
     background interest, not something to be nagged about. -->

- [ ] [[First prerequisite]]
- [ ] [[Builds on the first]]
- [ ] [[The thing you actually wanted to understand]]

## Source

<!-- Where the curiosity came from, and any material worth coming back to.

     HOW ATOMIC NOTES GET WRITTEN — the same rule as an Assignment: feed in a
     block of text, and the fewest atomic notes that cover the relevant
     roadmap items above get written. The roadmap is the filter; anything in
     the source that does not serve an item up there is out of scope. -->
""",
    "Syllabus.md": """---
id: "{{date:YYYYMMDD}}T{{time:HHmmss}}-syllabus"
title: "{{title}}"
bucket: resource
created: "{{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}"
updated: "{{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}"
tags:
  - syllabus
source: manual
category: hw
review:
  cycle_days: 120
  next:
---

# {{title}}

**Course:**
**Term:**

## Course Summary

<!-- Converted from the syllabus's own date / item / due table. One heading
     per due date, one checklist line per item underneath it. Every item
     title is a [[link]] to that item's OWN note:

       - Discussion / Assignment items → built from _templates/Assignment.md
       - Quiz / Exam items             → built from _templates/Quiz.md

     That mapping is still PROVISIONAL — never checked against a real course.
     Two known objections: an Exam covers a whole unit rather than one concept
     set, so it may want to aggregate every preceding item instead of reusing
     the Quiz shape; and a Discussion post is short writing, closer to Paper
     than to the extract→atomize→answer flow.

     Each of those notes carries its own real project.deadline (set to the
     date below), so THAT note produces the calendar task — this note carries
     no deadline of its own, it's the index, not a task. All of them are
     created AT ONCE when the syllabus is ingested, so the whole term hits the
     calendar immediately; the stubs stay empty until each one is fed its own
     guide and text blocks.

     Resource notes are never auto-rewritten by the dashboard (only Project
     and Area bodies are — see _project_body()/_area_body() in engine.py), so
     this whole table is safe to hand-edit or regenerate freely: no
     wipe-on-rerender risk here, unlike the Project-bucket templates. -->

### Sun Jul 5, 2026

- [ ] **Discussion** — [[Discussion: Topic name]] — due by 11:59pm
- [ ] **Assignment** — [[Assignment title]] — due by 11:59pm
- [ ] **Quiz** — [[Quiz title]] — due by 11:59pm

## Capture
""",
    "Area.md": """---
id: "{{date:YYYYMMDD}}T{{time:HHmmss}}-area"
title: "{{title}}"
bucket: area
created: "{{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}"
updated: "{{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}"
tags: []
source: manual
category:
habit:
  cadence: weekly
  target_count: 3
schedule:
  enabled: true
  time: "18:00"
  duration_minutes: 30
  days: []
  monthday: 1
  start:
  until:
---

# {{title}}

*Ongoing responsibility. No end state, so no due date — it holds a recurring
block of real time instead. Change the time, length or days in the dashboard,
or at the weekly schedule review.*

<!-- FRONTMATTER NOTES — down here, because comments inside the YAML block are
     what corrupted every template in this vault once already.

     category       blank = detected from the words you wrote
     habit.cadence     daily | weekly | monthly
     schedule.days     weekday numbers, Monday = 0. Empty = every day the
                       cadence allows.
     schedule.monthday day of the month, used only when cadence is monthly.
                       It needs a number, not a blank — leaving it empty is
                       what makes this file unreadable. -->

## Capture

## Check-in log
""",
    "Resource.md": """---
id: "{{date:YYYYMMDD}}T{{time:HHmmss}}-resource"
title: "{{title}}"
bucket: resource
created: "{{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}"
updated: "{{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}"
tags: []
source: manual
review:
  cycle_days: 90
  next:
---

# {{title}}

## Notes

## Source
""",
    "Session.md": """---
id: "{{date:YYYYMMDD}}T{{time:HHmmss}}-session"
title: "{{title}}"
bucket: resource
created: "{{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}"
updated: "{{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}"
tags:
  - session
source: manual
category: study
review:
  cycle_days: 90
  next:
---

# {{title}}

## Look these up

<!-- Terms captured during the session with no definition yet. Its own heading,
     above everything else, because it is the homework the session generated
     and it is worth nothing buried in the middle of the other lists. Each one
     is a flashcard the moment it has a definition. -->

## Terms

## Notes

<!-- Written when the session closes, from the session's own list of note ids —
     so it can only ever claim notes that were actually taken. A note deleted
     or moved in Obsidian mid-lecture is simply absent rather than appearing
     here as a dead link. -->
""",
    "Screenshot.md": """---
id: "{{date:YYYYMMDD}}T{{time:HHmmss}}-screenshot"
title: "{{title}}"
bucket: resource
created: "{{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}"
updated: "{{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}"
tags:
  - screenshot
source: manual
review:
  cycle_days: 90
  next:
---

# {{title}}

## Image

<!-- Embedded by filename — ![[screenshot-20260914T111500.png]] — not by path,
     because Obsidian resolves an embed by name from anywhere in the vault and
     the link therefore survives the note being moved by the group-file box. -->

## In my own words

<!-- A picture of a slide is the purest form of collecting: it took one
     keystroke and it contains nothing you produced. This heading is why the
     screenshot is a note rather than a loose file in an attachments folder. -->

## Seen in
""",
}

README = """# _system

Machine state for the Second Brain engine. Safe to exclude from Obsidian
search (Settings → Files & Links → Excluded files → add `_system`).

    calendar/   generated .ics — import or subscribe to it from your calendar
    index/      RAG embeddings cache (rebuildable; safe to delete)
    logs/       capture and calendar sync logs

Nothing here is a source of truth. Delete any of it and the engine rebuilds it
from the notes.
"""

TEMPLATES_README = """# _templates

The shape every note takes, whoever writes it — you in Obsidian, or the capture
box. `sb/render.py` reads these files and fills them in, so **editing a template
here changes what the system writes.**

## One rule, and it matters

**Never put a `#` comment inside the `---` frontmatter block.**

Obsidian's Properties editor rewrites frontmatter it cannot round-trip, without
warning. Between 23 August and 14 September it corrupted every template in this
folder: it flattened `habit:`/`cadence:` into siblings, renamed `cycle_days` to
`cycle days` and `materials` to `material`, turned inline comments into string
values, and left the YAML it could not parse sitting in the note body after a
stray `---`. A note made from any of them arrived with no id and no bucket, and
the system refused to read it.

Explanations belong in the body as `<!-- HTML comments -->`. Obsidian leaves
those alone, the person filling the note in can see them, and `render.py` strips
them out of generated notes so a term note does not carry a tutorial.

If you would rather not risk it at all: **Settings → Editor → Properties in
document → Source**. That turns off the Properties UI and shows the YAML as
text, which is what it is.

## If one breaks

    python run.py templates --repair     # put them all back
    python run.py doctor                 # check them without changing anything

`doctor` parses every template and builds a real note from it, so drift is
reported the week it happens rather than the next time you make a note by hand.
"""


def write_templates(vault: Vault, overwrite: bool = False) -> List[str]:
    """Write the templates into the vault.

    `overwrite=False` (the default) only creates what is missing, which is what
    setup wants: it must never clobber a template lj has edited. `overwrite=True`
    is the repair path — `run.py templates --repair` — for when a template has
    been corrupted rather than customised, and it says so before it runs.
    """
    vault.ensure_structure()
    written: List[str] = []
    tdir = vault.root / "_templates"
    tdir.mkdir(parents=True, exist_ok=True)
    for name, content in TEMPLATES.items():
        path = tdir / name
        if overwrite or not path.exists():
            path.write_text(content, encoding="utf-8")
            written.append(name)
    readme = tdir / "README.md"
    if overwrite or not readme.exists():
        readme.write_text(TEMPLATES_README, encoding="utf-8")
        written.append("_templates/README.md")
    sysreadme = vault.root / "_system" / "README.md"
    if not sysreadme.exists():
        sysreadme.write_text(README, encoding="utf-8")
        written.append("_system/README.md")
    return written
