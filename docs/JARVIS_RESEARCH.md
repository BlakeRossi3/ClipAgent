# Jarvis research: what to borrow for Sterling

Written 2026-11-13. Inputs: the two repos Blake sent (shallow-cloned and read), plus a web scan
of the "Jarvis" agents people are talking about. Treat the web claims as marketing until proven.
Sterling today: SMS / dashboard / outbound call, Obsidian-vault memory, sub-agents with skill
files, check-ins, ledger, meals, lessons/self-improvement, approvals by phone (see `ASSISTANT.md`).

## 1. open-jarvis/OpenJarvis (Stanford, Apache-2.0, Python)

**What it is.** A research framework for *local-first* personal AI. It is built on five primitives
(Intelligence/model catalog, Engine (Ollama, vLLM, llama.cpp...), Agents, Memory, Learning) wired
together through decorator registries. It ships eight agents (morning digest, deep research,
continuous "operative"/monitor, ReAct, CodeAct...), about 35 chat channels (Twilio SMS, iMessage,
WhatsApp, Telegram...), about 40 connectors (Gmail, Calendar, Drive, Notion, Obsidian, Oura,
Strava, Spotify...), and a Tauri desktop app.

**Worth borrowing**
- **Proactive propose → approve → execute with permission memory** (`tools/proactive_tools.py`,
  `tools/approval_store.py`). Actions queue as `pending_actions`. The owner replies `<id> yes`,
  `<id> no` or **`always yes <id>`**, and the "always" answer is stored as a rule keyed
  `action_type:fingerprint` (e.g. `sms_draft_reply:contact:+1555...`). Sterling's decisions are
  all one-off. This would cut approval fatigue a lot.
- **Background fact extraction** (`memory/extractor.py`). After each exchange, a cheap model pulls
  out durable facts on a background thread. On any failure it records "no facts". Sterling only
  remembers what the model explicitly decides to `memory_remember`.
- **Morning digest with TTS audio** (email + calendar + health + news). Sterling's `morning-brief`
  skill could also be phoned in as audio.
- **Skill overlays** (`skills/overlay.py`). Trace-optimised descriptions and few-shot examples are
  stored in a sidecar file next to a skill without editing the skill itself. This fits
  `assistant/improve.py`'s lessons loop.
- **Security wrappers**: secret/PII scanner with WARN/REDACT/BLOCK modes, **taint tracking** so
  private data cannot flow into outbound tools (`security/taint.py`), and SSRF guard.
- Skills follow the agentskills.io `SKILL.md` standard, which is the same shape as Sterling's
  `skills/`.

**Not applicable.** Local-model engines, routing and RL training (we use Claude). The
energy/FLOPs benchmarking. Most of the 35 channels. Mass skill import from OpenClaw/Hermes hubs
(that is a malware vector; see section 3).

## 2. adewaskar/jarvis (MIT, React + Node)

**What it is.** An Iron-Man browser HUD: Three.js arc reactor, boot sequence, "Hey Jarvis" voice.
The "brain" is **Claude Code run headless through the Claude Agent SDK** (`bridge/server.mjs`,
about 1.6k lines). It reuses the owner's Claude Code login, so it needs no API key and spawns
every MCP server in `~/.claude.json`.

**Worth borrowing**
- **Pre-decided permission policy instead of spoken confirmations** (`bridge/policy.mjs`). It
  starts read-only. Effectful tools (send, tap, delete, pay) are allowed only with
  `JARVIS_ALLOW_WRITES`, judged by tool-name verbs. It is enforced in a `PreToolUse` hook *and*
  `canUseTool`, with `settingSources: []` so no global bypass can override it. File tools are
  confined to a workspace, and `~/.ssh`, `~/.claude` and `.env` are always refused. Our
  Claude-Code-access work should copy this pattern.
- **Real-Chrome control through the Claude in Chrome extension's native-messaging pipe**
  (`bridge/chrome.mjs`). The reasoning: Playwright is a fresh profile that hits login walls and
  bot checks, while the user's own Chrome is already signed in. This confirms the teammates'
  Chrome direction.
- **Voice loop design** (`src/lib/voice.ts`, `vad.ts`, `capabilities.ts`):
  - local energy VAD, which makes **barge-in** possible;
  - STT tiers chosen automatically at boot (ElevenLabs Scribe if a key exists, else browser
    SpeechRecognition with a heartbeat);
  - TTS falls back from ElevenLabs to Kokoro to the system voice;
  - a `/health` capability probe, so adding a key upgrades the stack with no flags;
  - the wake word uses Porcupine (`hey jarvis`), with a clap detector as an alternative.
- **Filler lines** (`src/lib/fillers.ts`). He says one in-character acknowledgement the moment a
  tool starts ("Cross-referencing.", "Working on it, sir."), because ten seconds of silence reads
  as a crash. The grammar rules match Sterling's persona: no "let me", no exclamation marks, and
  "sir" at the end for routine, at the front for urgent.
- **Model-driven HUD**. A `display` tool renders sanitised panels (DOMPurify, a class allowlist,
  CSP). Remote media is proxied through an SSRF-guarded `/img`. This is a cheap path to a
  "Jarvis" dashboard view.
- Camera "look at me" vision. It is gated by OS permission, an on-screen indicator, and explicit
  owner intent only.

**Not applicable.** A browser-only face (Sterling must also work by SMS). MediaPipe hand-gesture
cursor and clap-to-wake (fun, but low value). Android/Higgsfield MCPs.

## 3. The web scene (2025-2026)

- **OpenClaw** (formerly Clawdbot, then Moltbot; by Peter Steinberger) is *the* viral "Jarvis",
  from early 2026. It is a self-hosted gateway that you text from WhatsApp, Telegram or iMessage.
  - Its persona and memory are plain files: `SOUL.md`, `MEMORY.md` and daily logs.
  - A **heartbeat** reads `HEARTBEAT.md` every ~30 min and acts on it unprompted, which is what
    makes it feel alive.
  - It has shell, file and browser tools, writes its own new skills on request, and has the
    ClawHub skill marketplace.
  - What people rave about: it *does things* and texts you first. Coverage reports it sold out
    Mac Minis.
  - What's real versus hype: ClawHub audits found **~12% malicious skills** (the AMOS infostealer
    campaign). SOUL.md/HEARTBEAT.md poisoning persists across restarts. 40k+ exposed instances
    were reported around CVE-2026-25253. API bills add up. Many demos were never verified.
  - Sources: [Towards AI](https://pub.towardsai.net/openclaw-inside-the-architecture-and-impact-of-the-viral-autonomous-agent-838c53f512d8),
    [Medium (Clawdbot rise)](https://medium.com/@gwrx2005/clawdbot-moltybot-a-self-hosted-personal-ai-assistant-and-its-viral-rise-520427c6ef4f),
    [Adversa security 101](https://adversa.ai/blog/openclaw-security-101-vulnerabilities-hardening-2026/),
    [CSA prompt-injection note](https://labs.cloudsecurityalliance.org/wp-content/uploads/2026/06/CSA_research_note_openclaw_indirect_prompt_injection_20260613-csa-styled.pdf),
    [CSA hardening guide](https://labs.cloudsecurityalliance.org/agentic/agentic-openclaw-hardening-guide-v1/),
    [besthub architecture](https://www.besthub.dev/articles/inside-openclaw-architecture-core-technologies-and-security-risks-88f4feb05ddb),
    [bitdoze security guide](https://www.bitdoze.com/openclaw-security-guide/).
- **Hermes Agent** (Nous Research) is the other big one, with ~150 skills that OpenJarvis
  imports.
  - v0.16 (June 2026) added a desktop app, a "Jarvis" voice mode and screen vision with
    click/type.
  - Most "I built Jarvis" posts are creator-marketing funnels, so the claims are unverified.
  - Sources: [Stork listing](https://stork.ai/en/hermes),
    [v0.16 recap](https://openclawdatabase.com/news/videos/2026-06-07-hermes-v016-email-voice-jarvis-kanban-agents/),
    [dev.to Cipher](https://dev.to/trickell/cipher-the-jarvis-with-a-hermes-core-1c4o).
- **Common recipe:**
  1. a messaging channel you already use;
  2. markdown memory and persona;
  3. a proactive heartbeat;
  4. shell, browser and computer control;
  5. self-written skills;
  6. voice on top (Whisper + ElevenLabs);
  7. Claude or another frontier model as the brain.

  Sterling already has 1, 2, 3 (check-ins) and 7. Items 4 to 6 are mostly in flight.

## 4. Capability gap list for Sterling (prioritised)

Effort: S = about a day, M = 2 to 4 days, L = a week or more. "In flight" means a teammate is
building it now.

| # | Feature | Why it matters | Effort |
|---|---|---|---|
| 1 | Full Claude Code tool access + self-editing | The core "does anything" feeling. Copy the adewaskar gate: read-only default, verb-based write gate, hook-enforced, secret paths always refused. | **in flight** |
| 2 | Chrome control via Claude in Chrome extension | Signed-in browser beats Playwright | **in flight** |
| 3 | **Standing permissions ("always yes")** on approvals | Lets Sterling act proactively without calling Blake for every repeat action. Add an `as_permissions` table plus the `YES ALWAYS <id>` reply keyword. | S |
| 4 | **Heartbeat / proactive watch loop** | The thing OpenClaw users love most. Run a scheduled cheap sub-agent pass over a vault `HEARTBEAT.md` checklist. Make that file **owner-editable only**: no tool may write it (poisoning lesson). | M |
| 5 | **Calendar + email read** (Google OAuth) | Every Jarvis brief has them. Morning brief and "what's my day" are useless without them. Start read-only. | M |
| 6 | **Automatic fact extraction** after each turn | Memory that grows without asking. Have Haiku propose facts into the vault in the background, deduped through `vault.remember`. | S |
| 7 | Voice conversation (stretch goal; see below) | The literal Jarvis experience | M to L |
| 8 | Audio morning brief by phone | We already have Twilio call. Render the brief as TTS and have it played on the call. | S |
| 9 | Desktop modes (gaming/work setup) | Physical-world "Jarvis, set up for work" | **in flight** |
| 10 | Image upload in chat + watch-reel skill | Gives Sterling eyes on what Blake sees and makes | **in flight** |
| 11 | Claude chat-history import into vault | Instant long-term memory of Blake | **in flight** |
| 12 | Twilio diagnose-and-fix | Reliability of the main channel | **in flight** |
| 13 | Taint / outbound guard | Vault data must not leak into web_search, browser or other outbound tools. Becomes critical once 1 and 2 land. | M |
| 14 | Filler acknowledgements + HUD dashboard theme | Character polish: instant "Compiling, sir." and an arc-reactor status page | S |
| 15 | Skill overlays from lessons | Self-improvement without rewriting skill files | S |

Explicitly **don't**: install third-party skill hubs (ClawHub/Hermes) unreviewed, or let any tool
write the persona or heartbeat files.

### Voice on Windows: concrete options

| Layer | Option | Notes | Effort |
|---|---|---|---|
| Wake word | **openWakeWord** (Apache-2.0, Python, ships a pre-trained `hey_jarvis` model, CPU-only) | Best fit. Custom "hey Sterling" can be trained from synthetic clips in about 1 hour. | S |
| | Porcupine (Picovoice) | Very accurate, built-in "jarvis". Free tier needs an access key, and a custom word is a console export. | S |
| STT | **faster-whisper** (already a dependency, `small.en` int8) plus Silero/webrtc VAD | Local and free. About 1 s latency on a modern CPU, less with CUDA. | S |
| | Deepgram / ElevenLabs Scribe streaming | Lower latency and better accuracy. Paid, so it needs a key via request_credential. | S |
| TTS | **ElevenLabs** (British-butler voice) | Best "Jarvis" sound. About $5 to $22 a month. Stream audio for barge-in. | S |
| | Kokoro-82M (local, Apache) or Piper | Free and offline. Kokoro's `bm_george`/`bm_lewis` British voices are close to the feel. | S |
| | Windows SAPI via `pyttsx3` | Zero-install fallback. Robotic. | XS |
| Glue | A small `assistant/voice.py` loop: mic → wake → VAD record → STT → `run_turn(channel="voice")` → polished text → TTS, with barge-in on VAD | Uses the existing `voice` 600-char channel limit in persona. Started by the Windows launcher as a tray process. | M |
| Alt | Browser HUD à la adewaskar (Web Speech API + Porcupine-web) talking to `server.py` over WebSocket | Gets the visual Jarvis too. Needs a real Chrome window for mic access. | L |

Recommended path: openWakeWord + faster-whisper + ElevenLabs (Kokoro fallback) in a Python tray
loop, about 3 to 4 days including barge-in. Then reuse the same TTS for gap 8.

*Not reached:* oreilly.com (blocked by the session's network policy). No Reddit threads came up in
search.
