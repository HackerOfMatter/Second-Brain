---
link: "[[Weekly example trent]]"
---



🔍 Theoretical background Two inter-related literatures help explain what’s going on: goal orientation theory and deep vs. surface learning.

• Goal Orientation Theory Students who adopt a mastery (learning) goal orientation aim to improve their understanding and competence. Research shows this orientation leads to deeper engagement and more effective use of learning strategies. Boston University +2 Educational Psychology +2

In contrast, a performance goal orientation focuses on demonstrating competence (e.g., “get the correct answers”, “look good in front of peers/instructor”) rather than truly learning. This orientation is associated with more shallow strategies. ERIC +1

For Trent’s situation: the structure of homework/quizzes pushes toward just get it done / find info rather than understand it deeply. So his environment encourages a performance orientation rather than mastery.

• Deep vs. Surface Learning Surface learning involves memorizing or reproducing information with minimal understanding of underlying meaning, often done when tasks demand only lookup or recall. economicsnetwork.ac.uk +1

Deep learning involves making connections, understanding the “why”, applying knowledge to new contexts, asking questions, synthesizing. This strategy leads to more durable understanding. BioMed Central +1

The research suggests that when assessments reward shallow recall (as you describe), students naturally adopt surface strategies. PMC

✅ Strategies for Trent to bridge the gap Since Trent wants to learn deeply, but the system rewards surface approaches, here are practical moves he can make:

1. Adopt a mastery goal mindset Reframe tasks: instead of “I must finish this homework so I pass the quiz”, think “I will finish this homework so I understand this topic and can apply it later”.

Set personal learning goals in addition to performance goals. For example: “By the end of this week I will explain distribution channels in my own words and give two real-world examples.”

When you do tasks, ask yourself: “What will I remember / be able to use in the future?” rather than just “What will be on the quiz?”

2. Use deep learning strategies even when tasks don’t require them When you copy/paste notes, rewrite in your own words, teach it to someone else (or an imaginary student), make connections to other concepts. That forces deeper processing.

After finishing the homework/quiz lookup, spend 5-10 minutes asking: “How does this topic link to ___?” or “What if X changed?”

Create mini-application questions for yourself: “If the company changed channels from direct to indirect, what would change in cost, margins, customer experience?”

3. Leverage the structure of the task environment If tasks reward finding where info is, use that as a first step, but then add a second step of your own: e.g., “Locate the information → Summarize it → Pose a critique or extension.”

Build a cheat-sheet of “key concepts + my own extension” after each homework. So even if quiz expects recall, you’ve prepared deeper understanding.

4. Make learning visible and meaningful to yourself Use a note in Obsidian (or whatever system you use) with YAML metadata like:

## yaml Copy code

## topic: Distribution Channels task: HW Chapter 10 personal_goal: “Explain channel conflict in own words + give two examples” // after completing mastered: false

Then, after you intentionally use a deep strategy, mark mastered: true.

Use Dataview (if you use Obsidian) to track topics you feel you’ve understood deeply versus just “completed”.

5. Align with your values Use your intrinsic motivation (you want to learn) as a resource. Remind yourself: learning deeply is “why you’re here”, not just “what you’re doing to finish the quiz”.

Consider noting a faith or values connection: e.g., “Stewardship of mind: I want to develop my capacity fully.” This helps anchor your deeper intention.

🧮 How Trent might implement this in one semester Step Action Week start Pick 1-2 “big ideas” you want to own this week (not just passively complete). Each homework After finishing required tasks, spend 5 minutes: write “Why does this matter?”, “How could I apply this in a job scenario?”, “What would happen if I changed one thing?” Quiz study Instead of only memorizing location of info, create one “application question” you answer in your own words. Weekly review In Obsidian, mark which topics you feel confident to explain and which you don’t. Use Dataview to track:

dataview Copy code TABLE topic, mastered FROM "ClassNotes" WHERE course = "Principles of Marketing" | End of semester | Reflect: Which topics do you still recall and apply? Which were just “done” and forgotten? |

## 🚧 Potential pitfalls & how to handle them Time pressure makes surface strategies tempting. ==Solution: schedule short, consistent deeper-review sessions== (even 10 minutes/day helps).

Lack of assessment for deeper learning may demotivate. Solution: create your own small “self-quiz” or explanation exercise.

Feeling the homework “doesn’t matter” because quiz is easy. Solution: think long-term: this knowledge will help you in future classes/jobs, not just now.

📝 Summary for Trent You are in a situation where the system rewards finding information quickly rather than understanding deeply. The research shows that you can **choose to adopt a mastery/learning orientation** (which leads to deeper, more lasting understanding) rather than just a performance orientation (which leads to shallow strategies). By incorporating ==small, deliberate deep-learning moves== (rewriting, applying, self-questioning), you’ll bridge your desire to learn with the demands of homework/quizzes. Over time, you’ll build real competence rather than just completing assignments.



## 🔁 4. **Learning Techniques Folder**

Inside `📖 Learning Skills/`, include these notes:

- **How to Learn.md:** quick reminders of evidence-based study habits (spaced repetition, retrieval practice, elaboration).
    
- **Active Recall Prompts.md:** example self-quizzes for each topic.
    
- **Spaced Repetition Tracker.md:** YAML list with review intervals (1 day, 3 days, 7 days, 14 days).
    

---

## 🧠 5. **Key Learning Philosophy**

Trent’s vault should support:

| Learning Goal        | Obsidian Tool                                                  |
| -------------------- | -------------------------------------------------------------- |
| Capture content      | Daily notes                                                    |
| Connect ideas        | Linking notes (e.g., [[Consumer Behavior]] ↔ [[Segmentation]]) |
| Review intentionally | Weekly review notes                                            |
| Practice retrieval   | Active recall questions                                        |
| Track growth         | Dataview dashboards                                            |

---

# how to take notes
| Layer                              | Purpose                                     | When to use            | Tools                                       |
| ---------------------------------- | ------------------------------------------- | ---------------------- | ------------------------------------------- |
| **Layer 1: Pre-Class Primer**      | Light preview — create mental “hooks”       | 10–15 min before class | Obsidian note pre-filled with key questions |
| **Layer 2: Live Class Capture**    | Record structure + cues, not full sentences | During class           | “Skeleton notes” template + shorthand       |
| **Layer 3: Post-Class Processing** | Rewrite for clarity + add “Teach-Back”      | Within 24 hrs          | Obsidian note expansion                     |

