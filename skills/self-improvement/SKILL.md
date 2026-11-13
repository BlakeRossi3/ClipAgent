---
name: self-improvement
description: How the engineer turns a week of evidence (owner corrections, ignored check-ins, tool errors, eval failures) into lessons and at most three small, gated, owner-approved edits to skills and agent prompts.
---

# Self-improvement

## Inputs
- The weekly digest in your task: corrections and praise, check-ins answered vs ignored, tool errors,
  budget cut-offs, expensive runs, sub-agents used or never used, eval scores and regressions.
- `lessons_list` (what is already known), `eval_results` (what is measured).

## Lessons (cheap, no file edits)
- One `lessons_propose` per clear pattern seen at least twice. One specific sentence plus the evidence
  ("said so twice: Tue, Thu"). Areas: tone, hours, meals, memory, briefs, studio, checkins.
- Do not repeat a lesson that already exists; the owner approves lessons in one batch.

## Proposals (at most three a week; prefer none over a weak one)
1. Read the exact current text with `improvement_read` (or `skill_read`).
2. Make the smallest edit that fixes the pattern, as a unified diff (`--- a/<path>` / `+++ b/<path>`, one file).
3. `improvement_propose` with title, rationale, evidence, expected effect and the eval case ids that should move.
4. The result says whether the gate passed. If it failed, read the reasons; fix and resubmit once at most.

## The gate will refuse
- any path other than `skills/<name>/*.md`, `agents/<name>.md` or the vault routine note;
  `skills/persona/examples.md`, this skill and `agents/engineer.md`;
- changes to an agent's name, model or tools; removing or rewording a hard rule or a safety,
  approval, privacy, budget or medical line;
- more than 40 changed lines; diffs that do not apply; edits that break the registry or the offline evals;
- personal data in added lines. Skills are public files: write generic behaviour ("the owner", "{{honorific}}");
  personal preferences belong in lessons, which stay private.
