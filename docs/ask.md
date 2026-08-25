# Ask — the info manager

Blueprint §7. A question with no note attached, answered out of everything you
have filed, with citations back to the notes it came from.

The **Ask** tab on the study page, or `POST /api/ask`.

This is the other half of the tutor. *Explain this* on a flashcard answers from
that card's own source note and needs no index at all — retrieval is trivial
when you already know which note to read. Ask is the case where you don't.

---

## What it will and won't do

**It answers from your notes, not from the model's training.** Retrieval runs
first. If nothing comes back above the score floor, the answer is "nothing in
your resources covers that" — not a fluent paragraph about what the model
happens to know. A model asked "what did I decide about the deposit?" will
otherwise invent a plausible deposit, and a personal knowledge base that
quietly stops using your notes is worse than one that admits it has nothing.

**Every claim is cited.** Sources are numbered, the model is told to cite them
inline, and the citations in the answer are clickable — they scroll to the
source they name. Sources that were retrieved but not cited are dimmed, so you
can see what the answer actually leaned on.

**Archive is opt-in.** Day-to-day retrieval covers Resources. Archiving
something is a decision that it is no longer relevant, and searching it anyway
would make the Archive bucket meaningless. Tick *include archive* to widen it;
archived sources are labelled in the results.

**It says when it disagrees with itself.** If two notes conflict, the model is
instructed to say so and cite both rather than silently pick one.

---

## The index

Lives in `_system/index/`, and — unlike `_decks/` — it **is** disposable.
Every byte derives from the notes, so deleting it costs one rebuild. That is
the same rule as the calendar, and it is why the index is allowed under
`_system/` at all.

```
manifest.json   model, dimension, count, when it was built
chunks.jsonl    one record per chunk: note, heading trail, text
vectors.f32     raw little-endian float32, row-major, row i ↔ line i
```

**Chunking** splits each note on headings, packs paragraphs up to ~900
characters, and windows anything longer with overlap so a sentence split
across a boundary survives whole somewhere. Every chunk is prefixed with its
note title and heading trail. That costs a few tokens and buys a lot: "400mg"
is a useless chunk on its own and a good one as "Ibuprofen › Dosage — 400mg".

**Embedding** uses `nomic-embed-text` through Ollama — already in your config,
already pulled. Vectors are normalised at write time, so every comparison at
query time is a plain dot product rather than a cosine.

**Incremental by fingerprint.** Each chunk carries a hash of the note it came
from. On rebuild, chunks whose note is unchanged keep their vectors untouched,
chunks whose note was edited or deleted are dropped, and only the difference is
sent to the model. Editing one note out of two hundred re-embeds one note.

**Staleness is computed, not trusted.** The Ask tab compares what is indexed
against what is in the vault right now, so a note you edited in Obsidian while
the app was closed shows up as needing a re-index. It says *2 new, 1 edited* in
plain words rather than showing a flag someone forgot to clear.

**Pure Python, on purpose.** The whole system is five packages and numpy is not
one of them. Cosine over a few thousand chunks is tens of milliseconds, which
is nothing next to the model call that follows. Good for roughly 5,000 chunks —
a few hundred notes. Past that, the honest fix is to add numpy, not to pretend
a list comprehension is a vector database.

---

## Ranking

Score is cosine similarity plus a small keyword bonus (0.15 × the fraction of
your question's content words that appear in the chunk). The bonus exists
because exact terms — a course code, a person's name, a scheme name — carry
more signal than their vectors suggest. It is small enough to break ties rather
than drive the ranking.

Two guards on what comes back:

* **A score floor.** Below 0.28 a "match" is noise, and the honest answer is
  that there isn't one.
* **A per-note cap** of 2 chunks. Without it one long note wins every slot and
  the answer reads like a summary of that note rather than of what you know.

---

## With no model running

Nothing fails. The index builds without vectors and search falls back to
keyword scoring — clearly labelled as *keyword only* in the Ask tab, with a
lower floor since keyword scores are scaled differently. You still get the
right passages back; you just get the passages rather than prose written from
them. Same rule that governs capture, date parsing and card generation.

If embedding fails partway through a build, **no** vector file is written
rather than a partial one: mixing rows with and without vectors would break the
row-*i*-to-line-*i* contract, and returning confidently wrong neighbours is
worse than returning keyword matches. The same check runs at load time, so a
vector file that does not line up with the chunk file is refused outright.

---

## API

| route | what |
|---|---|
| `POST /api/ask` | `{question, include_archive?, k?}` → answer, numbered sources, which were cited |
| `GET /api/index` | counts, semantic or keyword, and what is new / edited / gone since the last build |
| `POST /api/index/rebuild` | `{force?}` — incremental unless forced |

Asking a question with no index builds one first. Asking is a clear enough
statement of intent that you should not also have to find a button.

A rebuild that could not embed returns `warning`, not `error` — a keyword-only
index is a degraded success, and the two must not be confused. (They were, once:
the UI treated a working rebuild as a failed call and left the status line
stale. Caught in a browser, not by a test, which is why there is now a test.)
