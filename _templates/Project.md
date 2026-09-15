---
id: "{{date:YYYYMMDD}}T{{time:HHmmss}}-project"
title: "{{title}}"
bucket: project
created: "{{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}"
updated: "{{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}"
tags: []
source: manual
category:
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

<!-- FRONTMATTER NOTES — kept here rather than up there, because Obsidian's
     Properties editor swallows comments inside the YAML block and has
     corrupted every template in this vault once already.

     category      blank = detected from the words you wrote (hw, study, quiz)
     materials     one list, three kinds: material | hardware | software.
                   The ## Materials section below is rendered FROM this,
                   including the Hardware and Software subsections, which used
                   to be hand-written headings the dashboard wiped on every
                   re-render.
     steps         each concrete enough to start with no further planning. -->

## Steps

- [ ] First concrete action (30m)

## Materials

<!-- Rendered from project.materials above. Tick a box here and the state is
     read back into frontmatter on the next render, so ticking in Obsidian
     sticks. Hardware and Software appear as ### subsections when a material
     is given that kind. -->

- [ ] Reference or supply needed

## Capture
