---
name: memory-curation
description: Keep the owner's vault accurate and tidy - file new facts in the right note with provenance, merge duplicates, resolve contradictions, process the Inbox. Use when saving memories and for the weekly tidy.
---

# Memory curation

## Filing a fact
- Use `memory_remember` - it chooses the note from the area, adds provenance and de-duplicates.
- Areas: about, work, projects, preferences, timeline, open_questions, finance, health, meals, decisions, agent, inbox, `person:<Name>`, `project:<Name>`.
- confidence `stated` only when he said it; `inferred` when you deduced it. Never upgrade an inference without him confirming.
- One fact per call, written as a plain sentence about him in the third person ("Prefers texts to calls before 9am.").

## Weekly tidy (archivist)
1. `memory_list` the Inbox; file each item with `memory_remember`, then `memory_forget` the inbox copy.
2. Look for contradictions between notes (e.g. two different job titles). Keep the newest stated fact; turn the conflict into an Open Questions entry if unclear.
3. Do not rewrite notes he edited by hand. If a note looks messy, append a short "Suggested tidy" section instead of restructuring it.
4. Journal a one-line summary of what you changed.

## Never
Store secrets (passwords, card numbers, codes). Store anything about third parties beyond what he told you and needs. Delete his own writing.
