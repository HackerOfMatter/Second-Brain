# Scrum Backlog — Everyday Use (lj's daily driver)

Written 2026-08-29. Sibling to `backlog_public_release.md`, which is about
strangers. This one is about **one user actually using it every day**, which is
the prerequisite: a system nobody uses daily cannot be validated, tuned, or
sold.

## 0. Where the system actually is

The roadmap (Tiers 0–4) is *built*. The system is *not in use*. Measured on
lj-studio, 2026-08-29:

| Signal | Value | What it means |
|---|---|---|
| Notes in vault | 687 | 677 in `30-Resources`, 7 Projects, 2 Areas, 0 Archive |
| Decks | **0** | No card has ever been generated |
| Reviews logged | **0** | FSRS, calibration, retention dial, fit — all fed by nothing |
| Labelled examples | **0** | `AUTO_FLOOR` and the intake floor remain guesses |
| Timed steps | **0** | Estimate multiplier inert |
| Habits with an implementation intention | **0 of 2** | The largest lever in the build, unused |
| Areas | 2, one titled `1` | Test captures, not real commitments |
| Projects | 7, incl. `test-task`, `example-task` | Same |
| Git | 1 commit, ~20 modified files uncommitted | Two sessions edit this tree |
| `test-log.bat` on Windows | never run | Still the open item from Phase 11 |

**The bottleneck is not features. It is that nothing in the vault is real yet,
and the app is not present at the moments of the day when it would be used.**

**Product goal (D1 — "Daily Driver")**: for 21 consecutive days, lj captures
without thinking about it, reviews cards every day, and every Area has a cue.

**Exit criteria for D1**
- ≥ 200 reviews logged, ≥ 14 days with at least one session
- ≥ 3 real Projects live with steps, deadlines, and calendar blocks
- 2+ Areas with implementation intentions and ≥ 2 weeks of occurrences
- 0 unhandled errors in `_system/logs` over the final 7 days
- lj can answer "what do I do next?" from the dashboard alone

Legend — Pts: Fibonacci, 1 ≈ half a day. Priority: M/S/C/W (MoSCoW for D1).

---

## 1. Epics

| ID | Epic | Why it blocks daily use | Pri | Pts |
|---|---|---|---|---|
| A | Trust the build (green suite on Windows, git history) | You cannot rely daily on something never verified where it runs | M | 13 |
| B | Always-on (autostart, tray, health, backups) | An app you have to launch is an app you skip | M | 13 |
| C | Vault reset & real content | 677 unsorted notes and `test-task` make the dashboard unreadable | M | 21 |
| D | First real study loop (decks from actual material) | The whole learning half is unexercised | M | 21 |
| E | Habits made real (intentions, cues, check-ins) | Highest-effect feature, zero adoption | M | 13 |
| F | Capture friction to zero (hotkey, phone, Obsidian) | A thought not captured in 5s is lost | M | 13 |
| G | The daily surface (one screen answering "now what?") | Today the answer is spread over 3 pages | M | 21 |
| H | Notifications that arrive (calendar, Tasks, reminders) | The system must speak first | S | 13 |
| I | Data flywheel (labels, timings, thresholds) | Every guessed constant stays guessed until used | S | 13 |
| J | Failure is visible and recoverable | Silent failure kills a daily habit fast | S | 13 |
| K | Tutor/RAG made worth asking | 677 resources indexed and never queried | S | 13 |
| L | Weekly review as a real ritual | The loop that closes all other loops | S | 8 |

---

## 2. Sprint 1 — "It runs, and it is not a test vault" (Must)

| ID | Story | AC | Pts |
|---|---|---|---|
| A1 | Run `test-log.bat` on lj-studio and fix what it reports | `_system/logs/test-run.txt` exists, full suite green on Windows, log pasted into a build log | 3 |
| A2 | Commit the working tree; adopt a commit-per-session rule | `git log` > 1 commit; `.gitignore` excludes `_decks`, `_labels`, `_system`, `.venv`, `config.yaml` | 2 |
| A3 | Exclude `_decks/`, `_labels/`, `_system/` from OneDrive sync | Verified: no `.tmp`/conflict files appear during a 50-card session | 2 |
| A4 | Empty `_to_delete/` and stale backups | Vault size drops; no backup older than the last 2 phases | 1 |
| B1 | `start.bat` runs at login, minimized; tray/pinned shortcut | Reboot → dashboard reachable at 127.0.0.1:8787 with no manual step | 3 |
| B2 | Nightly backup of `_decks`, `_labels`, `_reviews.jsonl` to a dated zip | 7 rolling copies; restore tested once | 3 |
| C1 | Purge test captures (`1`, `test-task`, `example-task`, `Untitled.md`) | 0 notes whose title is a placeholder; `run.py lint` clean of vague-title in Areas/Projects | 2 |
| C2 | Triage `30-Resources` (677 notes): keep / archive / split | ≤ 150 active Resources; the rest in `40-Archive`; orphan count reported by `doctor` halved | 8 |
| C3 | Create 3 real Projects with all six metadata fields + steps | Each appears on the calendar with blocks; `run.py` workflow shows a "next step" | 3 |

**Sprint goal:** open the dashboard on any day and see only real things.

---

## 3. Sprint 2 — "The loop runs daily" (Must)

| ID | Story | AC | Pts |
|---|---|---|---|
| D1 | Generate decks for 3 real Resources; review the cards by hand | ≥ 60 cards exist; every card passes `quality.py`; lj deleted/edited < 20% | 5 |
| D2 | Study session scheduled daily at a fixed time + place (the system's own implementation intention) | Calendar event exists; 7/7 days it fires | 2 |
| D3 | 14 consecutive days of reviews | ≥ 200 lines in `_reviews.jsonl`; `doctor` fsrs line shows progress | 5 |
| D4 | Confidence tap used on every card | ≥ 20 predictions; Brier score renders; overconfident band non-empty and promoted | 3 |
| D5 | Session-length guardrail | A session caps at N minutes/cards so a bad day does not break the chain | 2 |
| E1 | Write implementation intentions for both Areas ("When X, I will Y at Z") | `doctor` habits line stops flagging; the sentence appears in the calendar reminder body | 2 |
| E2 | Add anchor + friction fields; log occurrences with time/place | Context stability line renders after 6 occurrences | 3 |
| E3 | Never-miss-twice surfacing on the dashboard | Consecutive-miss count visible; no streak counter anywhere | 3 |
| F1 | Global capture hotkey (Windows) posting to `/api/capture` | Ctrl+Alt+Space → box → filed in < 5s, works when the app is closed (Drop fallback) | 5 |
| F2 | Enable and hotkey the Obsidian capture plugin | Captures from inside the editor; `doctor` obsidian line reads enabled | 1 |
| F3 | Phone capture path (email-in, shortcut, or Google Tasks inbox → Drop) | A capture made on a phone appears in `00-Inbox` within 15 min | 5 |

**Sprint goal:** 7 days where lj captures, reviews, and hits both habits without opening a terminal.

---

## 4. Sprint 3 — "It speaks first, and it is legible" (Should)

| ID | Story | AC | Pts |
|---|---|---|---|
| G1 | "Today" screen: due cards, next project step, habits due, inbox count | One page answers "what now?"; loads < 500 ms | 8 |
| G2 | Inbox zero flow: keyboard-only triage of `00-Inbox` | 20 items classified in < 3 min | 5 |
| G3 | Streak-free progress panel (reviews/day, minutes, calibration) | Uses `retention.py` to show projected daily minutes | 5 |
| H1 | Verify Google Calendar + Tasks round trip end to end | Tick on phone → project step marked done in vault on next sync | 3 |
| H2 | Reminder copy carries the intention/cue, not just a title | Event body renders "When X, I will Y at Z" | 2 |
| H3 | Desktop notification when a study session is due and unstarted | Fires once, snoozeable, never twice in an hour | 3 |
| J1 | Error banner in the UI when a background job failed | Ollama down / calendar auth expired shows on the dashboard, not only in a log | 5 |
| J2 | `doctor` runs weekly and writes a dated report | `_system/logs/doctor-YYYYMMDD.txt`; surfaced in the weekly review | 2 |
| J3 | Ollama-not-running degradation path is honest | Capture still files; card generation queues rather than failing silently | 3 |

**Sprint goal:** the system initiates, and when it breaks lj knows within a day.

---

## 5. Sprint 4 — "The numbers stop being guesses" (Should)

| ID | Story | AC | Pts |
|---|---|---|---|
| I1 | Accumulate 40 intake + 40 connect labels through normal use | `run.py thresholds` produces a recommendation instead of a refusal | 5 |
| I2 | Adopt the recommended floors; record the before/after | `config.yaml` updated with a comment naming the sweep date and precision | 2 |
| I3 | Time 5+ project steps; enable the personal multiplier | Estimates render as `90 min = 45 × 2.0 (your last N steps)` | 3 |
| I4 | Log the text of cards `quality.py` drops | `_labels/dropped_cards.jsonl`; reviewed before any limit is tuned | 3 |
| K1 | Ask the tutor 20 real questions against the trimmed Resources | Answer cites notes; failures categorized (retrieval vs generation) | 5 |
| K2 | Fix the top retrieval failure mode found in K1 | Named fix, test, before/after on the same 20 questions | 5 |
| K3 | Index freshness: reindex on note change, not on demand | A note edited in Obsidian is searchable within 60s | 5 |
| L1 | Weekly review scheduled Sunday, done 4 weeks running | 4 dated entries; each closes habits, resources, and graduation prompts | 3 |
| L2 | Graduation loop exercised once end to end | One Project graduates to Resource on evidence + confirmation | 3 |

**Sprint goal:** every constant in the system is either measured or explicitly labelled provisional.

---

## 6. Backlog (not committed to D1)

- Retention dial exposed in the UI with projected daily minutes (needs 200+ reviews first)
- FSRS weight fit — gated at 1,000 reviews, do not touch before
- numpy index path — gated at 5,000 chunks (currently ~400)
- Interleaved practice by problem type inside a subject (needs typed cards at volume)
- Worked-example fading tuning (needs D1 review data)
- PWA / mobile review surface
- Anything from `backlog_public_release.md` — deferred until D1 exit criteria are met

---

## 7. Definition of Ready

A story is ready when it names: the moment in lj's day it changes; an
acceptance criterion observable from outside the code; what happens when it
fails; and whether it needs data that does not exist yet (if so, it is blocked,
not ready).

## 8. Definition of Done

Suite green **on Windows** via `test-log.bat`; committed with a message;
`doctor` re-read; no new orphan/lint smells; the build log updated in the
project; and the feature used once, for real, on a real note — not a fixture.

## 9. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Building instead of using — the failure mode of the last 12 phases | D1 never starts | Sprints 1–2 contain almost no new features; C2/D3/E1 are usage, not code |
| 677 unsorted Resources make every surface noisy | Abandonment | C2 is a Must and is scheduled first |
| Two sessions editing the tree | Silent loss | A2 commits; re-stage from device before every write |
| Ollama/GPU contention during a session | Reviews stall, chain breaks | Study lane keep-alive already set; J3 makes failure honest |
| Habit tracking becomes self-judgment | Quits | Never-miss-twice only; no streak counter (E3) |
| Capture friction on phone unresolved | Half of captures lost | F3 is a Must with three acceptable implementations |
