# The deferred work, and what fires it

Roadmap Tier 4. All three are built. Each is gated on a trigger rather than on
a decision, so nothing needs remembering.

## FSRS weights fitted to you — `sb/fit.py`

**Trigger: ~1,000 reviews.** Enforced, not suggested: `python run.py fit
--write` refuses below it.

Every line of `_decks/_reviews.jsonl` has carried the card's pre-review state
since phase 2, written in for exactly this. Nothing had to change when the
trigger finally came into range.

FSRS-5 has nineteen free parameters. Below roughly a thousand reviews you are
not fitting a memory model, you are memorising a bad fortnight — that threshold
is Jarrett Ye's own optimizer guidance. A fitted-looking number is far more
dangerous than an obviously absent one, because the defaults are honest and an
overfit replacement would silently reschedule everything.

The loss is log loss on predicted retrievability — a proper scoring rule,
minimised by telling the truth rather than by being confident. Coordinate
descent, no dependency, every parameter kept inside the published bounds, and
the result is only offered if it beats the defaults on the same data. Weights
land in `_decks/_fsrs_weights.json`, beside the history they came from. A
number you type into `config.yaml` always wins: that is a decision, this is a
suggestion.

    python run.py fit             # show the fit
    python run.py fit --write     # save it

## numpy for the index — `sb/index.cosines`

**Trigger: 5,000 chunks** (roughly a few hundred notes), the figure phase 3's
docs already named.

Scoring a query is one dot product per chunk. In pure Python that is nothing at
500 chunks and most of a second at 5,000. Above the trigger, and only if numpy
happens to be installed, the whole matrix is multiplied at once.

numpy is deliberately **not** a requirement. A vault that never reaches 5,000
chunks should never be asked to install a 60MB dependency, and below the
trigger the import costs more than the arithmetic saves. Both paths compute the
identical quantity — the vectors are already unit vectors, so the cosine *is*
the dot product and there is no normalisation step to get wrong.
`python run.py doctor` reports which path a search will take.

## The Obsidian capture plugin — `.obsidian/plugins/second-brain-capture/`

**Trigger: capture friction becomes the reason a thought is lost.** Fogg's
ability axis — the cheapest capture wins, regardless of what the better tool
can do afterwards.

Three capture surfaces existed and all three require leaving the editor. A
thought you have *while writing a note* is the one most likely to be worth
filing and the one most likely to be lost, because filing it costs a context
switch.

To enable: Obsidian → Settings → Community plugins → turn them on → Installed
plugins → enable **Second Brain Capture**. Then bind a hotkey to "Capture to
Second Brain".

- Three commands: capture, capture selection, send this whole note.
- The three Area/Project/Resource buttons are the blueprint's manual
  classification. The click is the decision; there is no guess to confirm.
- Ctrl/Cmd+Enter files as a Resource — the safe default, since a Resource has
  no deadline to be wrong about and can be reclassified.
- It posts to the same `POST /api/capture` the dashboard uses: a second entry
  point, not a second implementation, so the note comes out parsed, planned and
  linked identically.
- If the app is not running the capture is written to the `Drop` folder and
  filed on the next scan. Losing a thought because a local server was shut is
  the exact failure this plugin exists to prevent, so it must not be the
  failure it introduces.

No build step, no `node_modules`: one `main.js` and a manifest, readable and
fixable in place.
