---
name: toolsmith
description: When the owner asks for something you cannot do yet, give yourself the capability (a new tool or skill) through the self-edit flow instead of saying "I can't".
---

# Toolsmith: build the capability, then do the task

Never answer "I can't" to something the owner asks. Work through these steps:

1. **Check what you already have.** Existing tools (`mcp__clipagent__*`), skills, the shell
   (PowerShell/Bash), file tools, web fetch/search, and his own signed-in browser (`open_url`, or
   Claude in Chrome when connected) cover most requests: opening a link, downloading his own data,
   reading or tidying files, running a script. If they cover it, just do it.
2. **Otherwise design the smallest new capability** in your own code (the app checkout, your cwd):
   - A tool: a Python module `assistant/<name>.py` with plain functions, a small CLI
     (`python -m assistant.<name> ...`) and `register_tools(register)` that registers
     `Tool(name, description, input_schema, fn)` (see `assistant/usage.py` for the pattern). Add the
     module to `EXTENSION_MODULES` and the tool names to `PLANNED_TOOLS` in `assistant/tools.py`, and the
     tool names to `agents/main.md` (and any specialist that needs them).
   - Or a skill: `skills/<name>/SKILL.md` with `name` and `description` front matter.
   - Follow CLAUDE.md (no import side effects, `db.utcnow()`, DRY_RUN, offline tests) and add
     `tests/test_assistant_<name>.py`. Run `python -m pytest -q -x` yourself before you finish.
3. **Let the self-edit flow save it.** Do not commit or restart yourself: after your turn the suite
   runs, a pass commits it on `sterling/self` and restarts the app, a failure rolls it back.
4. **Tell him in one line** what you added and that UNDO reverses it.
5. **Finish his original request** with the new capability (after the restart, in the next turn if
   needed; say so).

Existing modules you may improve or rewrite when they fall short, like any of your code:
`assistant/history_import.py` (importing his Claude/ChatGPT history), `assistant/reels.py` (reel
analysis), `assistant/desktop_modes.py` (desktop/laptop modes), `assistant/twilio_doctor.py` (texting
diagnostics), plus every other module, skill and agent definition.

Ask him first (request_approval or a plain question) before anything that spends money, contacts
anyone besides him, installs third-party code or packages (vet them: well-known, maintained, pinned
version, no post-install scripts you have not read), or deletes his files. Never put secrets in code.
