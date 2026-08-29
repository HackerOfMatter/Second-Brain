# Verifying the round trip by hand

Story H1. Everything else in this system can be proved from a test. This one
cannot, because the far half of it is a Google account and a phone in lj's
pocket, and no test can tick a checkbox there.

So this file is the other half of the proof. Work down it once; each step
names the command to run and the single thing to look at afterwards. It takes
about ten minutes, most of which is waiting for a phone to sync.

**What is already proved without you.** `tests/test_all.py` drives the entire
round trip against a stubbed Tasks client — push, tick, read back, apply,
delete — and asserts the result by re-reading the note *off the disk*:

```
google tasks round trip: a tick on the phone reaches the vault
  ok   the due date reached Google Tasks
  ok   its uid maps back to the note
  ok   the tick was read back before the push
  ok   every step is marked done in the vault
  ok   the project is closed in the vault
  ok   and it is in the frontmatter on disk, not just in memory
  ok   the finished task is then removed from Google
```

**What is not proved, and only you can prove.** That the real Google Tasks API
answers the way the stub does, and that lj's token still carries the `tasks`
scope. Those are the two things below.

---

## 0. Preconditions (30 seconds)

```powershell
python run.py doctor
```

Look for the Google line. It reports what it *read from disk* and says so —
it does not claim Google has accepted the token, because nothing has asked
Google yet. Then check `config.yaml`:

```yaml
calendar:
  sink: both              # or google
  task_sink: auto         # auto follows sink, so this resolves to google
  google_tasklist: Second Brain
  read_back_completions: true
```

`read_back_completions: false` turns the inbound direction off entirely. If it
is off, nothing below will work and that is correct behaviour.

## 1. Push a due date out

Pick a project with a deadline, or make one:

```powershell
python run.py capture --bucket project "Round trip check by tomorrow
- step one
- step two"
python run.py sync
```

**Observable:** the last lines of `sync` say

```
read-back: 0 ticked task(s) on Google, 0 applied to the vault
{'sink': 'both', 'result': {..., 'gtasks': {'tasklist': '...', 'created': 1, ...}}}
```

`created: 1` is the push. `read-back: 0 … 0` is the pull finding nothing yet,
which is the right answer — nothing has been ticked.

If instead you see `read-back FAILED (RefreshError)`, the token is stale: delete
`_system/token.json`, run `python run.py sync` again, and complete the consent
screen. A failure here also raises a banner on the dashboard, so you would have
found out anyway.

## 2. See it on the phone

Open **Google Tasks** (or the Tasks strip in Google Calendar) on the phone, in
the list named by `google_tasklist` — "Second Brain" by default.

**Observable:** a task titled with the project's name and its category emoji,
due on the deadline. Open it: the notes hold the implementation intention (if
the note has one), the step progress, the remaining steps, and a trailing
`[sb:…]` marker. That marker is how the sink recognises its own work — a task
you typed in yourself has none and is never touched.

## 3. Tick it — on the phone, not in the app

Tick the checkbox. This is the whole point: it is the only edit that exists on
a phone, and it is the only field on which Google is allowed to beat the vault.

## 4. Read it back

Wait for the phone to sync (usually seconds), then, on the PC:

```powershell
python run.py sync
```

**Observable:**

```
read-back: 1 ticked task(s) on Google, 1 applied to the vault: 20260829T...-round-trip-check
```

## 5. Confirm it landed in the vault, not just in the log

This is the acceptance criterion, so check the file itself rather than the
dashboard:

```powershell
type "20-Projects\round-trip-check--*.md"
```

**Observable, in the frontmatter:**

- every entry under `steps:` has `done: true` and a `done_at:` timestamp;
- `status: done` — unless the project is a *learning* project, which stays
  `status: active` on purpose. A tick on a due-date reminder means the work is
  finished, not that the material is known; graduation is a separate,
  confirmed decision (blueprint §4);
- a `history:` line reading `event: completed`, `detail: ticked on Google Tasks`.

Open the dashboard and the project shows every step checked.

## 6. Confirm it does not come back

```powershell
python run.py sync
python run.py sync
```

**Observable:** `read-back: 0 ticked task(s) … 0 applied` and no new `history:`
line in the note. For a finished (non-learning) project the task is also gone
from Google Tasks — the vault no longer asks for it, so the push swept it. For
a learning project the task is still there and **still ticked**: the push must
never patch `status` back to `needsAction`, and if you find the checkbox has
cleared itself, that is the bug this step exists to catch.

---

## What each direction is allowed to win

The merge is per-field, not last-writer-wins:

| Field | Winner | Why |
|---|---|---|
| title, notes, due date, progress | the vault | derived from the note and rewritten on every push |
| completion | Google | ticking is the only edit a phone offers, and a tick that gets silently undone is worse than no sync at all |

That asymmetry is deliberate — see the comment at the top of
`sb/calsync/gtasks.py`, and Kleppmann's argument against generic
last-writer-wins, which discards a real edit whenever ordering disagrees.

## Order of operations

`Engine.sync_calendar()` **reads before it writes**, always. The push deletes
the Google task for any project the vault now considers finished; if the read
came second, a tick made on the phone would be deleted before it was ever
seen. Read, then write, is the only ordering under which the tick survives —
if you ever reorder those two lines, step 5 above is the test that fails.

## When it goes quiet

A failed *push* is loud: nothing new appears on the phone. A failed *read* is
silent in exactly the wrong way — ticking keeps working, the vault simply
stops hearing about it, and the first evidence is a project you finished a
fortnight ago still sitting open. So a read-back failure files an incident and
the dashboard shows a banner: **"Ticks made on the phone are not reaching the
vault"**. It clears itself on the next successful read.
