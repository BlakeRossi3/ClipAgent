---
name: usage-aware
description: Plan your work around the owner's Claude plan usage - check what is left, schedule work for later, and hand work over to the next usage window before the limit stops you.
---

# Working within the Claude plan

You run on the owner's Claude subscription. It has a rolling 5-hour window and weekly windows, plus
a daily cap of runs that ClipAgent sets. Use them deliberately.

1. Before long or optional work (deep research, reel deep-dives, memory distillation, reflection,
   self-improvement, big code changes), call `usage_status`.
   - `left_pct` under 20 in any window, or `limited` true: do not start it now. Use
     `schedule_prompt` with `at: "after_reset"` and a self-contained `text` (every fact the later
     run needs), then tell him in one line when it will happen.
   - `left_pct` null means unknown (Claude Code only reports usage when it changes). Go ahead,
     but keep optional work short.
2. Anything he asks for directly and urgently: do it now while the plan allows.
3. Scheduling for later ("later", "tonight", "every morning", "in an hour"): `schedule_prompt` with
   `at` = `in 45 minutes`, `7:10am`, `tomorrow 9am`, `YYYY-MM-DD HH:MM` or `after_reset`, and
   `repeat` = daily / weekdays / weekly when it should recur. Give it a short `title` and a one-line
   `why`. It runs as a background task with your full powers and reports back by itself. Choose
   `channel: "sms"` when he should also get a text with the result. `schedule_list` shows everything
   planned (the Scheduled page); `schedule_cancel` removes one of yours.

## Handing off before the limit (important)

When usage is getting low - `usage_status` shows `left_pct` at 10 or less in any window, `limited`
is true, or a run tells you the limit is near - and you are in the middle of real work, stop starting
new steps and call `usage_handoff` BEFORE the work is cut off:

- `summary`: what you were working on, in one or two sentences.
- `done`: what is finished (files changed, facts found, decisions made).
- `left`: what is not done yet.
- `next_steps`: the exact next steps, in order, each one actionable on its own (file paths,
  commands, the precise question to answer). Write them for a fresh session that remembers nothing.
- `files`: relevant files and vault notes.

The handoff is saved to `Agent/Handoffs/` in the vault and ONE resume task is scheduled for a few
minutes after the window resets ("Resume from handoff <path>"). A second handoff joins the same
resume. Then tell him in one line, e.g. "I'm nearly out of plan for now; I've saved where I am and
I'll pick it up at 4." ClipAgent also writes an automatic handoff for background tasks still
running when a window passes 90% or a task is stopped by the limit, but yours is better: write it.

When a resume task runs, it carries the handoff notes: follow the next steps in order, skip what is
already finished, and note at the end of each handoff that it was resumed and finished.

4. When the plan is at its limit, ClipAgent already queues his messages and tells him when they
   will run. Do not apologise repeatedly; one line with the reset time is enough.
5. Never invent usage numbers. Say "I can't see the exact figure" when `known` is false.
