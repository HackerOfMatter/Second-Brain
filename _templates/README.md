# _templates

The shape every note takes, whoever writes it — you in Obsidian, or the capture
box. `sb/render.py` reads these files and fills them in, so **editing a template
here changes what the system writes.**

## One rule, and it matters

**Never put a `#` comment inside the `---` frontmatter block.**

Obsidian's Properties editor rewrites frontmatter it cannot round-trip, without
warning. Between 23 August and 14 September it corrupted every template in this
folder: it flattened `habit:`/`cadence:` into siblings, renamed `cycle_days` to
`cycle days` and `materials` to `material`, turned inline comments into string
values, and left the YAML it could not parse sitting in the note body after a
stray `---`. A note made from any of them arrived with no id and no bucket, and
the system refused to read it.

Explanations belong in the body as `<!-- HTML comments -->`. Obsidian leaves
those alone, the person filling the note in can see them, and `render.py` strips
them out of generated notes so a term note does not carry a tutorial.

If you would rather not risk it at all: **Settings → Editor → Properties in
document → Source**. That turns off the Properties UI and shows the YAML as
text, which is what it is.

## If one breaks

    python run.py templates --repair     # put them all back
    python run.py doctor                 # check them without changing anything

`doctor` parses every template and builds a real note from it, so drift is
reported the week it happens rather than the next time you make a note by hand.
