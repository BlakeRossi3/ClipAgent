# Cecil roadmap (backlog)

Items Blake has asked for that are queued behind the current work.

## Next

- **Google calendar + email read access** (from docs/JARVIS_RESEARCH.md): read-only first; anything sent
  or changed goes through an approval (and messaging anyone else can never become a standing approval).

## Done

- Trusted skill installer (`assistant/skill_catalog.py`, Settings -> Skills at `/dashboard/skills`): catalog of
  anthropics/skills (+ `SKILLS_TRUSTED_REPOS`) pinned to commit SHAs, cached 24 h, update-available per skill
  folder (never auto-applied); quarantine + static scan + Cecil's summary -> card (SAFE one tap, CAUTION with a
  tick, UNSAFE never); Python packages detected, pinned on PyPI with a Windows-wheel check, installed only on
  approval; installs start restricted (not mirrored to Claude Code, outbound tools blocked once read, folder
  and manifest edit-denied) until promoted; uninstall archives to `skills/.archive/`. Only Blake approves;
  skill installs can never become standing approvals. Limit: a skill's scripts run as his user if Cecil runs
  them (deny rules bind Claude Code's tools, not child processes).
- Heartbeat / proactive checklist (`assistant/heartbeat.py`, vault `Agent/Heartbeat.md`, only Blake edits it).
- "Always yes" standing approvals (`assistant/approvals.py`; `YES ALWAYS <id>`, dashboard *Always allow*,
  keypad 3 / "always" on the approval call; Settings -> Approvals to revoke; spending, public posts,
  messaging others, deletions, unknown third-party code and strategy changes always ask).
- Automatic fact extraction after every conversation (`assistant/auto_memory.py`, `ASSISTANT_AUTO_MEMORY`).
- WhatsApp/SMS voice notes transcribed with ffmpeg + faster-whisper (`messaging.transcribe_voice_notes`).
