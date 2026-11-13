---
name: desktop-modes
description: Set the owner's Windows PC up for an activity - Gaming, Work, Wind down or his own modes - by running a mode's fixed steps (open/close apps, power plan, volume, notifications). Use when he says "gaming mode", "set up for work", "wind down" or asks to change or add a mode.
---

# Desktop modes

## Run a mode
- `desktop_modes_list` shows his modes; `desktop_mode_run` with the name he used ("gaming" finds
  Gaming). CLI: `python -m assistant.desktop_modes run Gaming [--dry-run]`.
- Only run a mode he asked for, once. Report in one sentence what happened and any step that failed
  with its fix (e.g. a missing power plan, an app not installed). Optional steps that fail are fine.
- Off Windows (or `DESKTOP_MODES_DRY_RUN=true`) a run only returns the plan; say so.

## Change or add a mode
Modes live in the vault note `Agent/Desktop Modes.md` (a ```yaml block) and in Dashboard ->
Settings -> Desktop modes, which validates before saving. Only these steps exist:
`launch` (Start-menu name, exe path, `shell:AppsFolder\<AppID>` or a URI like `steam://open/main`,
optional `args`), `close` (process name: Discord, ms-teams, olk/OUTLOOK, steam; `force: true` ends
it after 5 s), `open_folder`, `open_url` (http/https), `power_plan` (balanced, high_performance,
power_saver, ultimate or a GUID), `volume` 0-100, `mute` true/false, `notifications` on/off,
`wait` seconds. `optional: true` on any step. There is no "run a command" step on purpose; scripts
cannot be launched from a mode. Propose the YAML change, confirm with him, then edit the note.

## Known limits (tell him if relevant)
- Many laptops (Modern Standby, including recent ROG Strix models) only have the Balanced plan;
  `powercfg /list` shows what exists. ASUS Armoury Crate's Silent/Performance/Turbo profiles have
  no supported command line, so a mode cannot switch them; he can bind them to the Fn+F5 key, or
  add `launch: Armoury Crate` as a reminder step.
- `notifications: off` flips Windows' "Get notifications from apps" switch; Focus / Do Not Disturb
  itself has no supported command line.
- Closing an app asks its window to close (unsaved work prompts stay); `force: true` ends it.
