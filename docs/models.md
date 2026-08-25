# Two models, two jobs

Every model call in the system names a **role**, and the role decides which
model answers it. There are two lanes.

| lane | config | jobs |
|---|---|---|
| fast | `llm.model` | `parse` — capture → project metadata |
| good | `llm.study_model` | `generate`, `grade`, `explain`, `ask` |

```yaml
llm:
  model: llama3.1:8b
  study_model: phi4
  study_roles: [generate, grade, explain, ask]
```

`study_model: ""` means one model for everything, which is the right setting on
a machine that cannot spare the VRAM.

---

## Why this split and not another

It is not about which jobs feel important. It is about what each job actually
needs.

**Parsing is on the fast lane** because it runs on every capture while you are
watching, and because `sb/extract.py` has already settled the part that most
determines whether the result is right — the date. The model's contribution is
a step breakdown and a difficulty guess, both of which are validated on the way
back and both of which a small model does perfectly well. A model finishing in
two seconds beats a better one finishing in fifteen, every time, for something
you do twenty times a day.

**Card generation is on the good lane** because what it writes is permanent.
A wrong flashcard does not merely sit in a file: spaced repetition will drill
it into you on an optimal schedule until you believe it. The guards in
`sb/generate.py` — citation checking, approval before scheduling — exist
because of exactly this, and a better model means fewer cards for those guards
to catch and fewer for you to reject by hand.

**Marking is on the good lane** because judging whether a typed answer means
the same thing as the reference is the hardest single call the system makes.
An 8B model marking "the mitochondrial matrix" against "inside the
mitochondria" as wrong costs you a card you actually knew.

**Explaining and asking are on the good lane** because they are teaching, and
because neither runs while you are mid-sentence.

Moving a role between lanes is a config edit, not a code change. If captures
start feeling slow because parse got promoted, or if generated cards are still
weak on the fast model, change `study_roles`.

---

## VRAM: the two lanes take turns

An RTX 4080 laptop has 12GB. `llama3.1:8b` is about 5GB at Q4 and `phi4` about
9GB, so both cannot be resident at once — Ollama evicts one to load the other,
which costs a few seconds.

That is fine, because the work comes in blocks: you capture things, then later
you study. What would not be fine is reloading between every flashcard. Hence
two keep-alive settings:

```yaml
  keep_alive: "5m"          # the fast lane lets go quickly
  study_keep_alive: "30m"   # the study lane holds through a session
```

A capture is one call and can afford to release its model. A review session is
fifty calls, and paying a reload on each would cost more than the answers are
worth. Embeddings use a short two-minute hold of their own: `nomic-embed-text`
is small, but an index rebuild calls it hundreds of times in a row and should
not evict whatever chat model you are about to use.

If you find the switching annoying, the fix is to set `study_model: ""` and run
one model for everything, not to fight the scheduler.

---

## When the study model is not pulled

Naming a model you have not downloaded must not break studying. Before using
the study lane, the installed tag list is checked — one cheap call to
`/api/tags`, cached for twenty seconds so a twenty-passage generation run does
not ask twenty times. An absent model falls back to the fast one, and the
reason is reported:

```
!! study llm  phi4 · flashcards, marking, explain, ask
              'phi4' is not pulled — using 'llama3.1:8b'. Run: ollama pull phi4
```

Tag matching handles the implicit `:latest` — `ollama pull phi4` reports back
as `phi4:latest`, and a config saying `phi4` matches it. Without that the
fallback would fire on a model sitting right there.

Ollama being **down** is a different failure and is not reported as a missing
pull: with no tag list at all the system does not know what is installed, so it
proceeds and lets the ordinary availability check handle the unreachable
server. Blaming a stopped server on a missing model would send you off pulling
something you already have.

---

## Checking it

```powershell
python run.py doctor
```

```
OK llm        ollama · llama3.1:8b
              installed: llama3.1:8b, phi4:latest, nomic-embed-text
OK study llm  phi4 · flashcards, marking, explain, ask
              everything else: llama3.1:8b
```

`/api/health` carries the same thing under `llm.lanes`, including a per-role
map of which model each job resolves to.

---

## Picking a study model

For a 12GB card, the 14B class at Q4 is the sweet spot — roughly 9GB, leaving
room for the embedding model.

**`phi4`** (14B) is the shipped recommendation. It is a STEM and reasoning
specialist, and — importantly for this system — it is not a chain-of-thought
model, so it behaves with the strict JSON that card generation and answer
marking require. A model that narrates its reasoning before answering is
actively worse here: `format: json` fights it, and the extra tokens are latency
you feel on every card.

Alternatives that fit the same budget: `qwen3:14b` (stronger general reasoning,
but has a thinking mode that wants disabling for the JSON roles) and
`gemma3:12b` (lighter, tuned toward question answering).

On 16GB, `gpt-oss:20b` becomes an option at about 14GB — but it leaves little
room alongside the embedder, so an index rebuild mid-session will thrash.
