---
id: "{{date:YYYYMMDD}}T{{time:HHmmss}}-atomic"
title: "{{title}}"
bucket: resource
created: "{{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}"
updated: "{{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}"
tags:
  - atomic
source: manual
review:
  cycle_days: 90
  next:
---

# {{title}}

*From:*

<!-- *From:* takes one link back to the guide this came from — [[parent note
     title]]. Blank when this was captured standalone rather than atomized out
     of an Assignment or a Curiosity.

     This is also the shape a plain capture takes: press the capture hotkey,
     type a line, and it arrives here. That is deliberate. Every captured note
     then has the ## In my own words heading already on it, which is the one
     slot that tells a note you used from a note you merely kept. -->

## Definition

<!-- The minimum needed to answer the question(s) it came from — nothing
     extra. If it wasn't needed for a question or roadmap item in the guide,
     it doesn't belong here. -->

## In my own words

<!-- Empty on purpose, and it stays that way: this slot is never filled in for
     you, not even on request. The retrieval effort of writing it is the whole
     point, which also makes a blank one a reliable signal that you haven't
     processed this note yet — see sb/collected.py, which counts exactly this.

     Both sections above are what flashcards are actually made of. generate.py
     resolves [[links]] one level deep, so when a Quiz or Assignment links
     here, this note is the passage the cards come from. -->
