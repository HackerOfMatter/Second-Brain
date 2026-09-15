# Build log — Phase 14: the templates are the shape

**Status: DONE.** Written 2026-09-14. Full suite green: **132 tests, up from
127.** One ask, in lj's words: *"start utilizing obsidians templates for making
notes to be more uniform and legible"*, and *"when using ctrl alt z, the default
should be atomic note under templates. templates are to be used as defaults for
different objectives."*

## 1. The templates were all broken, and had been for three weeks

This was meant to be a tidying job. It was not.

**Every one of the nine templates in `_templates/` had had its frontmatter
rewritten, and a note made from any of them arrived with no id and no bucket** —
which means `Vault.read` refused it. Six of the nine had their `materials:` and
`steps:` YAML sitting in the note *body*, after a stray `---`.

The damage is specific and identifiable: nested blocks flattened (`habit:` and
`cadence:` became siblings), keys renamed (`cycle_days` → `cycle days`,
`materials` → `material`), and inline `#` comments swallowed into values, so
`category:` in `Area.md` literally held the string `"# blank = detected from the
words above (hw, study, quiz, ...)"`. That is **Obsidian's Properties editor**.
It rewrites frontmatter it cannot round-trip, it does not warn, and where it
hits something it cannot parse it stops, closes the block, and leaves the
remainder in the body.

The mtimes put it at 23 August — the day after the templates were written. So
the "notes made by hand don't match notes made by the system" problem was not
drift. The hand path had been producing unreadable notes since the day it
shipped, and nothing in the system was looking.

**The rule that holds it shut: no comments in frontmatter, ever.** Every word of
explanation moved into the body as an HTML comment, where Obsidian leaves it
alone and where the person filling the note in can actually see it. The
frontmatter is now plain, boring YAML that survives a round trip.

Two more defences, because one rule is not a system. `run.py templates` parses
every template and **builds a real `Note` from it** — the only test that would
have caught this — and `doctor` carries the same check as a failing line rather
than a warning. `run.py templates --repair` writes them all back.

## 2. Templates own the shape (`sb/render.py`, new)

Before this, `_templates/` was documentation: it described what a note ought to
look like, and `engine.py` built bodies in Python from scratch. Two sources of
truth for one thing, which is why they disagreed.

Now the engine hands over what it *knows* — a definition, an image embed, a list
of terms — and the template decides which headings exist, in what order, and
what prose sits between them. **Editing `_templates/Term.md` changes what the
capture box writes on the next capture**, with no restart and no code change;
the store is keyed on (mtime, size) like `Vault._parsed`, for the same reason.
There is a test that renames a heading in a template and checks the next note
comes out renamed.

What the template does **not** get to decide is `id`, `title`, `bucket`,
`created` or `updated`. A note whose bucket came from a file lj was halfway
through editing is a note in the wrong folder. What it *does* supply, per
objective, is `tags`, `category` and the review cycle — which is what makes "a
Term is tagged `term` and reviewed every 90 days" a fact about `Term.md` rather
than a constant in `engine.py`.

### Two rules that keep the output readable

**HTML comments are stripped from generated notes.** The guidance in these
templates is good and it is the reason lj wrote them — and it does not belong
six hundred times over in a vault of term notes, where it would outweigh the
content and make every card generated from those notes worse. So: `<!-- -->` is
for the human at the template, plain text is for every note made from it.

**A template supplies structure to text that has none, and never overrides
structure someone already wrote** (`render.wants_shape`). A line typed into the
capture box is exactly what a template is for. A nine-thousand-word document
dropped into `Drop/` already has an author's headings, and folding it under our
`## Definition` would bury them — the opposite of legible.

## 3. Ctrl+Alt+Z is an Atomic Note

`capture.default_template: Atomic Note` in config.yaml, and lj's reason is the
better one: it means **every captured note now arrives carrying the
`## In my own words` heading**, which is the one slot `sb/collected.py` reads to
tell a note that was used from a note that was merely kept. The debt check and
the capture default are the same idea approached from two ends.

Three templates were added for the note types phase 13 introduced and never gave
one: `Term.md`, `Session.md`, `Screenshot.md`. A session summary is no longer
rendered in Python — `summary_sections` returns content per heading and
`Session.md` decides the shape.

### One consequence, found by a test

Notes now ship with headings on them, and that moved two numbers in
`collected.py` that had to be moved back:

* `restatement()` returned the *first* `## In my own words` it found, which is
  now always the empty one the template put there. It scans for the first
  non-empty one instead.
* The word count counted headings and comments, so a 35-word capture would
  arrive as a 43-word note and cross the "worth processing" floor on the
  strength of its own scaffolding. `content_words` counts what the note says,
  not what is printed on it.

## 4. What is not done, and what to watch

1. **Project and Area bodies are still rendered in Python.** Their bodies are
   generated from tracked state — steps, materials, checkbox read-back, the
   preserve pass that stops the dashboard eating an Assignment's `## Answers` —
   and that logic cannot move into a template without losing it. Their templates
   were repaired to match what the renderer produces, so a hand-made Project
   still comes out right; but for those two, the template mirrors the engine
   rather than driving it. Closing that gap means teaching `render.py` about
   owned-versus-preserved sections, which is a phase of its own.
2. **Nothing stops the Properties editor doing this again.** The check catches
   it within a week and repair fixes it in one command, which is a great deal
   better than three weeks of silence, but the real prevention is one setting on
   lj's machine: **Settings → Editor → Properties in document → Source**. Not
   done here because it is not a file in this repo.
3. **`--repair` overwrites customisation.** It cannot tell a corrupted template
   from an edited one, so it says so before it runs and `init` still only writes
   what is missing.
4. **Existing notes are untouched.** Six hundred notes written before today keep
   the shape they were written in; this changes what gets written from now on. A
   re-render pass over the vault would be possible and is deliberately not
   automatic — rewriting every note in the vault on the strength of a template
   change is exactly the class of operation that ate `## Answers` sections
   before the preserve pass existed.

## 5. Files

New: `sb/render.py`, `_templates/Term.md`, `_templates/Session.md`,
`_templates/Screenshot.md`, `_templates/README.md`.
Changed: `sb/templates.py` (rewritten), `sb/engine.py`, `sb/session.py`,
`sb/collected.py`, `sb/config.py`, `run.py`, `config.yaml`,
`config.example.yaml`, all nine existing templates, `tests/test_all.py`
(+5 tests).
