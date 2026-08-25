# The tutor

Flashcards generated from your own notes, scheduled by FSRS, reviewed across
subjects in one sitting. Blueprint §4 and §5, and the spaced-repetition half
of §8.

Open it at **http://127.0.0.1:8787/study**, or from the Study button on the
dashboard.

---

## The loop

1. **Make cards.** Pick a Project or Resource under *Decks & cards* and press
   Generate. The local model reads the note one passage at a time and drafts
   questions, quoting the sentence each answer came from.
2. **Approve them.** Everything lands as a draft and is invisible to the
   scheduler until you have read it. Fix a wording, delete a bad one, approve
   the rest. This step is not optional politeness — see *Why drafts* below.
3. **Review.** Press Start. Cards from every subject that has something due
   are mixed together. Show the answer, then say how well you knew it.
4. **Graduate.** When a learning Project's deck says the material has stuck,
   the dashboard asks whether it should become a Resource. You decide; the
   system only notices.

---

## FSRS, in one page

The scheduler is FSRS-5 — the algorithm Anki ships today. It tracks three
numbers per card instead of SM-2's one, and the separation is the whole point.

| | what it means | how it moves |
|---|---|---|
| **Stability** *S* | days until your chance of recall falls to 90% | grows on every success, shrinks on a lapse — but never to zero |
| **Difficulty** *D* | 1–10, intrinsic to the card | steps with each grade, then reverts toward the mean |
| **Retrievability** *R* | chance you'd recall it *right now* | decays from *S* and days elapsed |

SM-2 could not tell a card you find hard from a card you simply have not seen
in a while, so it punished both — miss something three times and its ease
bottomed out permanently. The mean reversion in *D* is the fix.

*R* is why the timing of a review matters. Answering something you nearly
forgot teaches the model far more than answering something you saw an hour
ago, and FSRS gives it proportionally more credit. That is also why a card
reviewed three weeks late is worth more than one reviewed on the dot, and why
answering the same card twice in an evening barely moves it.

The four buttons show what each would cost you before you press it:

```
  Again        Hard         Good         Easy
   1d           4d           11d          21d
```

**The one dial worth touching** is `study.desired_retention` (default 0.9).
It is the target the intervals are solved for. 0.95 means seeing everything
far more often for a modest accuracy gain; 0.85 means a lighter load and more
forgetting. 0.9 sits at the flattest part of that trade.

The published FSRS-5 weights are fitted across millions of real reviews and
are a good starting point for anyone. If you ever accumulate a few thousand
reviews of your own, `_decks/_reviews.jsonl` holds exactly what an optimizer
needs — every entry records the card's state *before* the answer as well as
the grade — and the result goes in `study.weights`.

---

## Mixed subjects

A session pulls from every deck with something due and spreads them out, so
consecutive cards rarely share a subject.

This is deliberate and it will feel worse than the alternative. Studying one
deck at a time (blocked practice) produces a stronger sense of mastery and
weaker actual retention: the context never changes, so you end up retrieving
from working memory rather than from storage. Interleaving forces the harder,
more useful retrieval every time. Set `study.interleave: false` if you would
rather have the comfortable version.

Small decks are spread proportionally rather than being finished in the first
minute — a deck with three cards due and one with thirty are both distributed
across the whole session.

You can also tick specific subjects before starting, which narrows the pool
without changing anything else.

**Caps.** `new_cards_per_day` (10) and `max_reviews_per_day` (120) exist so a
backlog is a slope rather than a wall. Overdue cards come first in priority;
`session_size` (40) bounds one sitting.

---

## Two ways to answer

**Self-graded**, like Anki: reveal, then press Again / Hard / Good / Easy.
Fast, and it is the signal FSRS was fitted on.

**Typed recall**: write the answer from memory first, and the model marks it
against the reference. Slower, and a genuinely different test — recognising an
answer and producing one are not the same skill, and self-grading drifts
generous precisely because of that gap.

Marking and grading are separate steps. The model proposes a grade and shows
you why; you accept it or overrule it. Nothing is written to the deck or the
review log until you do, so a marker that gets you wrong costs a click rather
than a card. With no model running, marking falls back to word overlap, says
so plainly, and never awards Easy — the worst it can do is make you re-see
something you knew.

**Explain this** answers "but why?" from the card's own source note, without
leaving the review screen.

---

## Why drafts

A wrong flashcard is worse than no flashcard. It does not just sit in a file:
spaced repetition will patiently drill the error into you on an optimal
schedule, and you will end up more confident about something false than you
were before. So the generator has three defences, and the third is you:

1. **Chunking** — the model sees one passage at a time. Asked for twenty cards
   from four pages it drifts and invents; asked for three from one paragraph
   it stays honest. The system's own rendered sections (`## Steps`,
   `## Skills`, the check-in log) are excluded, which is the same lesson as
   the colour bug in Revision 1: never train on your own boilerplate.
2. **Citation** — every card must quote the sentence that justifies its
   answer, and the quote is checked against the passage. A quote that is not
   really there is dropped rather than shown, because a fabricated citation
   reads as evidence.
3. **Approval** — everything arrives as a draft.

Cards are also rejected for restating the question, for depending on "the
above", and for duplicating a card already in the deck.

With no model reachable, a heuristic path makes cloze cards from
definition-shaped sentences. Fewer and blunter, and every one is a verbatim
sentence from your note, so it cannot invent anything.

---

## Where cards live

`_decks/` in the vault, one markdown file per note, plus `_reviews.jsonl`.

**This folder is not disposable.** Everything under `_system/` can be rebuilt
from your notes; a year of review history cannot. Back it up.

Each deck file has two halves, owned by different parties:

* **Frontmatter owns scheduling** — stability, difficulty, due date, status.
  Leave it alone; there is nothing there you would want to hand-edit.
* **The body owns the card text** — question, answer, source quote. This is
  yours. Rewrite a question in Obsidian, add a card by copying a block and
  giving it a new id, delete a card by deleting its block.

They join on the card id in the heading. A card in the body with no scheduling
row is new and starts unscheduled; a scheduling row whose card you deleted is
dropped on the next write. The two halves can never disagree about *which
cards exist* — only about state you do not touch.

```markdown
### Card c3

**Q.** What does FSRS separate that SM-2 conflates?

**A.** Memory strength, intrinsic difficulty, and current recall probability.

**Why.** FSRS separates three quantities, which is the whole trick.
```

Wrap a phrase in `{{double braces}}` to make it a cloze deletion.

Editing a card's text does not reset its schedule. Fixing a typo in something
you have known for six months should not cost you those six months; if the
meaning changed enough to matter, delete it and write a new one.

---

## Mastery and graduation

Blueprint §4 wants the human in the loop at the moment an automatic decision
would be riskiest. The system decides when to *ask*, never when to move.

A card is **mature** when it has survived `review.graduation_min_reps` spaced
attempts (4) *and* its stability is at least `study.mature_stability_days`
(21). Both halves matter: four answers in one evening is not four spaced
attempts, and one lucky Easy is not stability.

A deck is **ready to graduate** when it has at least
`study.min_cards_to_graduate` active cards (6) and
`review.graduation_mastery_threshold` of them are mature (85%). The card-count
gate exists because a deck of two cards can hit 100% in a week and mean
nothing.

When that happens to a *learning* Project, its status becomes `graduating`,
the dashboard asks, and its `srs:` frontmatter block gets the headline numbers
so you can see where it stands without opening the app. Non-learning Projects
are never offered, however well their decks go.

Decks are keyed by note id, not by file path, so graduating a Project to a
Resource carries its cards and their whole history with it.

---

## On the calendar

One daily recurring block, `📚 Study — spaced repetition`, at
`study.study_time` (19:30). Same treatment an Area gets, and for the same
reason: a review queue only works if you meet it every day, which makes it a
habit rather than a task.

Deliberately not one event per day with a card count in the title — that would
mean rewriting 365 events on every sync, for a number that is stale by the
afternoon.

Set `study.calendar_event: false` to turn it off. It only appears once at
least one deck exists.

---

## API

| route | what |
|---|---|
| `GET /study` | the interface |
| `GET /api/study/overview` | decks, stats, limits, graduation prompts |
| `GET /api/study/stats` | streak, heatmap, forecast, grade mix |
| `POST /api/study/session` | `{subjects?, limit?}` → an ordered queue, answers withheld |
| `GET /api/study/{note}/{card}/reveal` | the answer side, plus the four intervals |
| `POST /api/study/{note}/{card}/mark` | `{typed}` → a proposed grade; schedules nothing |
| `POST /api/study/{note}/{card}/answer` | `{grade, mode, typed?, seconds?}` |
| `POST /api/study/{note}/{card}/explain` | `{question?}` → the tutor, scoped to the source note |
| `GET /api/decks/{note}` | one deck with every card |
| `POST /api/decks/{note}/generate` | `{max_cards?, source?}` |
| `POST /api/decks/{note}/approve` | `{cards?}` — all drafts, or the ones named |
| `POST /api/decks/{note}/cards` | `{front, back}` — a hand-written card, active at once |
| `POST /api/decks/{note}/cards/{card}` | edit, `{status}`, or `{delete: true}` |

Sessions hold no server-side state. Each answer is written as it happens, so
closing the tab halfway through loses nothing and double-counts nothing.
