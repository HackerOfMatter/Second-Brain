# Second Brain

A local, privacy-first PARA capture and execution engine on top of your
Obsidian vault. Nothing leaves your machine: the vault is plain markdown on
disk, the model runs in Ollama, and the server binds to `127.0.0.1`.

This is **phase 1** of the blueprint — the spine everything else hangs off:

    capture → you classify → metadata parsed → note written → steps scheduled → calendar

## Install (Windows)

```powershell
cd path\to\secondbrain
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

copy config.example.yaml config.yaml
notepad config.yaml          # set `vault:` to your Obsidian vault path
```

Then pull a model (you already have Ollama):

```powershell
ollama pull llama3.1:8b
ollama pull nomic-embed-text     # for the RAG phase
```

Check the wiring, create the folders, and start:

```powershell
python run.py doctor
python run.py init
python run.py
```

The UI opens at <http://127.0.0.1:8787>.

`doctor` tells you if Ollama is down or the configured model isn't pulled. If
it is down, captures still work — they fall back to rule-based parsing and get
flagged so you can re-parse later.

## Using it

Type into the box, then press the button — **Area**, **Project**, or
**Resource**. Classification is yours; everything after it is automatic.
Keyboard: `c` focuses the box, `1`/`2`/`3` file it, `Ctrl+Enter` files as a
Project.

A Project capture unpacks into the six blueprint fields — time, deadline,
level, skills, steps, materials — gets its steps laid into your working hours
around everything else already scheduled, and lands on the calendar.

Try:

```
Learn Rust generics by next Friday, about 4 hours
- read the Book ch.10
- work the exercises
- write a small generic container demo
```

### Calendar: time on the grid, due dates in the task list

Two streams, because they answer different questions.

**Events — when am I doing this.** Project work blocks land at real times in
your working hours. Areas get a *recurring* block: capture "Gym", set it to
3× a week, and it repeats Mon/Wed/Fri at the time you pick. Nothing about an
Area ever falls due.

**Tasks — when must this be finished.** Only a Project has a deadline, and it
becomes a task: a `VTODO` in the `.ics`, a Google Task in the Tasks strip of
Google Calendar. It carries a priority from the project's level and how close
the date is, tracks step progress, and stays visible until you tick it. It
does not take an hour of grid space to say "not today, but soon".

**Colour by keyword.** Every note picks up a category from its own words —
`quiz`, `hw`, `study`, `health`, `chore`, `fun`, `work`, `admin`, `social`,
`finance` — and that drives the event colour, the emoji on the task, and the
swatch in the dashboard. Override it from the dropdown on any card, or teach
it your own words in `calendar.categories`. (Google Tasks has no colour API,
which is why tasks carry an emoji rather than a hue.)

**Changing an Area** is the point of the Sunday *schedule review* — one weekly
event covering every Area, rather than a check-in banner per habit. The
dashboard's Areas panel is where the change actually happens: time, length,
days, or pause without deleting.

`calendar.sink: ics` (the default) writes `<vault>/_system/calendar/secondbrain.ics`,
rebuilt from vault state after every change.

- **Google Calendar**: Settings → Import & export → Import that file, or set
  `calendar.sink: google` for live push of events *and* due dates (see
  [docs/google-calendar.md](docs/google-calendar.md)).
- **Outlook / Apple Calendar**: subscribe to the file path directly and it
  stays current — both understand `VTODO`, so due dates arrive as real todos.
- **Google Calendar import ignores `VTODO`.** If you are on the `.ics` route
  only, due dates live in the file but not in Google's UI; that is exactly the
  gap `task_sink: google` closes.

### Dates: guessed, then approved

Every due date on the dashboard is a real date picker — the OS calendar, not a
typed string. Full notes: [docs/dates.md](docs/dates.md).

The parser reports **how** it knows a date, not just what it read. A date the
text names — `sept 8`, `2026-09-01`, `by 9/8`, `due: 2026-12-01` — arrives
confirmed. So does one with a single sane reading: `tomorrow`, `in 2 weeks`,
`by eod`, `this friday`. A date it had to interpret — "end of the week", "next
Friday", "in a month" — does not, and lands in a **Confirm these dates** panel
that quotes the words it came from. Those still reach the calendar; a
forgotten confirmation shouldn't mean no reminder. They just get a glance.

The same applies to the model: if it returns a date the rules didn't, that is
kept but never auto-confirmed. An 8B model quietly rewriting `sept 8` into a
different Tuesday is exactly what that catches.

There is also a Due picker beside the capture buttons. Set it and it beats
anything in the text, no confirmation needed.

### Study: flashcards and spaced repetition

The tutor lives at **http://127.0.0.1:8787/study**, or the Study button on the
dashboard. Full notes: [docs/tutor.md](docs/tutor.md).

**Cards come from your notes.** Pick a Project or Resource, press Generate,
and the local model drafts questions one passage at a time, quoting the
sentence each answer came from. Everything lands as a **draft** until you have
read it — a wrong flashcard is worse than no flashcard, because spaced
repetition will drill the error into you on a perfect schedule.

**Scheduling is FSRS-5**, the algorithm Anki ships today. It tracks memory
strength, intrinsic difficulty and current recall probability separately,
which is what stops one bad week from branding a card hard forever. The four
buttons show what each would cost before you press it.

**Subjects are mixed.** One session pulls from every deck with something due
and spreads them out. Studying one deck at a time feels more productive and
remembers worse. Tick specific subjects to narrow it, or set
`study.interleave: false` for the comfortable version.

**Two ways to answer.** Reveal-and-self-grade like Anki, or type the answer
from memory and have it marked. Marking and grading are separate steps: the
model proposes, you dispose, and nothing is written until you accept.

**Decks live in `<vault>/_decks/`**, one markdown file per note. Frontmatter
owns the scheduling; the body owns the card text, so you can rewrite a
question, add a card, or delete one directly in Obsidian. Unlike `_system/`,
that folder is **not disposable** — it holds your review history.

**Graduation** (blueprint §4): when a learning Project's deck is 85% mature,
the dashboard asks whether it should become a Resource. It never moves on its
own, and the cards come with it.

### Two models, two jobs

Every model call names a job, and the job picks the model. Full notes:
[docs/models.md](docs/models.md).

```yaml
llm:
  model: llama3.1:8b     # captures, parsing — runs while you wait
  study_model: phi4      # flashcards, marking, explaining, asking
```

The split is about what each job needs, not which feels important. Parsing runs
on every capture while you watch, and the date rules have already settled the
part most likely to be wrong — small and quick wins. Card generation writes
something permanent that spaced repetition will drill into you; marking a typed
answer is a judgement about meaning; explaining and asking are teaching. None
of those run mid-sentence, so quality wins.

`study_model: ""` runs one model for everything. Moving a job between lanes is
a `study_roles` edit, not a code change.

A 12GB card cannot hold an 8B and a 14B at once, so the lanes take turns —
which is why they have different keep-alive times. A capture is one call and
releases its model in five minutes; a review session is fifty calls and holds
on for thirty. If you name a model you have not pulled, the study lane falls
back to the fast one and `doctor` tells you which `ollama pull` to run.

### Ask your notes

The **Ask** tab on the study page answers a question from everything you have
filed, with citations. Full notes: [docs/ask.md](docs/ask.md).

It answers **from your notes, not from the model's training** — retrieval runs
first, and when nothing matches the answer is "nothing in your resources covers
that" rather than a fluent paragraph. Every claim is cited, the citations are
clickable, and sources that were retrieved but not used are dimmed.

**Archive is opt-in.** Archiving something is a decision that it is no longer
relevant; searching it anyway would make the bucket meaningless. Tick the box
to widen the search.

The index lives in `_system/index/` and is disposable — it all derives from
your notes. It rebuilds incrementally: edit one note and one note is
re-embedded. With Ollama closed it falls back to keyword search, clearly
labelled, rather than failing.

### Reviews and habits — the loops that close

**"Still needed?"** Every 90 days a Resource comes up for review. The answer
happens on the dashboard: *Still need it* (stamps it and pushes the next check
a full cycle out), *Ask later* (a short push, explicitly not a decision), or
*Archive*. Archiving is a move, never a delete — the Archive panel restores in
one click, and flashcards come along in both directions.

**Habits.** The Areas panel carries the weekly check-in's actual question:
continue at this rate, or change it. Set the target count and the cadence right
there. While the days are unpinned, raising the target re-spreads them
(3× a week is Mon/Wed/Fri, not Mon/Tue/Wed); tick a day by hand and the series
pins, so the target stops moving it.

### Command line

```powershell
python run.py capture "Fix the printer driver by tomorrow" --bucket project
python run.py next        # the execution queue, ranked by urgency
python run.py sync        # rebuild the calendar
python run.py doctor      # health check
```

## How it's put together

| Module | Job |
|---|---|
| `sb/models.py` | The vault schema — the frontmatter contract for every bucket |
| `sb/vault.py` | Obsidian I/O; atomic writes, bucket moves, audit trail |
| `sb/extract.py` | Rule-based extraction: dates (with confidence), durations, steps, titles |
| `sb/parser.py` | Capture → Project metadata (rules → model → validation) |
| `sb/workflow.py` | Step scheduling and the ranked "what's next" queue |
| `sb/calsync/` | Calendar sinks: `.ics` writer and Google Calendar |
| `sb/fsrs.py` | The FSRS-5 scheduler — stability, difficulty, retrievability |
| `sb/cards.py` | Decks and cards; the `_decks/` store and the review log |
| `sb/generate.py` | Note → flashcards (chunk → model → citation check) |
| `sb/tutor.py` | Session assembly, marking, progress, mastery |
| `sb/index.py` | The retrieval index — chunking, embedding, cosine search |
| `sb/ask.py` | Grounded answers with citations (the info manager) |
| `sb/llm/` | Ollama (default), cloud (opt-in), heuristic (fallback); per-role model routing |
| `sb/engine.py` | The application service the UI and CLI both call |
| `sb/api.py` | Starlette routes + the local web UI |

Design notes and the schema reference: [docs/architecture.md](docs/architecture.md).
The tutor in detail: [docs/tutor.md](docs/tutor.md).
How dates are read and approved: [docs/dates.md](docs/dates.md).
The info manager: [docs/ask.md](docs/ask.md).
Which model does which job: [docs/models.md](docs/models.md).

Three principles worth knowing before you change anything:

1. **The vault is the only source of truth.** Delete `_system/` and everything
   rebuilds. Edit a note by hand in Obsidian and the system picks it up.
2. **The model is never trusted with structure.** Rules extract dates first,
   the model fills in judgement, and every field is validated on the way back.
   A wrong deadline is worse than no deadline.
3. **A capture is never lost.** Model down, malformed JSON, calendar
   unreachable — the note still gets written.

## Tests

```powershell
python tests/test_all.py
```

655 checks, no test framework needed. They cover frontmatter round-tripping,
date parsing, LLM-output coercion, planner arithmetic, keyword→colour
matching, the `.ics` wire format (VEVENT, VTODO and RRULE), area recurrence,
both Google sinks against a fake service, and a full capture → note → move →
archive → restore cycle.

For the tutor: the FSRS curve's identities and its ordering properties (a
lapse never raises stability, Hard < Good < Easy, the spacing effect), deck
files round-tripping through hand edits, the generator's rejection rules
(fabricated quotes, restated answers, duplicates), session interleaving and
the daily caps, the marking fallbacks, streak arithmetic, and the three
mastery gates — plus an end-to-end pass through the HTTP layer from capture to
graduation.

For dates: the certain/ambiguous split across every phrase the parser knows,
all three parser bug fixes, the fraction and page-range rejections, model
drift, and the approval flow end to end. Plus a concurrency regression —
eight simultaneous writes to one note — found by driving the date pickers in a
real browser.

For the info manager: chunking with heading trails and windowed overlap,
incremental re-embedding (edit one note, one note is re-embedded), pruning
deleted notes, refusing a vector file that does not line up with its chunks,
the archive opt-in in both directions, the per-note cap, citation parsing, and
the refusal path — an unanswerable question must not reach the model at all.

For the loops: keep, snooze and archive with their separate bookkeeping,
flashcards surviving an archive round-trip, and the target-count/pinned-days
interaction.

For the model lanes: role-to-model resolution and its fallbacks, `:latest` tag
matching, a stopped server not being mistaken for a missing pull, and a routing
test that asserts every call site really does name its own role — otherwise the
whole split is decorative.

## What's next

The blueprint's definition of done (§10) is met. What is left is refinement
rather than missing parts:

- **Reading tasks back** — ticking a due task in Google Tasks should close the
  project. Today the flow is one-way, vault → Google.
- **FSRS weight fitting** from `_decks/_reviews.jsonl`, once there are a few
  thousand reviews to fit against. Every log line already records the state
  before the answer, which is exactly what an optimizer needs.
- **numpy for the index**, if the vault ever passes a few hundred notes. Pure
  Python cosine is fine up to roughly 5,000 chunks and honest about it.
- **Obsidian plugin** — a capture modal so you never leave the editor.
