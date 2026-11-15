# Cecil roadmap (backlog)

Items Blake has asked for that are queued behind the current work.

## Next

- **Trusted skill installer** (not main priority, per Blake 2026-11-13): install skills only from approved
  sources (start with Anthropic's official skills: documents, spreadsheets, slides, PDFs), pinned to an
  exact version; Cecil summarises each skill and flags risks, Blake approves on a dashboard card; new
  skills start restricted (no secrets, no outbound messaging). Scanners are advisory only - see
  docs/JARVIS_RESEARCH.md and the reel skill-vetting notes.
- **Google calendar + email read access** (from docs/JARVIS_RESEARCH.md): read-only first; anything sent
  or changed goes through an approval (and messaging anyone else can never become a standing approval).

## Done

- Heartbeat / proactive checklist (`assistant/heartbeat.py`, vault `Agent/Heartbeat.md`, only Blake edits it).
- "Always yes" standing approvals (`assistant/approvals.py`; `YES ALWAYS <id>`, dashboard *Always allow*,
  keypad 3 / "always" on the approval call; Settings -> Approvals to revoke; spending, public posts,
  messaging others, deletions, unknown third-party code and strategy changes always ask).
- Automatic fact extraction after every conversation (`assistant/auto_memory.py`, `ASSISTANT_AUTO_MEMORY`).
- WhatsApp/SMS voice notes transcribed with ffmpeg + faster-whisper (`messaging.transcribe_voice_notes`).
