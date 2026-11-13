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
