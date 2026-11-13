---
name: usage-aware
description: Plan your work around the owner's Claude plan usage - check what is left, defer non-urgent work until the window resets, and schedule prompts for later.
---

# Working within the Claude plan

You run on the owner's Claude subscription. It has a rolling 5-hour window and weekly windows, plus
a daily cap of runs that ClipAgent sets. Use them deliberately.

1. Before long or optional work (deep research, reel deep-dives, memory distillation, reflection,
   self-improvement, big code changes), call `usage_status`.
   - `left_pct` under 20 in any window, or `limited` true: do not start it now. Use
     `schedule_prompt` with `at: "after_reset"` and a self-contained prompt (every fact the later
     run needs), then tell him in one line when it will happen.
   - `left_pct` null means unknown (Claude Code only reports usage when it changes). Go ahead,
     but keep optional work short.
2. Anything he asks for directly and urgently: do it now while the plan allows.
3. He can ask you to do something "later", "tonight" or "after the reset": `schedule_prompt` with a
   local time `YYYY-MM-DD HH:MM` or `after_reset`. Choose `channel: "sms"` when he should get a text
   with the result, else the dashboard.
4. When the plan is at its limit, ClipAgent already queues his messages and tells him when they
   will run. Do not apologise repeatedly; one line with the reset time is enough.
5. Never invent usage numbers. Say "I can't see the exact figure" when `known` is false.
