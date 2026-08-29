# Card quality — one card, one thing

`sb/quality.py`. Runs inside `generate.py`, between the model's answer and the
deck.

## Why there is a gate at all

The generator already refused four things: a question that restates itself, a
question that says "the above", an answer that is only the note's title, and a
duplicate. None of those is what actually decides whether a card is learnable.
That is **how much one card asks you to remember**, and until this it was
unchecked.

The standard is Piotr Woźniak's *Twenty rules of formulating knowledge*
(SuperMemo, 1999), the only published one there is. Four of the rules are
mechanical enough to enforce:

| Rule | What it says | What the code does |
|---|---|---|
| 4 — minimum information | one card, one thing | an answer holding two facts is refused |
| 5 — cloze deletion | fill-in-the-blank beats a question | a wordy definition is converted |
| 7 — avoid sets | a set has no order and no cue | a two-item answer is refused |
| 8 — avoid enumerations | the least learnable card there is | a list is rewritten, one card per item |

A two-part answer is not a hard card. It is two cards sharing one schedule,
and the schedule is then wrong for both: failing one half re-drills the half
you already knew, on an interval computed from the average of the two.

## Repair before refusal

Refusing is cheap but it throws away a passage the model already read. Where
the offending answer is a list **and** the note contains a sentence saying so
verbatim, the card is rewritten instead: one cloze per item, over that
sentence. Rule 8's own prescribed remedy, and it cannot invent anything —
every character of the new card is copied from the source, the same guarantee
the citation check gives.

```
Q  What are the three branches of government?
A  legislative, executive, and judicial          <- one card, three facts

becomes

The three branches of government are {{legislative}}, executive, and judicial.
The three branches of government are legislative, {{executive}}, and judicial.
The three branches of government are legislative, executive, and {{judicial}}.
```

Where nothing in the note can cite it, the card is dropped and counted. The
study page says which rule dropped it, so a thin deck reads as a filter doing
its job rather than as a model failure.

## What is deliberately *not* flagged

Over-rejection would gut a deck, so the list detector is conservative in one
direction and the tests check the false positives as hard as the true ones:

- `September 4, 2026` — one comma, no conjunction. One fact.
- `about 1,000` — a thousands separator is not a list.
- `salt and pepper`, `trial and error` — fixed pairs are single terms.
- `the difference between stability and difficulty` — `between … and` is one
  idea that happens to contain a conjunction.

## The dials

```yaml
study:
  max_answer_words: 15       # 15 leaves room for a qualified definition
  enforce_card_quality: true # false keeps every card the model returns
```

15 is not from the paper — Woźniak gives no number. It is the practitioner
consensus (the Anki manual, the SuperMemo forums) rounded up. Unlike a
retrieval threshold it is not falsifiable from lj's own use, so it is a
preference, honestly labelled as one.

## Cards you type yourself

Advisory, never a refusal. `add_card` returns a `warning` and files the card.
A card someone writes by hand is a decision, not a draft.
