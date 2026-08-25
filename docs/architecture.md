# Architecture and schema reference

This document closes the three open questions the blueprint left for build
time (§9) and records the decisions behind them.

---

## 1. Frontmatter schema

**Decision.** Every note carries a common block; bucket-specific state lives
under a namespaced key (`project:`, `srs:`, `habit:`, `review:`). Defined in
`sb/models.py`, which is the only place the schema exists.

**Why namespaced.** A learning Project carries `project:` *and* `srs:`. When it
graduates it keeps `srs:` and gains `review:`. Flat keys would collide the
first time two subsystems both wanted `status` or `due`; namespacing also means
a human reading the file in Obsidian can tell which subsystem owns which field.

### Common block — every note

```yaml
id: 20260822T164313-learn-rust-generics   # timestamp + slug; sortable, stable
title: Learn Rust generics
bucket: project                            # inbox | area | project | resource | archive
created: '2026-08-22T16:43:13-07:00'
updated: '2026-08-22T16:43:13-07:00'
tags: []
source: capture                            # capture | manual | graduation | restore
history:                                   # append-only audit trail
  - at: '2026-08-22T16:43:13-07:00'
    event: captured
    detail: bucket=project
```

`history` is what makes bucket moves reversible and legible: `captured`,
`parsed`, `step`, `completed`, `graduated`, `archived`, `restored`.

### `project:` — the six blueprint fields

```yaml
project:
  status: active            # active | blocked | graduating | done
  deadline: '2026-08-28'    # date/deadline
  estimate_minutes: 240     # time
  level: 3                  # level, 1-5
  skills: [rust, generics]  # skill list
  materials: ['The Rust Book ch.10']
  learning: true            # eligible for the graduate-to-Resource lifecycle
  ideal_end: 'Can write a generic container with trait bounds unaided.'
  steps:                    # steps
    - id: s1
      text: Read the Book ch.10
      minutes: 80
      done: false
      scheduled: '2026-08-24T09:00:00-07:00'   # set by the planner
```

`estimate_minutes` rather than a free-text duration: integers survive
round-tripping, "2h" does not. `ideal_end` is the blueprint's "defined ideal
end" (§2), kept as one checkable sentence.

### `srs:` — learning state (§4)

```yaml
srs:
  reps: 0
  lapses: 0
  ease: 2.5
  interval_days: 0
  due: '2026-08-23'
  last_review: null
  mastery: 0.0        # 0..1; the graduation prompt thresholds on this
  history: []
```

SM-2 shaped, with `mastery` as an explicit scalar so "ready to graduate?" has a
single number to fire on rather than a rule spread across the codebase. Written
at capture time for learning Projects; the review loop that updates it is
phase 2.

### `habit:` — Areas (§8)

```yaml
habit:
  cadence: weekly       # daily | weekly | monthly
  target_count: 3
  last_checkin: null
  checkins: []          # {date, decision: continue|change, new_target}
  log: []               # dates the habit actually happened
```

### `schedule:` — when an Area actually happens

An Area has no end state, so it never gets a deadline and never becomes a
task. It gets a recurring event instead — a real block of time that repeats on
the habit's cadence.

```yaml
schedule:
  enabled: true         # false = paused, series leaves the calendar, note stays
  time: '18:00'
  duration_minutes: 30
  days: []              # 0=Mon..6=Sun. Empty = derived from habit.target_count
  monthday: 1           # monthly cadence only
  start: null           # first occurrence; null = today
  until: null           # null = open-ended
```

`days: []` is the interesting default: set `target_count: 3` and the series
lands Mon/Wed/Fri without you choosing. Tick a day in the dashboard and `days`
is written explicitly, pinning the series so the target no longer moves it.

### `category:` — the colour keyword

A single top-level string (`hw`, `study`, `quiz`, `health`, `chore`, `fun`,
`work`, `admin`, `social`, `finance`, `general`). Left unset, `sb/taxonomy.py`
detects one from tags, title and body; set by hand it is never overwritten, so
a correction sticks. It drives the `.ics` `COLOR:`, the Google Calendar
`colorId`, the emoji prefix on tasks, and the swatch in the dashboard.

### `review:` — Resources (§2)

```yaml
review:
  cycle_days: 90
  next: '2026-11-20'
  last: null
```

---

## 2. Folder layout

```
00-Inbox/       unclassified captures — should stay near-empty
10-Areas/       ongoing responsibilities; habits live here
20-Projects/    deadline-bound work
30-Resources/   reference material; the default RAG corpus
40-Archive/     retired material; searched only on request
_system/        machine state — calendar/, index/, logs/
_templates/     Obsidian templates matching this schema
```

Numbered so Obsidian's explorer sorts them in PARA order. `_system/` is
underscore-prefixed so it sorts away; add it to Obsidian's excluded files.

Filenames are `{slug}--{timestamp}.md`. The slug keeps them readable in the
graph view; the timestamp suffix prevents collisions. The `id` in frontmatter
is what the system actually resolves by, so renaming a file by hand is safe.

A bucket change is a **file move**, not a flag change — the folder and the
frontmatter always agree, which is what lets Obsidian's own navigation stay
meaningful.

---

## 3. How the surfaces connect to the vault (§9, third question)

**Decision: direct filesystem access, no sync layer, no plugin dependency.**

The dashboard, the calendar and (later) the RAG index all read the same
markdown files through `sb/vault.py`. Considered and rejected:

- *Obsidian plugin API* — would make the system unusable when Obsidian is
  closed, and scheduled jobs need to run whether or not the editor is open.
- *A sync layer with its own database* — a second source of truth, and the
  first time it disagrees with the vault you have to decide which one is lying.

The cost of the filesystem approach is that a hand-edit in Obsidian isn't
noticed until the next read. In practice reads happen on every dashboard load,
so the window is seconds. A file watcher can be added later without changing
the model.

Every derived artifact is disposable by design: delete `_system/` entirely and
the next run rebuilds the calendar and the index from the notes.

---

## 4. The parse pipeline

```
raw capture
   │
   ├─ extract.project_prior()   rules: deadline, duration, bullet steps, title
   │
   ├─ LLM (Ollama)              judgement: step breakdown, skills, level,
   │                            ideal end — shown today's date and the prior
   │
   └─ parser._merge()           validate every field; anything the model
                                mangled falls back to the prior
```

**Why rules first.** Dates are the field a small local model gets wrong most
often and plain code gets right most often. Telling the model what the date
parser found, and overriding it when the model returns something unparseable or
in the past, is the difference between a system you trust and one you
double-check.

**Guards in `_merge`:** dates before today are rejected as hallucinations; the
headline estimate is replaced by the sum of the steps when they disagree by
more than 2.5×; `level` is clamped to 1–5; string-instead-of-list is coerced;
step objects and bare strings are both accepted.

**Degraded mode.** With no model reachable, `ParseResult.degraded` is set, the
UI flags the capture in amber, and the rule-based metadata is written anyway.
Re-parse from the dashboard once the model is back.

---

## 5. Scheduling

`workflow.plan_project` lays outstanding steps into working windows from
`config.planner`, respecting:

- working hours and workdays,
- a daily cap on scheduled deep work (`max_minutes_per_day`),
- **blocks already committed by other Projects** — the planner is given the
  whole vault's commitments, so three projects don't all claim Monday 9am,
- existing future slots, which are left alone unless you press Re-plan.

If the work doesn't fit before the deadline the plan still happens and
`PlanReport.overflowed` says so. Silently compressing the estimate to make it
fit would hide exactly the information you needed.

---

## 6. Calendar

`calsync/events.py` derives two streams from the vault, and the split is the
whole design:

**Events — things that occupy time.**

| kind | source | shape |
|---|---|---|
| `block` | a scheduled Project step | timed, one-off |
| `area` | an Area's `schedule:` | timed, **recurring** (RRULE) |
| `review` | a Resource's `review.next` | all-day prompt |
| `schedule-review` | one per system, if any Areas exist | timed, weekly |

**Tasks — things that are due.** Exactly one per active Project with a
deadline. Nothing else in the system produces one: an Area has no end, a
Resource has no deadline, and an Archive entry has neither.

A deadline used to be an all-day event. That is how a calendar acquires a row
of banners meaning "not today, but soon" — unactionable, and easy to scroll
past. As a task it sits in the task list, carries a priority derived from
level and days-remaining, tracks step progress as a percentage, and stays put
until it is ticked.

### Sinks

| | `.ics` | `google` | `gtasks` |
|---|---|---|---|
| Carries | events + VTODO | events | tasks |
| Setup | none | OAuth client + consent | same token |
| Works offline | yes | no | no |
| Updates in place | file is rewritten | upsert by event id | upsert by `[sb:…]` marker |
| Colour | `COLOR:` (RFC 7986) | `colorId` | emoji only — no colour API |

`calendar.sink` picks the event sinks; `calendar.task_sink` picks the task
sinks and defaults to `auto`, meaning "follow `sink`". Every item has a stable
id derived from the note, so re-syncing updates rather than duplicates. The
Google event sweep still lists the retired `deadline` and `habit` kinds, which
is what migrates an existing calendar: they are found, absent from what the
vault now wants, and deleted.

### Colour

`sb/taxonomy.py` holds one table — keyword lists, emoji, a CSS3 colour name, a
hex, and a Google `colorId` per category — and every colour decision reads it.
Matching is weighted: an explicit `category:` wins, then a tag, then the
title, then the body. Nothing lands uncoloured; unmatched notes get `general`,
because an uncoloured item on a colour-coded calendar reads as a bug.

Google Tasks has no colour API at all, so a task's category survives only in
its title. That is what the emoji prefix is for.

---

## 7. Phase 2 hooks already in place

- `srs:` frontmatter is written on learning Projects at capture time.
- `ProjectStatus.GRADUATING` exists for the SR engine to set before the
  human confirms; `engine.move(id, "resource")` already logs `graduated`.
- `review.next` is set on every Resource, and the review event already fires
  on the calendar — only the answer-back UI is missing.
- `OllamaProvider.embed()` is implemented for the RAG index; `_system/index/`
  is created and gitignored.
- `Vault.notes(bucket)` is the corpus selector the RAG layer will use to
  honour "Resources by default, Archive only on request" (§7).
