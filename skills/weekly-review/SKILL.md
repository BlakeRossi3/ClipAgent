---
name: weekly-review
description: Sunday review of the owner's week - money, hours, goals, the Shorts business, meals and wellbeing - plus one suggestion for next week; also reviews the assistant's own performance and proposes lessons.
---

# Weekly review

## Gather
- `ledger_summary` (period `week`): hours, gross and net pay, reels revenue, other income.
- `studio_status`: the week's uploads, views, spend, the pipeline strategist's latest report.
- `goals_list`: progress on each active goal.
- This week's journal notes (`memory_search` the date range) and check-in answers.

## Write
1. A dashboard note `Journal/YYYY/YYYY-MM-DD.md` entry via `journal_append` with: money in, hours, goals progress, best and worst moment of the week (only if he mentioned them).
2. One SMS to him: the headline number, goal progress, one suggestion for next week. At most 320 characters.

## Self-review (lessons)
- Look at the week's agent runs and his replies: where did he correct you, ignore you, or answer "stop"?
- For each clear pattern, call `lessons_propose` with area, the lesson as one sentence, and the evidence.
- Lessons are proposals; the engineer turns adopted lessons into skill edits, which he approves.
