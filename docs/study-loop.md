# The study loop — Anki × NotebookLM (Phase 17)

The loop has two halves. **NotebookLM** is good at reading a whole source and
tying every answer to a line in it. **Anki** is good at the parts around one
card: new-card limits, sibling burying, leeches, and a scheduler you can trust.
This phase takes the best of each.

## Making cards (`sb/guide.py`, `sb/generate.py`, `sb/quality.py`)

| Step | What happens | Why |
|---|---|---|
| Study guide | Key terms are found by rule: **bold**, definitions (`X is …`, `… is called X`), glossary lines, and acronyms. With `study_guide: true`, one model call over an outline adds key concepts. A concept the model names but the note never mentions is dropped. | Read the whole note before writing questions (NotebookLM). |
| Plan | Passages are visited uncovered first, then by weight, then spread across the note (0, n/2, n/4, …). | Before this, a 20-card cap only ever reached the first ~7 passages. |
| Prompt | Each passage's request includes its section heading and its key ideas. | Questions name the idea, not "this chapter". |
| Grounding | A card is kept if its quote is really in the passage (**verbatim**). If the quote isn't, the card is kept when one sentence of the passage holds ≥60% of the answer's content words (≥40% for explain cards); that sentence becomes the citation (**recovered**). Otherwise the card is dropped as `ungrounded`. | Before this, the app dropped a broken quote but kept the card. |
| Overlapping cloze | A list sentence longer than 28 words is cut down to its stem, the blank, and one item on each side, with `…` marking the cuts. The citation stays the whole sentence. | Stops a 50-word sentence from hiding one blank. |
| Fill gaps | The **Fill gaps** button in *Decks & cards* (or `fill_gaps: true` on `/api/decks/{id}/generate`) writes cards only for passages that have none. | Coverage is visible and can be fixed. |

## Studying (`sb/tutor.py`, `sb/engine.py`, `sb/web/study.html`)

| Feature | Behaviour | Dial |
|---|---|---|
| Drafts in sessions | Drafts come up marked **draft**, inside `new_cards_per_day`, after approved new cards. Answering a draft keeps it. <kbd>f</kbd> fixes it; the fixed card comes back five cards later. <kbd>x</kbd> drops it. Every action goes to `_decks/_triage.jsonl`. | `drafts_in_session` |
| Sibling burying | Cards cut from the same source sentence appear one per session. A due sibling also holds back new ones. | `bury_siblings` |
| Leeches | A card forgotten `leech_lapses` times (Anki's default is 8) is suspended. The debrief offers **Rewrite from source**: up to 3 grounded replacement drafts. The leech keeps its history. | `leech_lapses`, `leech_action: suspend\|tag` |
| Citation in context | After the reveal, <kbd>n</kbd> shows the card's passage with the quote highlighted, the text around it, and an `obsidian://` link. | — |
| Exam cap | For an open Project with a deadline, no review is scheduled past the day before it. The answer buttons show the capped intervals. After the deadline, FSRS resumes. | `exam_cap` |
| Debrief | When a session ends: number answered, % remembered, minutes, due tomorrow, drafts triaged, passages to re-read (grouped, highlighted, with Obsidian links), cards that slipped (sure-and-wrong first), and leeches. | — |

## API

```
POST /api/study/debrief                      {since}
POST /api/study/{note}/{card}/triage         {action: keep|fix|drop|suspend, front?, back?}
GET  /api/study/{note}/{card}/source
POST /api/study/{note}/{card}/rewrite
GET  /api/decks/{note}/coverage
POST /api/decks/{note}/generate              {fill_gaps: true}
```

## What is still a guess

- `GROUND_SHARE` 0.6 / 0.4 and `LONG_CLOZE_WORDS` 28 are guesses, not measured.
  `_triage.jsonl` drops are the labelled data to tune them with.
- The model concept pass has only been tested with a fake provider. Before
  trusting it, run one real generation with Ollama up and read the output.

## Terms in bulk — paste a glossary (`sb/glossary.py`)

Write or paste one term per line. Each line becomes a Term note (from
`_templates/Term.md`) with a card you wrote yourself: active right away, no
model needed.

```
## Chapter 4 terms                     ← becomes "Seen in" for the terms below
Elasticity: how much demand responds to price
- **Opportunity cost** — the value of the next best alternative
Marginal utility = the extra satisfaction from one more unit
WIIFM :: What's in it for me?          ← `::` works even on a single line
Sunk cost<TAB>a cost that cannot be recovered   ← spreadsheet / Quizlet rows
```

| Surface | What happens |
|---|---|
| Dashboard capture box | Two or more term lines turn on the preview. Press **Resource** to see and edit each term and definition, then **File them**. |
| Ctrl+Alt+Z box (note mode) | Paste and press Enter. The server files the terms; the hotkey waits up to 30 s for a glossary instead of 2 s. |
| Obsidian plugin | "Send to Second Brain" on a glossary files terms, and the notice says how many. |
| Drop folder | A glossary file (including the offline fallbacks) is filed as terms. |
| Class session | Terms go into the session's folder and are counted in its summary. |

- A term already in the vault is not filed again. If it was saved without a
  definition, the paste fills in `## Definition` (your `## In my own words` is
  kept) and makes its card.
- "Terms with no definition" on the dashboard now has an input on each row:
  type the definition and press Enter.
- Not treated as terms: label lines (`Due:`, `Room:`, `Speaker:`), links,
  times, and pastes where fewer than 75% of the lines are term lines (60% for
  the preview). `"glossary": false` on `/api/capture` turns routing off.
