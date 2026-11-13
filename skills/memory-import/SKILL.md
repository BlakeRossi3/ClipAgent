---
name: memory-import
description: Import the owner's claude.ai chat history (data export) into searchable storage and distil it into the vault - from a pasted export manifest or uploaded zips - and answer "what did I say about X before" with history_search.
---

# Claude history import

## Getting the export (he does this part)
claude.ai -> Settings -> Privacy -> Export data. Claude e-mails him a manifest (JSON with
`data_files`: light_metadata, conversations parts, projects, memories...). Each link is private,
needs his claude.ai login and **expires after 24 hours**.

## Hands-free import
- He pastes the manifest to you (or in Settings -> Claude history). Call
  `history_import_from_manifest` with the full text. On his Windows PC it opens each link in his
  signed-in browser and a background watcher imports each zip from Downloads as it finishes, then
  starts distilling. Elsewhere it lists the files; he opens the links and uploads the zips in Settings.
- Never fetch the links yourself, never repeat them in messages or logs.
- If the paste was cut off, say how many files are missing versus `total_files` and ask for the rest.
- If a file never arrives: the link probably expired - he requests a new export.
- `history_import_status` shows files, conversations, distilled %, facts added and the watcher state.

## Distillation
Runs nightly (`HISTORY_IMPORT_BATCH`, default 15 conversations, newest first) until done, leaving
`HISTORY_IMPORT_RESERVE_TURNS` Claude-plan runs for his own chats. Each conversation becomes facts
via `vault.remember` with provenance `claude chat "<name>" <date>`, stated vs inferred, deduplicated.
When you are the archivist doing a distillation task: return only the JSON asked for, lasting facts
about him only, never secrets.

## Answering from history
`history_search` searches the raw messages. Cite the conversation name and date; say when
something is only from an old chat and may be out of date. The memory vault wins over old chats.
