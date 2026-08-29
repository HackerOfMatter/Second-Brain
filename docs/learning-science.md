# The learning-science layer

Roadmap Tier 2 and Tier 3. The scheduler was always good; the pedagogy around
it was missing. Each section below is one lever, the evidence for it, and where
it lives in the code.

Ordering here is by effect size, not by build order.

---

## Card quality — one card, one thing

`sb/quality.py` · full write-up in `docs/card-quality.md`

Woźniak's minimum information principle, enforced in the generator. A list
answer is rewritten as one cloze card per item over the note's own sentence;
an unciteable one is dropped and counted by rule.

---

## Calibration — did you know that you knew it?

`sb/calibration.py`

The scheduler knows whether you were right. It has never known whether you
*thought* you would be, and the gap between those is the only signal in the
system that points at a specific fixable belief.

- **Koriat & Bjork (2005)** — the illusion of competence. Retrieval feels easy
  while the answer is in front of you, and that feeling is what people use to
  decide they have studied enough. It is wrong in one direction.
- **Kornell & Bjork** — judgments of learning track *fluency*, not
  retrievability, and re-reading inflates fluency specifically.
- **Dunlosky & Rawson (2012)** — calibration training improved retention more
  than the same minutes spent on extra review.

**What you do:** tap `j` / `k` / `l` — sure, think so, no idea — before
revealing. **What you get:** a Brier score, a predicted-vs-actual curve, and
the cards you were sure about and missed, moved to the front of your next
session. Being right but unsure is reported and *not* acted on: that is not a
study problem, and re-drilling it would spend real minutes on nothing.

---

## Self-explanation — you explain first

`sb/tutor.mark_self_explanation`

The "Explain this" button runs the transfer in the weaker direction. On a card
graded Again or Hard you are asked to say why in one line, before the
explanation appears.

- **Slamecka & Graf (1978)** — the generation effect: material you produce is
  remembered better than the same material read.
- **Chi et al. (1989, 1994)** — the self-explanation effect, and the finding
  that *prompted* self-explanation beats spontaneous. Almost nobody does it
  spontaneously, so the prompt is the intervention.

Marked like a typed recall: the model proposes, you dispose. Nothing touches
the schedule, and it is logged to `_decks/_explanations.jsonl` — never the
review log, which would skew the streak and any future FSRS fit.

---

## Habits — the causal half

`sb/habits.py`

The old model was an occurrence count and a weekly "continue or change it?".
Count is the weakest lever in the literature. In descending order of effect:

1. **Implementation intention** — *"When [cue], I will [behaviour] at
   [place]."* Gollwitzer (1999), d ≈ 0.65 across 94 studies: the largest single
   effect in the field, and it is one sentence. Stored as three fields, because
   a sentence with a blank in it gets left blank. It is rendered into the note
   *and* into the calendar reminder — the reminder is the cue's moment, and the
   effect comes from the plan being present when the situation arrives.
2. **Anchor** — Fogg, B = MAP. The reliable prompt is an existing routine;
   habit stacking is the practitioner form of the same idea.
3. **Friction** — Wendy Wood: context and friction outweigh motivation, in both
   directions. One thing made easier, one made harder.
4. **Never miss twice** — Lally et al. (2010): median 66 days to automaticity,
   and a single miss did *not* measurably impair formation. So the check-in
   reports **consecutive misses**, never a streak. A streak counter destroys a
   whole number on the first miss, at exactly the moment nothing has gone
   wrong, and watching someone quit at a broken streak is watching a metric
   cause the failure it claims to measure.
5. **Context stability** — Wood again: same time, same place is what
   automates a behaviour, at any frequency. So an occurrence records when and
   where, and the check-in reports how consistent that has been.

---

## Estimates — the outside view

`sb/forecasting.py`

`ProjectMeta.time` drove every calendar block and nothing ever checked it.

- **Buehler, Griffin & Ross (1994)** — the planning fallacy. People
  underestimate their own tasks, do it even knowing they have before, and do
  *not* do it when estimating someone else's.
- **Kahneman & Tversky** — the outside view: stop reconstructing the task, go
  look at how long tasks like it took.
- **Flyvbjerg** — reference-class forecasting as the working method.

Start a step, tick it off, and the real duration is recorded next to the plan.
Once five finished steps exist, new estimates are multiplied by the median
ratio of your own class — and told to you as `90 min = 45 × 2.0 (your last 14
steps)`, because an estimate silently doubled is a system you stop trusting.
The median, not the mean, and capped, because one afternoon with a timer left
running should not put a week of blocks on a two-hour job.

---

## Interleaving — across problem types, not only decks

`sb/tutor._lane`

Interleaving across decks was already there. **Rohrer & Taylor (2007, 2015)**
measured the effect on problem types *within* a subject, and the mechanism they
identified is discrimination: blocked practice never asks you which method
applies, because the last problem already told you. Cards carry a `Type.` label
and consecutive cards rarely share one. An unlabelled deck behaves exactly as
before.

---

## Worked examples — and taking them away

`sb/tutor.worked_example_for`

**Sweller's worked-example effect**: for material you cannot yet do, studying a
full solution beats attempting the problem, because attempting it spends all
your working memory on search. A bare recall card on first encounter is that
wasted search.

And **expertise reversal**, also Sweller: the same example that helps a novice
*hurts* someone competent. So a `Worked.` block is shown in full on the first
attempt, as an opening fragment for the next few (the last step always left to
you — Sweller's completion-problem format), and withdrawn once the card has
been answered enough times or the deck as a whole is mature.

---

## Retention vs workload

`sb/retention.py`

`desired_retention: 0.9` was a default, not a decision. Raising it shortens
every interval, and the cost is superlinear at the top. **Jarrett Ye's** FSRS
simulator work gives the counter-intuitive result worth surfacing: optimal
retention — most retained per minute spent — is usually **below** 0.9.

The projection uses FSRS's own inverted forgetting curve and your measured
seconds-per-review. It deliberately does not model lapses, which makes it a
mild understatement of the cost of going low — the safe direction for a number
arguing for going low.

---

## Atomicity

`sb/lint.py`

Phase 6's free linking tier works *because* notes are atomic and precisely
named, and nothing ever checked that it was still true. Ahrens and Luhmann give
the rule — one idea per note, links over folders — and the reason is mechanical:
a note holding three ideas can only be linked as a unit, so the third idea is
invisible to retrieval, to the card generator and to the graph.

Reports, never edits. `python run.py lint`.

---

## The weekly review

`sb/engine.weekly_review` · the page at `/review`

Three prompts existed — habit check-in, Resource review, "ready to graduate?"
— each on its own timer and each answerable without ever looking at the week.
**David Allen**: the weekly review is the load-bearing habit of the whole
method, because without one place where everything outstanding is seen
together, trust decays and you go back to keeping it in your head.
**Zimmerman's** self-regulated-learning cycle gives the order, and it is the
order of the three columns: forethought → performance → self-reflection.

Read-only. A review that changes things while you read it is one you cannot
trust.
