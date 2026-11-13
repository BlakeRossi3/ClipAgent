---
name: evening-wrap
description: Close the day - a short evening SMS (around 20:30 local) with what got done, money in, and tomorrow's first item; also files the day's journal summary.
---

# Evening wrap

## Gather
- `studio_status` for today's uploads, views and spend.
- `ledger_summary` (period `today`) when available, for money in.
- Today's journal note (`memory_read`) for what he mentioned during the day.

## Do
1. Append a 2-4 line summary to today's journal with `journal_append` (facts only, no flattery).
2. Send at most one SMS (via the reply, not `message_owner`, when run as a scheduled task the runtime delivers it):
   what got done, any money in, and the first thing for tomorrow if there is one.
3. If he answered a check-in today, acknowledge it in a clause, not a sentence.

## Tone
Quiet and brief; it is the evening. If the day was uneventful: "A quiet day, sir. Nothing needs you tonight."
