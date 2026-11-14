# The assistant (phase 2)

The Shorts pipeline is now one division of a personal assistant: a chief of staff that texts
the owner, remembers what matters, keeps his books and checks in on him. Everything is plain
files, markdown and SQLite, so it can be read, edited, versioned and replaced piece by piece.

## The picture

```
 owner (SMS / dashboard / call)
        |  inbound SMS: phase-1 webhook authenticates the owner's number, keeps YES/NO/PAUSE/STATUS,
        v  hands everything else to assistant.runtime.handle_inbound_sms (in the background)
 runtime.run_turn ── main agent (ASSISTANT_MODEL, agents/main.md + skills/persona)
        |                 └─ delegate ─> sub-agents (SUBAGENT_MODEL): studio, ledger, quartermaster,
        |                                archivist, researcher, engineer, briefer  (agents/*.md)
        |                                each with its skills (skills/<name>/SKILL.md)
        v
 assistant.tools (the ONLY capabilities; allowlist per agent file)
        |
        ├─ vault  (Obsidian markdown: memory, journal)      <data_dir>/vault, outside git
        ├─ store  (SQLite as_* tables: messages, check-ins, jobs, hours, income, meals, goals,
        |          agent runs, lessons, vault search index)  same DB as the pipeline
        └─ phase 1 (read-only studio view; notify for outbound texts and approvals; llm_usage)
 output -> persona.polish (hard rules in code) -> owner
```

Scheduled work (briefs, reviews, tidy-ups) calls `runtime.run_task(agent, task, deliver=...)`.

## Runtime decision: Messages API tool loop, not the Agent SDK

`claude-agent-sdk` 0.2.181 was installed and read. It is Claude Code as a library: every query
spawns the bundled CLI binary (`_bundled/claude`, ~360 MB) as a subprocess and talks to it over
stdio. It does support what we would want - `AgentDefinition(model=...)` takes an alias or a full
model id, `tools=[]` removes built-ins, in-process MCP tools via `create_sdk_mcp_server`/`@tool`,
`skills=[...]`, `setting_sources=[]`, `max_budget_usd`, `ResultMessage.total_cost_usd` and
per-model `model_usage[*].costUSD` - but for a small server answering texts it adds a heavyweight
process per turn, a filesystem/shell-centric harness we would spend effort disabling, budget
enforcement inside the CLI rather than in our code before each call, and skills discovered from
`.claude/skills` directories. So `assistant/runtime.py` runs a ~200-line loop on the Messages API:
system prompt from files, client tools from `assistant/tools.py`, delegation as a `delegate` tool
that runs the sub-agent's own loop. Agents and skills stay in the standard file formats, so a
future switch to the Agent SDK (or anything else) is one new `Backend` / adapter, nothing more.

Model calls: adaptive thinking (model default), `output_config.effort`, the system prompt cached
(breakpoint on the last stable block; time, history and memory go in the user message), and
server-side refusal fallbacks (`fallbacks: "default"`, beta) when `LLM_SERVER_FALLBACKS` is on.

## Sterling on the Claude plan: full powers (ASSISTANT_BACKEND=claude_code)

With the Claude Code backend the **main agent** (dashboard chat, texts, scheduled main tasks) runs as
real Claude Code on the laptop (`assistant/claude_code.py`, `ASSISTANT_CC_FULL=true` by default):

* **Tools**: Claude Code's own built-ins (Read, Edit, Write, Glob, Grep, Bash, PowerShell, WebFetch,
  WebSearch, Agent, Skill, tasks) plus every ClipAgent tool over MCP (`mcp__clipagent__*`).
  Run as `claude -p --output-format stream-json --verbose --append-system-prompt <persona> --setting-sources user
  --settings <generated> --permission-mode dontAsk --agents <generated> --add-dir <data> --add-dir <his folders>
  [--resume <session>] [--chrome]`, cwd = the app checkout. `--append-system-prompt` (not `--system-prompt`)
  keeps Claude Code's own tool instructions; the persona and a "your powers" note are appended.
  The repo's CLAUDE.md is not auto-loaded (only ClipAgent's own Claude Code profile is read, which keeps
  every chat turn small); the system prompt tells Sterling to read CLAUDE.md before editing itself.
* **Folders**: its own code (cwd), its data dir, and `ASSISTANT_USER_DIRS` (default Desktop, Documents,
  Downloads, Pictures, Videos, incl. OneDrive copies). Reads anywhere else are allowed by Claude Code,
  edits only inside these folders.
* **Sub-agents and skills, one source of truth**: `agents/*.md` (minus main) become `--agents` JSON every
  run (each with its own MCP tools + read-only built-ins); `skills/*/` are mirrored into
  `<data>/claude-code/skills/` (the persona skill is already the system prompt). Sub-agents launched
  through Claude Code's Agent tool share the run's MCP server, so tool calls are audited as `main`.
* **Conversation memory**: one resumed Claude Code session per channel (dashboard, sms), stored in
  kv `assistant:cc:session:<channel>`. A new session starts each local day, after
  `ASSISTANT_SESSION_MAX_TURNS` turns or once the context passes `ASSISTANT_SESSION_MAX_TOKENS`; the old
  one's last exchanges go to the vault journal (plain code, no extra model run). A session Claude Code
  no longer knows is replaced silently.
* **Images**: the dashboard chat takes uploads and pasted images (PNG/JPEG/WebP/GIF by content,
  8 MB, 4 per message) into `<data>/assistant/uploads/`; the turn gets their paths and opens them
  with the Read tool (Claude Code returns images as visual content). Thumbnails are served owner-only.
* **Long tasks**: chat turns run in the background (`POST /dashboard/chat` with `async=1`, then
  `GET /dashboard/chat/job/<id>`); a run may take `ASSISTANT_CC_TASK_TIMEOUT_SEC` (900) and
  `ASSISTANT_CC_MAX_TURNS` (40) tool rounds. The daily run cap and plan-limit backoff still apply.
* **Browser**: `open_url` opens any page in his default (signed-in) browser, so pages that need his
  login work; Sterling is told to do this itself and never to extract cookies or passwords.
  `ASSISTANT_CHROME=true` also passes `--chrome` (Claude in Chrome extension; one-time setup: install
  the extension in Chrome, sign it in to the same Claude account, restart Chrome). Whether `--chrome`
  connects in `-p` mode is not verified; `open_url` works either way.
* **Plan usage** (`assistant/usage.py`, skill `usage-aware`): Claude Code's stream-json output carries
  `rate_limit_event` lines (`status`, `utilization`, `resetsAt`, `rateLimitType` five_hour / seven_day...)
  when the limit state changes; they are recorded and shown by `usage_status` and on the dashboard.
  Non-urgent work (`usage.should_defer`) and his requests during a plan limit are queued in
  `as_deferred` and run oldest-first a few minutes after the reset (`assistant_deferred` job, every 10
  min); `schedule_prompt` queues any prompt for a time or for after the reset.

### Guardrails for full powers

| Risk | What enforces it |
|---|---|
| Reading/printing secrets | Permission deny rules (Read and Edit): `config.env`, `*.env`, `*token*.json`, `client_secret*.json`, `*.pem`, `*.key`, `.ssh/`, `.credentials.json`, and ClipAgent's Claude Code profile. They cover the file tools and the shell commands Claude Code recognises (cat, head, redirects), **not** a script that opens files itself (e.g. `python -c open(...)`); the persona forbids it. |
| Destructive commands | Deny rules: recursive `rm`/`Remove-Item`, `git push`, `git reset --hard`, `git clean`, format/shutdown, `iex`. Command-pattern rules are bypassable (aliases, scripts); they stop accidents, not a determined model. |
| Texting others, purchases, public posts, deleting his files | Instructed to ask first (`request_approval`); ClipAgent's own tools only ever text him. Not technically enforceable for the browser or shell. |
| Breaking itself | `assistant/selfedit.py`: each turn starts on the local branch `sterling/self` (created from the installed branch, which is remembered as `clipagent.upstream`); afterwards changed files -> offline test suite (`pytest -q -x`) -> pass: commit (trailer `Sterling-Self-Edit: yes`) and restart, then `/healthz` is checked; fail or unhealthy: `git reset --hard` to the pre-turn commit and he is told what failed. A tree that was already dirty is never reset. If the restarted app cannot even start, the Windows launcher resets to the commit in `data/assistant/selfedit_pending.txt`. |
| Unwanted change | Dashboard "Undo last change" or SMS `UNDO`: `git revert` of the newest self-edit commit (never anyone else's), then restart. |
| Runaway | SMS `STOP STERLING` / dashboard Stop: no assistant model runs until `START STERLING`. `PAUSE` still stops all AI spending. |
| Updates | On `sterling/self`, the launcher and Settings > Update merge `origin/<upstream>` with `git pull --no-rebase --no-edit`; a conflict is aborted and the local version kept. |

The weekly `engineer` proposal flow (assistant/improve.py) still exists and is the only self-change path
on the API backend; nightly evals remain the regression check.

## How memory works

* **Vault** (`assistant/vault.py`): the long-term memory, an Obsidian folder. Notes have YAML
  frontmatter (`type, created, updated, source, confidence: stated|inferred, tags`) and
  `[[wikilinks]]`; daily notes live at `Journal/YYYY/YYYY-MM-DD.md`. Created from `vault_template/`
  on first use. Facts are bullets under `## Facts` with provenance:
  `- Allergic to peanuts — _stated, sms, 2026-11-13_`.
* **One front door**: `vault.remember(fact, area=, source=, confidence=)` files a fact in the
  right note (`about, work, preferences, finance, meals, person:<Name>, project:<Name>`, ...)
  and skips near-duplicates. `forget()` really deletes.
* **Search**: SQLite FTS5 (`as_vault_fts`), synced incrementally (mtime, then sha) before each
  search, so edits made in Obsidian are found immediately; `reindex()` rebuilds.
* **Prompt context**: `context_for(message)` = core profile notes + best search hits + the last
  three journal days, capped in characters, passed to the model as untrusted data.
* **Human edits win**: writes carry the sha of what was read; a note changed on disk since is
  never overwritten - the new text is appended under a "Merged" heading.
* **Structured facts** (hours, income, meals, goals, check-ins) live in SQLite (`assistant/store.py`),
  because numbers need arithmetic and exactness; the vault holds context and preferences.

## Add a sub-agent

1. Create `agents/<name>.md`: frontmatter `name` (= file stem), `description` (when the main agent
   should delegate), `model` (`subagent`, `main` or a full `claude-*` id), `tools` (names from
   `assistant/tools.py`), `skills`; the body is its system prompt. Keep it short and specific.
2. `pytest tests/test_assistant_registry.py` validates it (unknown tools/skills fail loudly).
3. The main agent sees it in its roster and `delegate` enum automatically.

## Add a skill

Create `skills/<name>/SKILL.md` (frontmatter `name` = directory name, `description` = what and
when; body = instructions; optional resources beside it). List it under an agent's `skills` to
inline it into that agent's system prompt, or let agents with `skill_read` load it on demand.

## Change the persona

`skills/persona/SKILL.md` is the character (placeholders `{{assistant_name}}`, `{{owner_name}}`,
`{{honorific}}`); `ASSISTANT_NAME` / `OWNER_HONORIFIC` rename. Hard rules are code in
`assistant/persona.py` (`lint`/`polish`: SMS <= 320 chars, plain text, no emoji or exclamation
marks, honorific at most once, no "As an AI", no URLs read aloud) and are applied to every reply.
`skills/persona/examples.md` holds golden replies and counter-examples; tests (and later evals)
check every golden reply passes and every counter-example is caught and fixed.

## Safety model

* **Tool allowlist**: an agent gets exactly the tools named in its file that are registered in
  `assistant/tools.py`; anything else is refused at dispatch and logged. No shell, no file access
  outside the vault, no web unless `ASSISTANT_WEB_SEARCH=true` (server-side search, researcher only).
* **Vault path safety**: relative `.md` paths only; `..`, absolute paths, hidden folders and
  symlinks are refused.
* **Untrusted text**: vault notes, tool results, video titles and web content are wrapped in
  `<untrusted>` tags (forged closing tags are escaped). They can never add tools: the tool list is
  fixed in code per agent before the first call.
* **Budgets**: before every model call the worst-case cost is checked against the per-turn ceiling
  (`ASSISTANT_MAX_TURN_USD`), the assistant's own daily cap (`ASSISTANT_DAILY_BUDGET_USD`) and the
  owner's PAUSE switch; after it, usage is written to `llm_usage` (`purpose assistant.<agent>`,
  `tier assistant`) and each agent run to `as_agent_runs` (audit trail).
* **Owner-only messaging**: `message_owner` has no recipient parameter; it goes through phase-1
  `notify` (owner's number, quiet hours, DRY_RUN), capped at `ASSISTANT_MAX_SMS_PER_DAY`.
* **Approval**: anything irreversible goes through `request_approval` -> the phase-1 decision flow
  (call / SMS, `YES <id>`); only registered handlers can act on an approval.
* **Privacy**: the vault is outside git (`data/` and `vault/` are ignored; a test fails if the
  default vault path is not ignored). Only the empty `vault_template/` is committed.

## How it gets better

Fine-tuning is not available for these models, so nothing is learned "in the weights". The
honest equivalent is three things, all inspectable: **measured behaviour** (evals),
**remembered lessons**, and **reviewed edits** to the files that define the assistant. It
never changes itself unreviewed.

**Evals** (`assistant/evals.py`, cases in `evals/*.yaml` + the golden replies in
`skills/persona/examples.md`). A case is a situation (channel, message or scheduled task,
memory/notes/jobs/check-in/clip fixtures) plus plain-code checks: persona lint, length,
must/must-not contain, tool called/refused/not called, delegation, approval requested, nothing
sent to anyone, memory saved, untrusted text wrapped, forged delimiters escaped, safety rules
present in the prompt. Offline (`python -m assistant.evals run`, free, part of `pytest`) replays a
scripted "good" model through the real runtime, so it catches regressions in code and prompt
files. Live (`--live --max-usd 1`) uses the real model and also judges `rubric` checks with a
model; it estimates cost first and refuses above the ceiling or today's assistant budget.
Every case runs in a throwaway sandbox (temp DB and vault, DRY_RUN, generic owner); results
are kept in the DB (`kv evals:*`) for the dashboard (`evals.eval_results()`).

*Add a case*: append to `evals/core.yaml` (or a new `evals/<topic>.yaml`) - `id`, `category`,
`message` or `task`, optional `context` and `script`, `checks`, optional `rubric`. Invented,
generic content only: the repo is public. `pytest tests/test_assistant_evals.py` validates it.

**The loop** (`assistant/improve.py`, weekly and `python -m assistant.improve run`):
observe (plain code: corrections/praise by keyword, check-ins ignored vs answered, tool errors,
budget cut-offs, expensive runs, unused sub-agents, eval regressions) -> reflect (one `engineer`
run with the `self-improvement` skill: lessons via `lessons_propose`, at most 3 diffs via
`improvement_propose`; a deterministic stub in fake mode) -> **gate** -> owner approval.

**The gate** (plain code, `python -m assistant.improve rules`): one file per proposal, only
`skills/<name>/*.md`, `agents/<name>.md` or the vault routine note (`Agent/Routine.md`); never
code, config, budgets, `skills/persona/examples.md`, the self-improvement skill or
`agents/engineer.md`; an agent's name/model/tools (its allowlist) cannot change; no line of the
persona's "Never" rules, nor any line about approval, untrusted input, secrets, budgets,
privacy, medical matters or third parties, may be removed or reworded; at most 40 changed
lines; no personal data in added lines (phone numbers, emails, URLs, his name, text copied from
his messages); the diff must apply exactly; the registry must validate; and the offline evals,
run in a subprocess on a patched temp copy, must show no new failures. A proposal that fails is
stored as `rejected_by_gate` with its reasons and never offered.

**Approval and apply.** A passing proposal becomes a phase-1 decision (`assistant_action`,
handler `improvement`) with a one-sentence spoken summary. On YES it is re-gated against the
current tree, written, and re-checked (registry + offline evals); any failure rolls it back.
It is **never committed or pushed**: the edit stays in the working tree and a ready commit
message is written to `<data_dir>/assistant/improve/proposal-<id>.commit.txt`; a human (or the
deploy process) reviews `git diff` and commits. `python -m assistant.improve revert <id>`
restores the previous content. Lessons are adopted in one weekly batch approval and then go into
every agent's system prompt (`improve.lessons_context`, max 12 / 1,200 chars, most reinforced and
newest first; an older lesson in the same area that a new one overlaps is retired).

**Where the private record lives**: proposals and evidence in the DB (`kv improve:proposal:*`)
and the vault (`Agent/Proposals/`), lessons in `as_lessons` and `Agent/Lessons/Lessons.md`,
conversations in `as_messages`, agent runs in `as_agent_runs`. None of it is in the repo.

**If fine-tuning becomes available** for these models, the eval cases (what good looks like),
the adopted lessons, the approved/rejected proposals and the owner's conversations with their
corrections are exactly the dataset one would use - all already in the places above. No exporter
is built yet. Model ids are settings, so newer models drop in without code changes.

## The heartbeat (proactive checks)

`assistant/heartbeat.py`, skill `skills/heartbeat/`. OpenClaw's "heartbeat", minus its poisoning risk.

- **Checklist**: `Agent/Heartbeat.md` in the vault (default shipped from `vault_template/`, never
  overwritten). Bullets are conditions ("If a Short failed to upload, tell me") or timed items
  ("Each morning ...", "At 9pm ...", "Every hour ...", "Every Monday ..."). **Only Blake edits it**:
  `vault.write_note/append_to_note/delete_note` raise `VaultProtected` for `vault.PROTECTED_NOTES`
  (unless `owner=True`), Claude Code gets Edit/Write deny rules for it, and `selfedit.finish` restores
  it (and its template) after any full-power turn that changed it. Sterling proposes changes with
  `request_approval` handler `heartbeat_checklist` (`details.text` = the full new note); YES writes it.
- **Tick** (job `assistant_heartbeat`, every `HEARTBEAT_MINUTES`, default 30, only with
  `ASSISTANT_ENABLED`): skipped while stopped or while `usage.constrained()` (plan >= 80%). Plain code
  hashes the checklist + `heartbeat.facts()` (failed uploads, approval age buckets, plan, eval
  regressions, error events, pipeline pause, running tasks). Same hash and nothing timed due -> no model
  call. Otherwise one call on voice_front's fast model (assistant budget, `assistant.heartbeat` usage)
  with the checklist and `voice_front.status_snapshot()` wrapped as untrusted; it returns
  `{"notify": [{"text", "urgency"}], "tasks": [{"task", "why"}], "nothing"}`.
- **Notes** go through `tools.send_to_owner(kind="heartbeat")` (daily SMS cap), are not repeated
  within 12 h (exact or near-identical), and are spoken by the voice process and the Jarvis page
  (`task_done` voice message, origin `any`). Quiet hours: urgent notes are texted at once, the rest
  (and tasks) are held and re-asked on the first tick after quiet hours; timed items wait too.
- **Tasks** run like `voice_front.delegate` (origin `any`): one at a time, at most
  `HEARTBEAT_MAX_TASKS_PER_DAY` (default 6); `usage.should_defer("heartbeat", ...)` queues them when
  the plan is tight. The brief says it came from the heartbeat; approvals apply as always.
- **Visibility**: `heartbeat_status` tool, "Last heartbeat: 12 min ago — all quiet" in the voice
  status snapshot, one `heartbeat` event per tick, `python main.py assistant heartbeat [--now]`.

## Toolkit: reels, skill vetting, desktop modes, Claude history, Twilio doctor

Each is a plain module with `register_tools(register)` (tools reach Sterling in-process and over MCP)
and a CLI for Claude Code's shell: `python -m assistant.<module> --help`. Settings: "Assistant
toolkit" in `config.env.example` (`assistant/toolkit_settings.py`). Pages: Settings -> Desktop modes /
Claude history / Twilio check (`assistant/toolkit_web.py`, `templates/toolkit.html`).

* **Watch a reel** (`assistant/reels.py`, skill `watch-reel`, tool `reel_watch`): yt-dlp download into
  `<data_dir>/assistant/reels/<id>/` (optional browser cookies, falls back without), whisper transcript,
  8-12 ffmpeg keyframes (scene cuts + 1 fps fill, ~768 px JPEG), metadata, vault note
  `Inbox/Reels/<date>-<slug>.md`. The model looks at the frames (Read) and follows the skill: improve
  Sterling (default for AI/tool reels) or content feedback for the channel.
* **Skill vetting** (`assistant/skill_vetting.py`, tools `skill_vet`, `skill_install`, `skill_third_party`):
  fetch into quarantine pinned to a commit (never executed), static checks, GitHub reputation, then the
  model's SAFE/CAUTION/UNSAFE verdict. Decision table: trusted repo (anthropics/skills, the official
  plugin marketplace, `SKILLS_TRUSTED_REPOS`) + SAFE + no red flag -> installed via the self-edit flow
  (tests, commit, UNDO); CAUTION or unknown source -> owner approval (`YES <id>`); UNSAFE -> never.
  Installed skills are recorded in `skills/third_party.json` (source, commit, verdict, file hashes)
  and are reference text only. **Limits, plainly:** static scanners are easy to evade (published
  research shows one-line bypasses) and a model's reading can miss things; the verdict is advisory,
  the allowlist and the owner's YES are the gate. Under the full-power Claude Code backend the agent
  has a shell, so a skill it follows can do whatever the agent can - another reason unknown sources
  always need approval.
* **Desktop modes** (`assistant/desktop_modes.py`, skill `desktop-modes`): modes in the vault note
  `Agent/Desktop Modes.md` (YAML block), fixed step vocabulary only (launch, close, open_folder,
  open_url, power_plan, volume, mute, notifications, wait), argv lists, values passed to PowerShell
  through an environment variable (no interpolation). Windows only; elsewhere a run is a plan.
  Armoury Crate profiles have no supported CLI; Focus/DND likewise (the `notifications` step flips
  the "Get notifications from apps" switch instead).
* **Claude history** (`assistant/history_import.py`, skill `memory-import`): claude.ai export manifest ->
  links opened in his browser (Windows) -> detached watcher imports the zips from Downloads (handles
  `(1)` names, `.crdownload` partials) -> `as_import_*` tables + FTS (`history_search`) -> nightly
  distillation (15 per run, newest first, `HISTORY_IMPORT_RESERVE_TURNS` left for him) through the
  archivist, facts filed with `vault.remember` and provenance. Export links are never fetched or logged.
  Any number of zips, any category, any order, re-uploadable; unknown JSON shapes are logged.
* **Twilio doctor** (`assistant/twilio_doctor.py`, skill `twilio-doctor`): read-only diagnosis
  (config, account type, caller-ID verification on trial, number capabilities and SMS webhook,
  Messaging Service override, A2P 10DLC brand/campaign status, recent error codes mapped to cause +
  fix + docs link). Fixes only on his click or YES, all reversible: `set_sms_webhook`
  (`revert_sms_webhook`), `service_use_number_webhook`, `normalise_numbers`.
