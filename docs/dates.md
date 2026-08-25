# Dates

Two things go wrong with dates in a capture, and only one of them is a bug.

The bug half is ordinary parsing, and it is fixed: "next Friday" and "this
Friday" no longer land on the same day, "a week from today" no longer returns
today, "in a month" is a calendar month, and "read pages 3/4" is no longer the
4th of March.

The other half cannot be fixed by code at all. "By the end of the week" is
Friday to one person and Sunday to another. There is no correct answer to
find, so instead of guessing silently the parser reports **how** it knows, and
anything it had to interpret waits for a glance.

---

## The three bugs

**1. "this Friday" and "next Friday" were the same day.**
`_next_weekday` computed "next" as *Friday of next calendar week*, which on a
Saturday is the same Friday a bare "Friday" already meant. Both gave 28 Aug.
A deadline landing a week early is exactly the kind of wrong that looks right.

Now: bare or "this" → the next occurrence, strictly in the future. "next" →
that, plus seven days. From Saturday 22 Aug, `this friday` is 28 Aug and
`next friday` is 4 Sep.

**2. "a week from today" returned today.**
The bare `today|tonight|eod` matcher ran before the interval matcher and
claimed the word "today" out of the middle of the phrase, returning the wrong
end of the interval. Interval rules now run first.

**3. `in a month` was 30 days.**
From 22 Aug that gave 21 Sep. It is real month arithmetic now, clamped to the
end of short months: a month after 31 Jan is 28 Feb, not 3 Mar.

### And a false-positive class

`\d{1,2}/\d{1,2}` matched "do 1/2 the chapter" → 2 January, and "read pages
3/4" → 4 March. A slash or dash pair is now only read as a date when something
marks it as one: a preposition in front (`by 9/8`, `due 9/8`), a four-digit
year, or the pair being the entire capture. Both of those phrases now parse to
**no date**, which is the correct answer — this module's own rule is that an
absent deadline beats a wrong one.

---

## What it understands

| | |
|---|---|
| named dates | `2026-09-01`, `sept 8`, `8 september`, `the 30th of September` |
| marked numeric | `by 9/8`, `9/8/2026`, `8-25` |
| day of month | `the 30th`, `by the 15th`, `due 5th` |
| relative | `today`, `tonight`, `eod`, `tomorrow`, `day after tomorrow` |
| intervals | `in 3 days`, `in 2 weeks`, `a week from today`, `2 weeks from now`, `in a month` |
| weekdays | `friday`, `this friday`, `next friday`, `by monday` |
| periods | `eow`, `end of the week`, `eom`, `end of the month`, `next week`, `next month` |
| weekends | `this weekend`, `next weekend` |
| literal | `due: 2026-12-01`, `deadline = friday` |

A `due:` or `deadline =` prefix is read literally and never re-interpreted.
Typing one is an unambiguous act, so whatever follows counts as named — even
`deadline = friday`, which as bare text would only be a guess.

---

## Confidence

`extract.parse_deadline_guess()` returns a `DateGuess`: the date, the words it
came from, and a kind.

| kind | example | confirmed on arrival |
|---|---|---|
| `explicit` | `sept 8`, `2026-09-01`, `by 9/8`, `due: 2026-12-01` | yes |
| `exact` | `tomorrow`, `by eod`, `in 2 weeks`, `a week from today`, `this friday` | yes |
| `ambiguous` | `end of the week`, `next Friday`, `in a month`, `this weekend` | **no** |

The distinction is not "did we find a date" but "did the text *name* one".

Two further kinds are stamped further up:

* **`llm`** — the model returned a date the rules did not. Never confirmed.
  A small local model quietly rewriting `sept 8` into a different Tuesday is
  precisely what this catches. The date is kept rather than discarded, because
  sometimes the model has correctly read a phrase the rules missed — but you
  get asked.
* **`manual`** — you picked it. Confirmed by definition, and it survives a
  re-parse: re-reading the note is a request to re-read the *note*, not to
  overrule a choice you already made.

Stored on the project as `deadline_confirmed`, `deadline_source` and
`deadline_phrase`. Unconfirmed dates still reach the calendar — a forgotten
confirmation should not mean no reminder at all — but they are surfaced for
approval.

---

## The UI

**Confirm these dates** sits at the top of the dashboard whenever something is
waiting. Each row names the project, quotes the words the date was read from —
*read from "end of the week" — people read that two ways* — and offers a date
picker and a **Looks right** button. Approve or correct; the row leaves the
queue either way.

**Every due date is a real date picker.** Native `<input type="date">`, so it
is the OS dropdown calendar, keyboard-navigable, and theme-aware via
`color-scheme`. Guessed dates render amber, confirmed ones plain, because "we
are sure" is the boring state and should look like it. Changing one re-plans
the project's work blocks around the new date and re-syncs the calendar.
Clearing one is allowed: no deadline is a legitimate answer, and a better one
than a wrong date.

**A Due picker sits beside the capture buttons.** Set it and it beats anything
in the text, with no confirmation needed — you already chose it, and a picked
date cannot be misread.

---

## API

| route | what |
|---|---|
| `POST /api/capture` | `{text, bucket, due?}` — `due` is the capture-time picker |
| `POST /api/notes/{id}/deadline` | `{confirm: true}` approve · `{date}` correct · `{date: null}` clear |
| `GET /api/dashboard` | `pending_dates[]` — the approval queue, with `phrase` and `why` |

---

## Verified

`test_date_confidence` asserts the full certain/ambiguous split, all three bug
fixes, the fraction and page-range rejections, and every phrase in the table
above. `test_deadline_approval` covers model drift — kept, sourced to the
model, never confirmed — and drives the real flow through HTTP: capture a
vague date, watch it appear in `pending_dates` with the right phrase, confirm
it, pick a different one, check it lands in the `.ics` as
`DUE;VALUE=DATE:20260930`, survive a re-parse, then clear it.
