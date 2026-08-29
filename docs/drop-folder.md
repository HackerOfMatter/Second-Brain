# The Drop folder

Put a note you already wrote into `Drop/`. The system reads it, decides
whether it is a **Project**, an **Area** or a **Resource**, and files it with
the same treatment a capture of the same text would have got.

    C:\Users\lukeb\secondbrain\Drop\        <- put files here
    C:\Users\lukeb\secondbrain\Drop\_filed\ <- your originals, after filing
    C:\Users\lukeb\secondbrain\Drop\_problem\ <- anything unreadable

Accepted: `.md`, `.markdown`, `.txt`, `.docx`.

## When it runs

* **By itself**, every 20 seconds, while the app is open (`intake.watch`).
* **On the button** — *File dropped notes*, top right of the dashboard. The
  badge next to it counts files waiting.
* **From a shortcut** — `file-notes.bat`, or `python run.py intake`. Add
  `--dry-run` to see where things would go without moving anything.

## What happens to a filed note

Exactly what happens to a capture, because it is the same code path
(`Engine._apply_bucket`):

| Filed as | What it gets |
|---|---|
| Project | Parsed into the six metadata fields, steps planned into work blocks, a due task on the calendar |
| Area | A habit, a recurring calendar block, a place in the weekly schedule review |
| Resource | A review date, and a place in the RAG corpus at the next reindex |

## When it is not sure

The note lands in `00-Inbox` carrying its suggestion, and the dashboard asks
you under **Where does this go?** — three buttons, and the note gets the full
treatment the moment you pick one. Nothing sits in a folder waiting to be
noticed.

The line between filing and asking is `intake.auto_floor` (0.6). Confidence is
`strength × separation`:

* **strength** — how much evidence there is at all.
* **separation** — how far ahead the winner is. A note with strong evidence
  for *two* buckets is not a confident call, and a scheme that looked only at
  the winner's total would file it as one.

So the Inbox catches the notes a person would also have hesitated over. Raise
`auto_floor` to be asked more often; lower it to be asked less.

## How the decision is made

Rules first, model second — the same shape as `parser.py` and `connect.py`.

1. `intake.score()` tallies signals: task verbs, assignment words, checklists
   and a parsed deadline for Project; recurrence phrases, per-period counts
   and habit words for Area; explanatory phrasing, links, cited chapters and
   long-form shape for Resource. Every hit is recorded, which is why the
   dashboard can tell you *why*.
2. Only if that comes out below the floor is the model asked — one small
   question, one call. Agreement raises confidence; disagreement is taken but
   discounted, so a hedging model cannot push a note past the floor alone.
3. `intake.use_model: false` skips step 2 entirely. Intake still works; an
   undecided note simply goes to the Inbox without a second opinion.

Notes this system rendered are scrubbed of their own boilerplate before
scoring — a note exported from the vault and dropped back in must not be
classified by *our* wording, which is the mistake phase 1 made with the colour
table.

## Rules of the folder

* **Nothing is deleted.** The original moves to `_filed/` with a timestamp, or
  to `_problem/` if it could not be read. Clear those out yourself when you
  trust the results.
* **Only the top level is read.** A subfolder you made is a subfolder you are
  still organising.
* **A file still being written is left alone** until it has been still for
  `intake.settle_seconds` — Word and OneDrive both write in stages.
* **Two runs never overlap.** The watcher and the button take a lock, so a
  file cannot be read twice and filed as two notes.
* **The `.docx` reader needs no install.** A .docx is a zip with XML inside;
  headings and bullets are read straight out of it. python-docx is used as a
  fallback if it happens to be installed and the direct read comes up empty.

## Configuration

```yaml
intake:
  folder: Drop
  auto_floor: 0.6       # below this, ask instead of filing
  use_model: true       # only for notes the rules could not call
  settle_seconds: 5     # ignore files touched more recently than this
  watch: true           # file dropped notes while the app is open
  poll_seconds: 20
  max_per_run: 25
```

## Where the code is

| File | What it holds |
|---|---|
| `sb/intake.py` | Reading dropped files, the scoring table, the confidence rule, the model tie-break |
| `sb/engine.py` | `intake()`, `classify_note()`, `_apply_bucket()`, `ensure_drop_folder()` |
| `sb/api.py` | `POST /api/intake`, `POST /api/notes/{id}/classify`, the folder watcher |
| `sb/web/index.html` | *File dropped notes*, the **Filed from Drop** panel, the **Where does this go?** queue |
