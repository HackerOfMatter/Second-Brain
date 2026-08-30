
> 💡 Use the **Templater plugin** to auto-fill the date/week info.

---

## 📆 **Weekly To-Do Template**


```markdown
---
type: weekly
week: <% tp.date.now("WW") %>
domain: (Work / Study / Personal)
---

# 🗓️ Week <% tp.date.now("WW") %> — {{domain}}

### 🎯 Weekly Goals
1. 
2. 
3. 

### 📋 Tasks
- [ ] 
- [ ] 
- [ ] 

### ⏰ Tasks with Deadlines
```tasks
path includes "Daily"
due after this Monday
due before next Monday
