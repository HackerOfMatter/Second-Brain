---
type: daily
date: <% tp.date.now("YYYY-MM-DD") %>
week: <% tp.date.now("WW") %>
---

# 📅 Daily To-Do — <% tp.date.now("dddd, MMMM D, YYYY") %>

### 🔗 Current Week Overview
- [[Week <% tp.date.now("WW") %> - Work]]
- [[Week <% tp.date.now("WW") %> - Study]]
- [[Week <% tp.date.now("WW") %> - Personal]]

### 🌞 Morning Focus
> What are the 3 most important things I must complete today?

1. [ ] MIT 1 — 
2. [ ] MIT 2 — 
3. [ ] MIT 3 — 

### 📋 Tasks
- [ ] 
- [ ] 
- [ ] 
### 🗓️ Monday
| Time | Task / Focus | Status |
|------|---------------|--------|
| 8–10 AM |  |  |
| 10–12 PM |  |  |
| 1–3 PM |  |  |
| 3–5 PM |  |  |
| 7–9 PM |  |  |

### 🔁 From Weekly Lists
`tasks
not done
path includes "Weekly"
due before tomorrow

## 🗓️ Weekly Calendar
| Day | Date                                                   | MIT | Notes |
| --- | ------------------------------------------------------ | --- | ----- |
| Mon | <% tp.date.now("MM-DD", -((tp.date.now("d")-1)%7)) %>  | [ ] |       |
| Tue | <% tp.date.now("MM-DD", 1-((tp.date.now("d")-2)%7)) %> | [ ] |       |
| Wed | <% tp.date.now("MM-DD", 2-((tp.date.now("d")-3)%7)) %> | [ ] |       |
| Thu | <% tp.date.now("MM-DD", 3-((tp.date.now("d")-4)%7)) %> | [ ] |       |
| Fri | <% tp.date.now("MM-DD", 4-((tp.date.now("d")-5)%7)) %> | [ ] |       |
| Sat | <% tp.date.now("MM-DD", 5-((tp.date.now("d")-6)%7)) %> | [ ] |       |
| Sun | <% tp.date.now("MM-DD", 6-((tp.date.now("d")-7)%7)) %> | [ ] |       |
| Mon | <% tp.date.now("MM-DD", -((tp.date.now("d")-1)%7)) %>  | [ ] |       |
|     |                                                        |     |       |

