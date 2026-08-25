"""Obsidian templates matching the schema in models.py.

These exist so a note created by hand in Obsidian is indistinguishable from one
created by the capture UI — the system reads both, and neither path is second
class.

**These are deliberately minimal.** `write_templates` only writes a template
that does not already exist, so what ships in `_templates/` is the real
article: the same frontmatter plus the long comments explaining how each type
is meant to be filled in. What lives here is the schema-correct skeleton that
recreates a usable vault if `_templates/` is ever deleted. The rule for
keeping the two in step is that the **frontmatter must match exactly**; the
body prose is allowed to be thinner here. (Before this was written down, the
Project fallback had silently drifted a full schema revision behind.)
"""

from __future__ import annotations

from typing import Dict, List

from .vault import Vault

TEMPLATES: Dict[str, str] = {
    "Project.md": """---
id: "{{date:YYYYMMDD}}T{{time:HHmmss}}-project"
title: "{{title}}"
bucket: project
created: {{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}
updated: {{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}
tags: []
source: manual
project:
  status: active
  deadline:
  estimate_minutes: 60
  level: 3
  learning: false
  ideal_end:
  skills: []
  materials:
    - text: Reference or supply needed
      kind: material
      done: false
  steps:
    - id: s1
      text: First concrete action
      minutes: 30
      done: false
---

# {{title}}

**Done means:**

## Steps

- [ ] First concrete action (30m)

## Materials

- [ ] Reference or supply needed

## Capture
""",
    "Assignment.md": """---
id: "{{date:YYYYMMDD}}T{{time:HHmmss}}-assignment"
title: "{{title}}"
bucket: project
created: {{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}
updated: {{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}
tags: [assignment]
source: manual
category: hw
project:
  status: active
  deadline:
  estimate_minutes: 60
  level: 3
  learning: true
  ideal_end:
  skills: []
  materials: []
  steps:
    - id: s1
      text: "Why does [[Key Term]] behave the way it does?"
      minutes: 20
      done: false
---

# {{title}}

**Done means:**

## Steps

- [ ] Why does [[Key Term]] behave the way it does? (20m)

## Answers

### Why does [[Key Term]] behave the way it does?

## Materials

## Capture
""",
    "Paper.md": """---
id: "{{date:YYYYMMDD}}T{{time:HHmmss}}-paper"
title: "{{title}}"
bucket: project
created: {{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}
updated: {{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}
tags: [paper]
source: manual
category: hw
project:
  status: active
  deadline:
  estimate_minutes: 240
  level: 3
  learning: false
  ideal_end:
  skills: []
  materials: []
  steps:
    - id: s1
      text: "Intro — frame the question, state the thesis"
      minutes: 30
      done: false
---

# {{title}}

**Done means:**

## Steps

- [ ] Intro — frame the question, state the thesis (30m)

## Thesis

## Draft

## Materials

## Capture
""",
    "Quiz.md": """---
id: "{{date:YYYYMMDD}}T{{time:HHmmss}}-quiz"
title: "{{title}}"
bucket: project
created: {{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}
updated: {{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}
tags: [quiz]
source: manual
category: quiz
project:
  status: active
  deadline:
  estimate_minutes: 60
  level: 3
  learning: true
  ideal_end:
  skills: []
  materials: []
  steps:
    - id: s1
      text: Compile key concepts and link their atomic notes
      minutes: 20
      done: false
---

# {{title}}

**Done means:**

## Steps

- [ ] Compile key concepts and link their atomic notes (20m)

## Key Concepts

- [ ] [[Concept A]]

## Materials

## Capture
""",
    "Atomic Note.md": """---
id: "{{date:YYYYMMDD}}T{{time:HHmmss}}-atomic"
title: "{{title}}"
bucket: resource
created: {{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}
updated: {{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}
tags: [atomic]
source: manual
review:
  cycle_days: 90
  next:
---

# {{title}}

*From:*

## Definition

## In my own words
""",
    "Curiosity.md": """---
id: "{{date:YYYYMMDD}}T{{time:HHmmss}}-curiosity"
title: "{{title}}"
bucket: resource
created: {{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}
updated: {{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}
tags: [curiosity]
source: manual
category: study
review:
  cycle_days: 180
  next:
---

# {{title}}

## Roadmap

- [ ] [[First prerequisite]]

## Source
""",
    "Syllabus.md": """---
id: "{{date:YYYYMMDD}}T{{time:HHmmss}}-syllabus"
title: "{{title}}"
bucket: resource
created: {{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}
updated: {{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}
tags: [syllabus]
source: manual
category: hw
review:
  cycle_days: 120
  next:
---

# {{title}}

**Course:**
**Term:**

## Course Summary

### Sun Jul 5, 2026

- [ ] **Assignment** — [[Assignment title]] — due by 11:59pm

## Capture
""",
    "Area.md": """---
id: "{{date:YYYYMMDD}}T{{time:HHmmss}}-area"
title: "{{title}}"
bucket: area
created: {{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}
updated: {{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}
tags: []
source: manual
habit:
  cadence: weekly
  target_count: 3
---

# {{title}}

*Ongoing responsibility. No end state — reviewed on a cycle.*

## Check-in log
""",
    "Resource.md": """---
id: "{{date:YYYYMMDD}}T{{time:HHmmss}}-resource"
title: "{{title}}"
bucket: resource
created: {{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}
updated: {{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}
tags: []
source: manual
review:
  cycle_days: 90
  next:
---

# {{title}}

## Notes

## Source
""",
}

README = """# _system

Machine state for the Second Brain engine. Safe to exclude from Obsidian
search (Settings → Files & Links → Excluded files → add `_system`).

    calendar/   generated .ics — import or subscribe to it from your calendar
    index/      RAG embeddings cache (rebuildable; safe to delete)
    logs/       capture and calendar sync logs

Nothing here is a source of truth. Delete any of it and the engine rebuilds it
from the notes.
"""


def write_templates(vault: Vault) -> List[str]:
    vault.ensure_structure()
    written: List[str] = []
    tdir = vault.root / "_templates"
    for name, content in TEMPLATES.items():
        path = tdir / name
        if not path.exists():
            path.write_text(content, encoding="utf-8")
            written.append(name)
    readme = vault.root / "_system" / "README.md"
    if not readme.exists():
        readme.write_text(README, encoding="utf-8")
        written.append("_system/README.md")
    return written
