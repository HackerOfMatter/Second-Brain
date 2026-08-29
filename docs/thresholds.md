# The two numbers that decide things for you

`sb/threshold.py`, `sb/labels.py` · roadmap 1.2

Two cutoffs act on your behalf:

| | where | what it does |
|---|---|---|
| `connect.AUTO_FLOOR` | 0.62 | above this, a suggested link is written into the note without asking |
| `intake.auto_floor` | 0.60 | above this, a dropped file is filed without asking |

Both were picked by thinking about it for a minute. Manning, Raghavan &
Schütze (*Introduction to Information Retrieval*, ch. 8) put it plainly: a
retrieval threshold without a labelled evaluation set is a **preference, not a
parameter**.

## You do not have to sit down and label sixty pairs

The system already produces labelled examples every time it asks a question and
you answer it:

- a note held below the intake floor, and the bucket you chose → a
  `(confidence, right or wrong)` pair;
- a suggested link you keep or delete → a `(score, right or wrong)` pair.

They go to `_labels/`, **not** `_system/`. `_system/` is disposable by
declaration — the doctor may suggest deleting it — and labelled data is the
opposite: it accrues one confirmation at a time over months and cannot be
regenerated. A labelled set a cache clear destroys is one you will never
accumulate.

## The recommendation

`python run.py thresholds` sweeps every cutoff and reports precision, recall
and how often you would be asked. The recommendation is **the lowest cutoff
whose precision clears 0.9**.

Precision is the constraint, not F1, and that is specific to what these
thresholds do. A false positive is a note in the wrong folder or a wrong link
written into a note — errors that persist silently and that you may never look
at again. A false negative is a question on the dashboard. Those costs are not
symmetric, so optimising a metric that treats them as equal would be optimising
the wrong thing.

If no cutoff in range reaches the target, it says so, and says that the scoring
rules are the problem rather than the threshold. A cutoff cannot rescue a
signal that is not there.
