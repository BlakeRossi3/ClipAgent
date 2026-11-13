---
name: morning-brief
description: Compose the owner's morning brief (one SMS, sent around 07:30 local). Use for the scheduled morning job or when he asks "what's on today".
---

# Morning brief

Goal: in at most 320 characters, tell him what matters today and nothing else.

## Gather (tools)
1. `studio_status` - yesterday's uploads and views, anything pending his approval, budget problems.
2. `goals_list` - any goal due within 7 days.
3. `memory_search` for today's date and "today" - appointments or reminders he asked for.
4. Yesterday's journal via `memory_read` (`Journal/YYYY/YYYY-MM-DD.md`) for loose ends.

## Compose
- Order: anything needing his action first, then one line of good news or numbers, then (optional) the day's single most useful suggestion.
- If nothing needs him, say so: "Nothing needs you today."
- Numbers rounded and comparative where helpful ("up a third on last week").
- Start with "Good morning, sir." only if the message still fits.

## Do not
- List every metric. Mention the pipeline only if it did something worth knowing or needs him.
- Send a brief that says nothing new two days running; send a single line instead.
