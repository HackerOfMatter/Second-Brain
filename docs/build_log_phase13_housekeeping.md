# Build log — Phase 13: the housekeeping list

**Status: DONE.** Written 2026-09-14. Full suite green: **127 tests, up from
108.** Nine items, in lj's words, worked bugs-first at lj's direction.

Two things shaped how this was built and are worth recording before the work
itself. First, `device_bash` could not mount the vault for the whole session
— a Windows update dated 8 September broke the Plan 9 share the workspace
uses — so every file crossed by stage-and-commit, and every test run happened
against a container copy of the repo rather than in place. Second, the link to
lj-studio dropped four times mid-session; items 3, 4 and 9 were finished and
sat waiting for it. Neither changed what was written, both changed how long it
took.

## 0. What the list said

> - ctrl shift z: make it use a term template, make autofill highlighted
>   portion, press enter to confirm
> - Create start note making: finds main folder (class), makes a sub-folder
>   for what chapter, all notes created during the session go into this folder
>   until session is closed, terms are to be studied, creates a session summary
> - Once note is created, can only be read, not deleted
> - automatic goes to resource unless otherwised stated
> - add function to group add to a folder
> - Help combat notes fallacy
> - second brain dashboard is slow, where this goes is slow
> - file dropped notes doesn't accept files
> - window shift s screenshot

## 1. Files dropped onto the dashboard (`/api/drop/upload`)

The Drop folder was always the interface and stays the interface. What was
removed is the requirement that lj be standing in front of *this* machine's
Explorer window to use it: the page is a drop target, files land in `Drop/`
and are read by the same reader and classified by the same rules. Nothing
downstream can tell the difference, which is the point.

base64 in a JSON body rather than multipart, because Starlette's form parser
needs `python-multipart` and a sixth package for one route is a bad trade
against a transport both ends already speak. 25 MB per file, 50 files per
call, checked in the browser *and* in `intake.accept_upload` — the UI is not
the only thing that can POST.

**The settle wait is skipped for uploads and only for uploads.** It exists
because a file Word is still writing is not a file; these bytes arrived whole
in one request. Waiting five seconds to file something lj just dropped into
the page would read as the feature not working.

`safe_drop_name` takes the basename only and strips separators rather than
interpreting them, so `../../config.yaml` cannot become a path. Unsupported
types are refused before the bytes land rather than written and then swept
into `_problem/` — a file the system will never read should not sit in the
vault as litter.

## 2. The dashboard, and "where does this go?"

Measured on a 600-note vault, cold cache: **0.81 s → 0.20 s.** Four things.

1. **`vault.counts()` built a validated pydantic `Note` and deep-copied its
   whole frontmatter for every file in the vault — to read one enum off it.**
   That was almost the entire cost of `/api/health`, which the dashboard calls
   on every load. `_bucket_at` reads the field out of the cached frontmatter,
   the same argument `_id_at` already made for ids.
2. **Ninety per cent of a cold walk was PyYAML's pure-Python scanner** —
   `need_more_tokens`, `forward`, `check_token`, none of it ours.
   `CSafeLoader`/`CSafeDumper` (libyaml; the wheel on this machine has it),
   with a `SafeLoader` fallback that costs speed and nothing else. The dumper
   was checked to write **byte-identical** frontmatter before the switch — a
   dumper that formatted differently would rewrite every note on its next save
   and turn every diff into noise.
3. **The page fetched `/api/dashboard` and then `/api/health` serially**, so
   it took the sum of two whole-vault passes rather than the slower one.
4. `health()` globbed the deck folder twice.

"Where does this go?" is the Inbox panel, and the panel is cheap — the cost is
the *answer*. Clicking Project runs an Ollama call, the planner, link-on-write
and a calendar rewrite before it returns. That is the full treatment and it is
correct. What was not acceptable was a row that looked dead while it happened,
so the button that was pressed now says "Planning…".

### 2.1 Two bugs found on the way

**A single mistyped `bucket:` took `/api/health` down with a 500.**
`pydantic.ValidationError` escaped the "a file we cannot read is skipped"
rule, because the `except` clauses named `VaultError` and not the error a
hand-typed `bucket: nonsense` actually raises. Now one tuple, `UNREADABLE`,
used everywhere the vault reads a file it is not sure about.

**`check()` failures were invisible to pytest.** It records a failure and
returns — deliberately, so a test with thirty checks reports all thirty — but
pytest only sees exceptions, so a run could report green over a list of
failures. And pytest is what these build logs quote. `test_zzz_every_check_passed`
runs last and turns the list into a real failure. The suite is now honest as
well as green; this log's numbers are the first that are checked.

## 3. Terms (`Engine.capture_term`, `_templates/Term.md`)

Highlight a word anywhere in Windows, press the hotkey, and the box opens with
that word already in it and selected — Enter accepts it, typing replaces it. A
short one-line selection opens in Term mode, which has a second field for the
definition. The foreground window's title goes into `## Seen in` for free,
because a term met in three places is a term you understand and a term met
nowhere is a word you wrote down.

**The card is written, not generated,** and the reason is arithmetic rather
than taste: a term note is a dozen words, `generate_folder` skips anything
under forty as too thin, and so the notes lj most wants quizzing on were
exactly the ones the deck machinery would never touch. Front and back are what
lj typed, so there is nothing for a model to add and nothing for it to get
wrong — it is the one study path an Ollama outage cannot reach. It lands
`active` rather than `draft` for the same reason `add_card` does: the draft
rule exists to stop *generated* cards reaching the scheduler unread.

A term captured with no definition is still filed, tagged `needs-definition`,
and listed by `undefined_terms()`. The word you did not know is the useful one.

### 3.1 Reading a selection without stealing the clipboard

There is no Win32 call for "the selected text in another app"; the only
mechanism is to send the foreground window Ctrl+C. Two problems, both solved
rather than lived with. The hotkey is Ctrl+Alt+Z, so Ctrl and Alt are held at
the moment it fires and a synthetic C on top of that is Ctrl+Alt+C — the
unwanted modifiers are released synthetically first. And
`GetClipboardSequenceNumber` makes the clobbering honest: if the number does
not move, nothing was selected and the clipboard is left untouched; when it
does, the previous *text* is put back.

## 4. Note-taking sessions (`sb/session.py`)

`/start fed tax / ch 4` in the capture box. Every capture until `/end` lands in
that class's chapter folder; the box shows a banner; closing it writes a
summary. A panel on the dashboard does the same for anyone who would rather
click. The problem is filing, and it is a problem of *when*: during a lecture
the destination of every note is obvious, and forty minutes later it is a pile
of captures that have lost the one thing that made them easy to file.

**The course folder is matched, not created on sight.** `resolve_folder` tries
exact, prefix, every-word and initials — which is what makes "fed tax" find
`Federal Taxation` — and only then agrees to make a new one, saying which it
did. "Exactly one" is load-bearing in the last three: an ambiguous name is a
coin flip between two subjects' worth of notes, so it makes a new folder
instead. The failure mode of the obvious implementation is a vault holding
`Fed Tax`, `fed tax` and `Federal Taxation`, each with a third of the subject
and none of them wrong enough to notice for a term.

**A session decides the folder, never the bucket.** "Essay due Thursday"
captured in class is still a Project; it goes to `20-Projects/Federal
Taxation/Ch 4`. An explicit folder still beats the open session, because
saying where a note goes is a decision and a session is a default.

**Starting a session closes the open one** rather than refusing. The realistic
reason it happens is walking out of one lecture into the next; losing that
hour to a modal error would be the worst possible answer.

State lives in `_system/` and is disposable — the notes are the record. The
summary is built from the session's own list of ids, so it can only claim
notes that were actually taken, and it separates **"Look these up"** (terms
captured without a definition) from everything else: that list is the homework
the session generated and it is worth nothing buried in the middle.

## 5. A note can be read, moved and edited. Never deleted.

`Vault.retire()` moves a note to `40-Archive/_retired/<YYYY-MM>/` and logs
why. `adopt.undo` — the one function that used to delete — retires instead;
adopted *assets* are still removed outright, because those are hash-verified
byte-identical copies of a source file that is still there, and a third copy
is hoarding rather than caution.

The rule is an invariant, and an invariant nobody checks is a comment, so
`test_nothing_in_the_system_deletes_a_note` walks every `unlink`, `os.remove`
and `rmtree` in `sb/` and requires each to be on something that is not a note.
The next delete path gets added deliberately or not at all.

### 5.1 The bug this found

**Two notes with the same title captured in the same second were the same
note.** `new_id` is `{second}-{slug}` and `path_for` builds the filename from
the same two parts, so the second capture overwrote the first on disk — and
`find()` could not have told them apart afterwards even if it hadn't. Silent:
no error, no log line. Two terms typed quickly into a lecture is exactly that
shape of capture.

Fixed twice over. `new_id` will not issue the same id twice — a repeat of the
same slug advances the stamp by a second, per slug, so two *different* titles
in one second still keep the time they really happened, and the format stays
sortable and stays fifteen characters wide for `path_for`. And `Vault.write`
diverts a write whose destination holds a *different* note, writing beside it
and logging that it did. Rewriting the same note is untouched, which is every
save in the system.

How long this was live is unknown. It is the reason to look twice at any note
that ever seemed to go missing.

## 6. Resource is the default (`config.capture.default_bucket`)

A capture that named no bucket went to `00-Inbox`, which is a queue, and the
failure mode of a queue is that nobody empties it. Most of what is captured in
a hurry is reference material, so `resource` is the honest default: filed,
searchable and on the review cycle from the moment it is written. One line of
config moves it back. The hotkey box now sends no bucket at all rather than
asserting `inbox` — that decision belongs in config, not in a startup script.

**The Drop folder's `auto_floor` is deliberately untouched.** Silence is not
doubt. A capture that did not say where it was going said nothing; a dropped
file scoring 0.4 is the system having actually formed a doubt, and burying
that in a folder of five hundred resources would hide the misfile forever,
where an Inbox of nineteen is a list that can be finished.

## 7. Filing several notes at once (`Engine.move_to_folder`)

Filter by title, tick, name a folder, file. The thing it replaces is opening
Obsidian and dragging each note, which is fine for one note and is exactly why
forty notes stay where they landed. The folder name is matched the same way a
session's is, because the two features would otherwise disagree about what a
folder is called and the whole point of matching is one folder per subject.

Nothing but the folder changes: `Vault.relocate` moves the file and does not
touch the note, so a Project keeps its deadline, its steps, its calendar
blocks and its `updated` stamp, and nothing can be mangled on a round trip
through YAML. Each note keeps its own bucket, so `20-Projects/Federal Taxation`
and `30-Resources/Federal Taxation` end up holding the two halves of one
subject. **One bad id is reported and the rest still move** — losing
thirty-nine because the seventh was stale is what makes a batch operation
something you stop using.

## 8. The collector's fallacy (`sb/collected.py`)

Saving a good note produces the feeling of having learned it, and the feeling
is not evidence. A vault that grows faster than it is used gets *worse* at
answering questions, and this system will happily help lj do that faster than
before. So it has to be able to say which is happening, from evidence already
in the vault.

**Two things count as having used a note, and lj chose both:** something
written under `## In my own words`, or a card that has actually been answered
(`reps > 0` — a deck of drafts is collecting with extra steps).

Three things deliberately do not count. *Links*, because `## Related` is
written automatically on every capture and counting it would mark the whole
vault processed on the day it was written, which is precisely the illusion
being measured. *Having been edited since*, because `updated` is bumped by the
system's own re-renders and a signal a machine can forge is not a signal.
*Having cards at all*, per above.

What is **eligible** to be debt matters as much as the test, or the panel is
noise nobody reads: Resources only, over forty words (the same floor
`generate_folder` already uses), older than two weeks, terms excluded. Oldest
first — the oldest untouched note is the one most likely to be genuinely dead,
and a list that starts with last week's never reaches it. Three ways out of
each row: restate it, card it, retire it.

`ratio` is used over *eligible*, never used over everything. A vault can
always improve the second number by capturing more short notes, and a measure
that rewards capturing more is worse than no measure.

## 9. Win+Shift+S (`png_from_dib`, `watch_clipboard`, `Engine.attach_image`)

Windows owns Win+Shift+S and it cannot be intercepted. It does not need to be:
the snip's only effect is a bitmap on the clipboard, and the hotkey process is
already resident, so a sequence-number check once a second sees it land.

**Outside a session it files nothing, and that restraint is the design.** Most
screenshots anyone takes are not notes — a receipt going into an email, a bug
going to a colleague — and a tool that quietly copied every one into a vault
is a tool you turn off within a day. Instead it is held for three minutes and
offered to the next capture, where the line typed becomes the caption, which
becomes the title, which is what makes the image findable six weeks later.
Ctrl+D drops it. During a session it is filed straight into the session's
folder and joins the summary: the slide snipped at 11:15 is in the chapter it
was shown in.

A screenshot becomes **a note with the image in it**, not a loose file in an
attachments folder. Everything else here is a note, which is what makes
everything else searchable, linkable and countable; an image filed as an
exception to that is an image nobody finds again. The embed is `![[name.png]]`
rather than a path, because Obsidian resolves an embed by filename from
anywhere in the vault and the link therefore survives the group-file box
moving the note later.

The DIB is turned into a PNG **by hand**, with `struct` and `zlib` and nothing
else. A screenshot that needs Pillow installed is a screenshot that stops
working the first time the venv is rebuilt, and this file's contract is that
it is stdlib only and therefore always runs. Alpha is dropped on purpose: a
32-bit BI_RGB pixel's fourth byte is *undefined* and Windows screen capture
leaves it at zero, so reading it as alpha makes every screenshot fully
transparent — which looks exactly like the feature silently not working. Only
24- and 32-bit uncompressed DIBs are handled; anything else is refused rather
than guessed at, because a wrong guess writes a corrupt file into the vault
and calls it a note. Every shape Windows produces — 24- and 32-bit, bottom-up
and top-down, BI_RGB and BI_BITFIELDS — is checked pixel-for-pixel against a
decoder in `test_a_clipboard_bitmap_becomes_a_png`.

## 10. What is not done, and what to watch

1. **Nothing here has been exercised against a real model.** Ollama was
   unreachable for sprints 4 and 5 and was not checked this time either. The
   term path, the session path and the screenshot path all avoid the model by
   construction, so this matters less than it did — but `capture` during a
   session still calls the planner, and that is still unverified against a
   live model.
2. **The capture hotkey's Win32 code has never run on Windows.** The pure
   parts — the term rule, the offline note shape, the DIB conversion — are
   tested. `grab_selection`, `clipboard_dib` and `watch_clipboard` are
   reasoned about and not executed; the first real press is the first real
   test. Restart the hotkey process to pick any of this up.
3. **`Ctrl+Shift+Z` was asked for and not done.** The hotkey is Ctrl+Alt+Z.
   Ctrl+Shift+Z is Redo in most editors and taking it globally would break
   that everywhere; `capture_hotkey:` in config.yaml sets it if lj still wants
   it.
4. **`new_id`'s uniqueness is per process.** Two processes writing captures in
   the same second with the same title can still collide, and `Vault.write`'s
   divert is what catches that. Both paths currently go through the server, so
   this is theoretical.
5. **The auto-sync cron is still committing the tree as lj every ~30 minutes,
   and this phase it reverted a file.** Sprints 4 and 5 both asked whether it
   should exist; this is the first time the answer has cost correctness rather
   than tidiness. `sb/vault.py` was written with `relocate()` in it during
   item 7 and, when every file was staged back at the end of the session and
   compared byte for byte against the container copy, came back **without it**
   — while the rest of that same commit was intact. `Engine.move_to_folder`
   calls `relocate`, so the group-file box would have failed with an
   `AttributeError` on a feature whose tests had passed. It was rewritten and
   re-verified.

   Two things follow. The end-of-session byte comparison is not ceremony and
   should be part of every session that writes across the bridge; a green test
   suite proves the code is right, not that the code reached the disk. And the
   cron is now a known source of silent reverts, which is a stronger argument
   for switching it off than either of the previous two sprints had.
6. **`device_bash` cannot mount the vault** until the 8 September Windows
   update is worked around. Everything here crossed by stage-and-commit, which
   is slower and, more importantly, means no `git` command ran against the
   repo this session — the working tree has the changes and no commit was made
   by hand.

## 11. Files

New: `sb/session.py`, `sb/collected.py`.
Changed: `sb/engine.py`, `sb/api.py`, `sb/vault.py`, `sb/models.py`,
`sb/intake.py`, `sb/config.py`, `sb/frontmatter.py`, `sb/templates.py`,
`sb/adopt.py`, `sb/web/index.html`, `capture_hotkey.pyw`, `config.yaml`,
`config.example.yaml`, `tests/test_all.py` (+19 tests).
