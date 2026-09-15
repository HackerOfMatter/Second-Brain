---
id: "{{date:YYYYMMDD}}T{{time:HHmmss}}-area"
title: "{{title}}"
bucket: area
created: "{{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}"
updated: "{{date:YYYY-MM-DD}}T{{time:HH:mm:ss}}"
tags: []
source: manual
category:
habit:
  cadence: weekly
  target_count: 3
schedule:
  enabled: true
  time: "18:00"
  duration_minutes: 30
  days: []
  monthday: 1
  start:
  until:
---

# {{title}}

*Ongoing responsibility. No end state, so no due date — it holds a recurring
block of real time instead. Change the time, length or days in the dashboard,
or at the weekly schedule review.*

<!-- FRONTMATTER NOTES — down here, because comments inside the YAML block are
     what corrupted every template in this vault once already.

     category       blank = detected from the words you wrote
     habit.cadence     daily | weekly | monthly
     schedule.days     weekday numbers, Monday = 0. Empty = every day the
                       cadence allows.
     schedule.monthday day of the month, used only when cadence is monthly.
                       It needs a number, not a blank — leaving it empty is
                       what makes this file unreadable. -->

## Capture

## Check-in log
