# Voice speed and turn-taking: research and plan

Written 2026-11-14. Research only: no code was changed. This answers two of Blake's problems on Windows:
- "It's way too slow, even for basic tasks."
- "Pauses split what I say into several messages, and then it freezes trying to answer all of them."

The code read for this: `assistant/brain.py`, `voice/web.py`, `static/jarvis.js`, `assistant/selfedit.py`, and the
reference bridge `adewaskar/jarvis/bridge/server.mjs`. Latency figures marked "est." are estimates from the code
and published numbers. They are not measurements on his laptop.

## 1. What fast voice stacks do

| Project | Technique worth taking |
|---|---|
| **Pipecat** ([turn strategies](https://docs.pipecat.ai/server/utilities/user-turn-strategies), [Smart Turn v3](https://www.daily.co/blog/announcing-smart-turn-v3-with-cpu-inference-in-just-12ms/), [repo](https://github.com/pipecat-ai/smart-turn)) | The default end-of-turn check is **Smart Turn v3**: an audio model (~8 MB ONNX, **BSD-2**, weights and data open) that runs on the **CPU in 12-65 ms**. Silero VAD only waits `stop_secs=0.2` of silence, then the model decides whether he has finished. `MinWordsInterruptionStrategy(min_words=3)` keeps one word from cutting the bot off. `user_idle_timeout` re-prompts after silence. |
| **LiveKit Agents** ([tuning](https://docs.livekit.io/agents/logic/turns/tuning/), [turn-handling options](https://docs.livekit.io/reference/agents/turn-handling-options/), [plugin](https://pypi.org/project/livekit-plugins-turn-detector/)) | The turn detector is a **text** model: a Qwen2.5-0.5B fine-tune, int8 ONNX, about 400 MB of RAM, 25-160 ms on a CPU. It is on by default, together with `endpointing.min_delay` / `max_delay` (max 3.0 s). **Preemptive generation** starts the LLM on the final transcript before the turn is confirmed and throws it away if he keeps talking (at most 3 attempts per turn). False-interruption handling: if no words follow within `false_interruption_timeout` (2 s), the agent resumes speaking (`resume_false_interruption=True`). A [subagent pattern](https://docs.livekit.io/agents/logic/patterns/subagent-delegation/) puts a fast model in front with a slow worker behind it. **Licence: the [LiveKit Model License](https://scancode-licensedb.aboutcode.org/livekit-model-2024.html) allows the model *only inside LiveKit Agents*, so we cannot use it.** |
| **OpenAI Realtime** ([VAD guide](https://developers.openai.com/api/docs/guides/realtime-vad)) | `server_vad` cuts the turn on silence (`silence_duration_ms`, default 500). `semantic_vad` cuts it when the words sound finished, with `eagerness` set to low, medium, high or auto. "Low" lets the user pause mid-thought. |
| **Deepgram Flux** ([config](https://developers.deepgram.com/docs/flux/configuration), [eager EOT](https://developers.deepgram.com/docs/flux/voice-agent-eager-eot)) | `EagerEndOfTurn` fires at `eager_eot_threshold`: the agent starts the LLM speculatively, and `TurnResumed` cancels it. `EndOfTurn` fires at `eot_threshold` (default 0.7), and `eot_timeout_ms` is the cap. This is the canonical **speculate, then cancel on new words** design. |
| **AssemblyAI** ([turn detection](https://www.assemblyai.com/docs/streaming/universal-streaming/turn-detection)) | The turn ends when an end-of-turn confidence is reached *and* a minimum silence has passed (`min_turn_silence`, 160-400 ms). If confidence is never reached, a silence cap ends it anyway. |
| **Vapi** ([pipeline](https://docs.vapi.ai/customization/voice-pipeline-configuration)) / **Retell** ([turn-taking](https://www.retellai.com/blog/turn-taking-voice-ai-hidden-problem), [latency](https://www.retellai.com/learn/understanding-voice-ai-latency-and-its-impact-on-call-experience)) | Vapi gives separate wait times for a transcript that ends in punctuation and one that does not, plus "smart endpointing" (its own model, or LiveKit's). The LLM request goes out at once, and the `waitSeconds` pause (0.4) is applied after TTS. Retell reports 600-620 ms from end of speech to first audio and sets a 1.5 s budget. Its turn-taking treats "uh-huh"/"okay" as backchannels, not as turns. |
| **Moshi / Hume / Sesame** ([summary](https://www.coval.dev/blog/the-future-of-speech-to-speech-ai-inside-gradium-and-kyutai-s-approach-to-full-duplex-conversation)) | Moshi is full-duplex at about 200 ms on a GPU, but weak at tool calling. Hume EVI 3 is claimed at under 300 ms. Sesame CSM is half-duplex. **None of them can run Claude Code's tools**, so they are not an option for us. The lesson they teach is that the *first sound* must not wait for the reasoning. |
| **Latency hiding** ([Stream](https://getstream.io/blog/speculative-tool-calling-voice/), [DevX](https://www.devx.com/artificial-intelligence-ai/voice-agent-latency-fixes/), [Full-Duplex-Bench v3](https://arxiv.org/pdf/2604.04847)) | Use **templated** fillers, not model-generated ones: they are instant and predictable. Run the tool while the filler plays. The benchmark found that a model which opens every answer with filler gets the fastest first word but the *slowest* tool call, because it speaks before it acts, and it talks over users 48% of the time. |

These stacks share a typical latency budget, adding up to about 0.6-1.2 s from end of speech to first sound:

| Stage | Time |
|---|---|
| End-of-turn decision | 200-500 ms |
| STT finalisation | 100-300 ms |
| LLM time to first token | 300-700 ms |
| TTS first audio | 100-250 ms |

## 2. Our pipeline, hop by hop (browser STT, Edge on Windows, Pro plan, `sonnet` at low effort)

| # | Hop | Est. time |
|---|---|---|
| 1 | Silence timer in `bsr.onResult` | **900 ms** |
| 2 | `assemble`/`holdFor` | 0 ms if the text ends in "." (Edge punctuates). 250 ms if not. **1600 ms** if the last word dangles ("and", "for"...) |
| 3 | `POST /ask`: auth, `store.add_message`, `prompt_for` (status, then `vault.context_for`, which runs `sync_index` and walks the vault) | 50-300 ms |
| 4 | `selfedit.begin()`: 3-4 `git` calls run **synchronously on the brain's event loop** (this blocks message reading too) | 150-600 ms on Windows |
| 5 | `_ensure_client`: no-op when warm. A reconnect is a CLI subprocess spawn, his MCP servers, `--chrome` and a session resume | **5-15 s+** ([Windows SDK start-up issue](https://claudeissues.com/issue/16005-claude-agent-sdk-80-second-startup-time-on-windows)) |
| 6 | Claude Code builds the request: the `claude_code` preset, about 20 built-in tools, every `clipagent` tool, his MCP servers and Chrome tools, plus the whole day's conversation. That is likely **40-100k tokens**. With the cache warm, time to first token is 1.5-3 s. With the cache cold (more than 5 min since the last turn), 3-8 s | 1.5-8 s |
| 7 | Adaptive thinking at low effort | 0-2 s |
| 8 | First sentence streamed | ~0.3 s |
| 9 | Browser TTS start ("Microsoft Ryan Online" is a cloud voice) | 200-600 ms. Kokoro on CPU: 0.3-1.5 s per sentence |

That gives **about 3-6 s for plain chat**. Every tool round adds one more model call (1.5-3 s) plus the tool's own run time.
"Search the web for the Ohio State score" is WebSearch, which is a nested server-side search request, probably at about 5-15 s per call
(not measured), usually 2-3 calls, then the answer: **20-40 s**. The results looked old because nothing in the prompt makes the model
put today's date in the query (see the [stale-age issue](https://claudeissues.com/issue/75934-bug-web-search-tool-returns-results-with-incorrect-stale-age-metadata-on-windows)).
Another risk is Claude Code auto-compacting the day-long voice session in the middle of a turn, which adds tens of seconds.

## 3. Why several quick utterances still freeze

Each item below is a path in the current code.

1. **The stitch rule has a hole: N utterances become 2 serial turns.** `stitch_target` refuses to merge once the previous
   turn has spoken a sentence and is 4 s old (`MERGE_WINDOW_SEC`). The page sends `/interrupt` only while the speaker is
   busy (`heardText`). So suppose turn A has said one line and is now in a long tool call (a web search). Nothing is playing, and
   he speaks again. A is neither interrupted nor merged, and the new turn B is **queued behind A**, waiting for A to finish
   (up to the 900 s ceiling). Later breaths merge into B, so it stays one queued turn, but B answers only after A's whole run.
   Then A's late sentences and B's answer both play. This matches what he described: "frozen trying to reply to all of them".
2. **Every merge into an in-flight turn costs an interrupt, and often a full reconnect.** `abandon` interrupts the CLI and
   then waits up to `ABANDON_WAIT_SEC` = 4 s for the abandoned turn's result. Two things make that wait fail:
   - An interrupt sent while a tool (WebSearch, an MCP call) is running often does not settle in 4 s.
   - An interrupt that reaches the CLI before it has started the query is ignored.

   In both cases `_recover` disconnects and **respawns Claude Code**, which takes 5-15 s on Windows with his MCP servers.
   So with three breaths, the second and third each pay 1-4 s, or a reconnect. The page shows "thinking" throughout.
   - Watchdog loop: after a resume, the first turn can be slow. If it passes `FIRST_OUTPUT_SEC` (25 s), the watchdog reconnects again, and he hears `LOST_TRAIN`.
3. **Leftover output from an abandoned turn hijacks the next one.** If the CLI emits anything after the abandoned turn's result,
   or after `current` was cleared, `_on_message` turns it into an *unasked announce turn* with origin `any`. The page
   **speaks** it. That turn also becomes `self.current`, so the next real question waits another 4 s in `_ask`
   (`prev.finished`, which never fires).
4. **The self-edit guard blocks the queue, possibly for minutes.** `_run` awaits `_finish_selfedit` before the queue moves to
   the next turn. `_finish` runs **the full test suite whenever the working tree has *any* uncommitted file**, whether or not this
   turn changed it. That is because `changed_files()` is compared with nothing, and the timeout is 1200 s. A dirty checkout on his laptop
   whose tests fail gives `kept_dirty` after **every** voice turn, and every queued utterance waits for pytest.
   *Check `data/logs` and the dashboard history for "kept_dirty" or self-edit messages.*
5. **Two listeners can produce two turns.** If `VOICE_ENABLED=true` (the laptop loop, origin `voice`) and the Jarvis page
   (origin `browser`) both hear him, each utterance produces two turns, and stitching works per origin only. Nothing pauses one
   listener while the other is active.
6. Smaller cases:
   - An announce turn queued at a task finish delays the next answer.
   - Chained merges re-ask from scratch, so all progress is lost.
   - `holdFor` returns 0 for Edge's punctuated text, so every 900 ms pause fires a turn and makes merges common.

## 4. Prioritised plan

| # | Change | Gain | Effort | Risk |
|---|---|---|---|---|
| 1 | **Strict single flight** (d): while any chat turn from this listener is not `done`, new speech always *merges*. Abandon the old turn, join the text, and if it had already spoken, prepend "(You had begun: "...". He added:)". Drop the 4 s and "has spoken" rules. The page sends `merge=1` whenever `pending` is set, not only while audio plays. Typed text is the exception: it may still queue. | Removes freeze path 1. This is Blake's own idea | S | Low. A deliberate "and also..." is now one combined request, which is what he asked for |
| 2 | **Interrupt without reconnecting.** Route messages by counting results: the SDK emits exactly one `result` per query, so mark every message before the abandoned turn's result as stale and drop it. Send the merged question at once, without waiting. Reconnect only if no result arrives within 20 s. Never create an "unasked" turn within 10 s of an abandon. Optionally move to streaming-input mode as the reference bridge does. | Removes 1-15 s per merge, plus the stray speech | M | Medium. Test it with the FakeClient, a delayed result and a missing result |
| 3 | **Take self-edit off the voice path.** Run `begin`/`finish` only if the turn used Edit, Write or Bash. Test only the files that changed *during this turn* (diff against the snapshot list). Run it in the background and never block the queue. Move `begin` off the event loop (`to_thread`). | Removes 0.2-0.6 s per turn, and minutes when the tree is dirty | S | Low |
| 4 | **Instant local acknowledgement** (a): within 300 ms of end of speech the page plays a short templated line ("One moment, sir.", "Checking.") if the utterance looks like a task (search, open, find, check, look up, what's the score...), or as soon as no sentence has come within 1.5 s. Add a second line at 8 s ("Still on it, sir."), then silence plus the rail. Pre-synthesise the lines when the page loads. Never let the model narrate. | The experience feels 2-5 s faster | S | Low. Cap it at one ack per turn, and skip it for chit-chat |
| 5 | **Fast search tool** (f): add an MCP tool `fast_search` (Brave `freshness=pd`, or Exa Instant, ~0.4-0.7 s, [benchmarks](https://parallel.ai/articles/best-fast-search-apis)). It is date-stamped and returns 5 snippets. The prompt says: "for live facts (scores, news, prices) use fast_search with today's date, put the source on screen". Keep WebSearch for research inside `start_task`. This needs an API key (`request_credential`). | Score-type questions drop from 20-40 s to 3-5 s, and results are fresh | S | Low. About $3-5 per 1k queries |
| 6 | **A smaller prompt for the voice session**: the `--chrome` tools only when he asks for the browser, his user MCP servers by allowlist, and the fewest `clipagent` tools in voice (the rest go to `start_task`). Roll the session over at about 60k tokens rather than once a day. While the page is open, a keep-warm ping every 4.5 min keeps the cache hot (it costs plan usage, so offer it as a setting). | Time to first token 1.5-4 s faster when cold | S-M | Low |
| 7 | **Fast voice model in front** (e): Haiku through the API key with a lean prompt, keeping the warm Sonnet session for anything that needs a tool. The earlier two-layer attempt failed because the front had **no context and no tools**, so it guessed. See the rules below this table. | Simple replies in about 0.8-1.5 s | M | Medium. Two voices in one memory. Ship after 1-4 and measure first |
| 8 | **Semantic end of turn** (b): **LiveKit's model is not allowed outside LiveKit Agents (licence), so skip it.** Use **Pipecat Smart Turn v3** (BSD-2, about 8 MB, 12-65 ms on CPU, audio input). On the server/VAD path, run it in `voice/` with onnxruntime. In the browser, run it with onnxruntime-web on the mic stream. Then cut the silence timer to 300-400 ms when the model says "complete", and allow 1.5-2 s when it says "incomplete". | 0.5-1 s off every turn, and fewer split utterances | M | Medium. Tune it on his speech, and keep `holdFor` as a fallback |
| 9 | **Speculative start on interim text** (c): send the query at about 400 ms of silence. If more words come before the first sentence plays, cancel and merge (needs 1 and 2). Never speculate when tools have side effects: speak only after the turn is confirmed. | Hides 0.5 s | M | Medium. Wasted plan usage. Do it only after 2 makes cancel cheap |
| 10 | **Measure every hop**: server timestamps for ask received, query sent, first stream event, first text and first sentence, sent with the existing `timing` event and shown in the **D** panel. | Shows us where the time goes | S | None |

**Rules for item 7, so it does not fail like the last attempt:**
- **Routing.** A tiny classifier (Haiku itself, or rules) sends anything about acting, fresh data, files, his screen or tasks
  *straight to Sonnet*. Haiku answers only recall, chit-chat, time, and confirmations.
- **Context.** Haiku gets the *same* context as the brain: today's voice transcript from `store.list_messages`, the vault
  context, and the live state (running tasks, the turn in flight).
- **One memory.** Whatever Haiku says is written into the Sonnet session's next prompt ("You (fast reply) already said: ..."),
  so the conversation has one history.
- **Escalation.** When Haiku is unsure, it says the item-4 ack and hands off; it never says "I can't". Claude Code
  `--model haiku` is *not* a substitute, because the preset prompt and tool load would erase most of Haiku's speed.

**Order:** 1 + 3 (the freeze) → 2 → 4 + 5 + 10 → 6 → 8 → 7 → 9. Items 1-5 should take a basic task from "frozen" or 5-40 s to
an immediate ack and an answer in 3-6 s. Item 7 brings simple chat under about 1.5 s.

*Not reached:* docs.livekit.io and huggingface.co were blocked in this session, so the LiveKit and model-card facts above
come from search snippets and the [scancode licence record](https://scancode-licensedb.aboutcode.org/livekit-model-2024.html).

## 5. Implemented: items 6-8 (2026-11-15)

Items 1-5 and 10 were done first. This section covers 6 (lean session), 8 (Smart Turn) and 7 (fast lane).
Item 9 (speculative start) was **not built**: the end of turn already comes after about 300 ms of silence when Smart
Turn is confident, so starting early would save at most about 0.3 s, and every cancelled start costs plan usage.

### Item 6: a lean, rolling voice session (`assistant/brain.py`, `ASSISTANT_VOICE_LEAN=true`)
- **Built-in tools**: the voice session passes `tools=VOICE_BUILTINS` (Read, Glob, Grep, Edit, Write, Bash,
  PowerShell, WebSearch, WebFetch, Agent, Skill, TaskStop). Background tasks (`start_task`) keep every built-in.
- **Chrome and his MCP servers on demand**: the Agent SDK can switch a configured MCP server on and off in a live
  session (`ClaudeSDKClient.toggle_mcp_server`). Tests here confirmed that a server switched off disappears from the
  next request, comes back when switched on, and that nothing is written to the profile. His servers are configured,
  switched off after connecting, and switched on in place by the new tool `need_tools(kind="my_servers")`, without a
  reconnect. Claude in Chrome comes from the `--chrome` flag, so it cannot be toggled. `need_tools(kind="browser")`
  therefore ends the turn, reconnects with `--chrome` while resuming the same session, and continues the request
  automatically (timing field `escalated_ms`). A new session starts lean again.
- **Rollover at about 60k** (`ASSISTANT_VOICE_SESSION_MAX_TOKENS`): the session size is now the context of the last
  model call, read from the stream's `message_start`. A result's usage adds up every call in a multi-tool turn,
  which overstated the size. A rollover writes the summary to the journal as before. It also leaves the summary in kv
  (`claude_code.carry_over`), and the next session's first prompt opens with it.
- **Keep-warm: not built.** Any ping into the Agent SDK session is a real turn. It costs plan usage and also writes a
  turn into the conversation, so it fails the rule "free on the plan or negligible on the API". The fast lane covers
  the cold-cache chit-chat case instead.

Measured prompt sizes for one "hello" turn. Claude Code 2.1.323 (bundled with claude-agent-sdk 0.2.181) was pointed
at a local capture server. These numbers exclude Chrome and his own servers, which this sandbox does not have:

| | Before | After (lean) |
|---|---|---|
| Whole request (system + tools + messages) | ~44.6k tokens | ~27.7k tokens |
| Tool definitions | 113 tools, ~33.0k | 91 tools, ~16.8k |
| ...of which ClipAgent MCP tools | ~9.2k | ~9.3k (plus `need_tools`) |
| Claude Code's own `/context` (`get_context_usage`), MCP excluded | 20.3k | 15.7k |
| Chrome tools + his MCP servers | all, every turn | 0 until `need_tools` |

The system prompt (~3.8k, of which ours is ~2.5k) and skills (~6.4k in `/context`) are unchanged. The skills listing
is the next thing to trim. After it connects, the brain logs `voice brain context` (Claude Code's `/context`
breakdown) and shows it in the D panel, so the figure from his laptop, with his servers, can be read there.

### Item 8: Smart Turn v3 (`voice/smart_turn.py`, `VOICE_SMART_TURN=auto|off`)
- Source: `pipecat-ai/smart-turn-v3`, file `smart-turn-v3.2-cpu.onnx` (int8, about 8 MB). BSD 2-Clause, Copyright
  Daily. The input format was verified from the repo's `inference.py` and `audio_utils.py` (raw.githubusercontent.com):
  16 kHz mono, the last 8 s, zero padding at the front, Whisper log-mel features of shape (1, 80, 800) as
  `input_features`, and a sigmoid output where > 0.5 means complete. huggingface.co was blocked here. The file name
  comes from search results, and the model downloads on first run into `data/models/smart_turn/`, with progress.
- The features are computed in numpy and match faster-whisper's Whisper extractor to within 2e-7 (tested). The
  runtime is onnxruntime, which faster-whisper already installs.
- **Laptop loop**: after 300 ms of quiet, the `Recorder` asks once per pause about the whole utterance. "Finished"
  ends the turn after about 300 ms. "Not finished" waits up to 2 s. New speech resets the check.
- **Jarvis page, server hearing**: the page cuts at 350 ms of silence (`smart_turn_ready`). `/talk` runs Smart Turn on
  the whole turn, in parallel with Whisper (`cont=1` joins the clip that is still held). The audio verdict overrides
  `holdFor`. The browser-hearing path has no audio on the server, so it keeps the timer. onnxruntime-web would need a
  CDN or a vendored WASM build of about 10 MB, so it was skipped.
- No model, or the model fails: `None`, and the timer decides as before.

### Item 7: the fast lane (`assistant/fastlane.py`, `VOICE_FAST_LANE=auto|off`)
The rules from section 4 are implemented as written. Routing uses local rules, and the quick model's own
`<handoff/>` flag can hand off in the middle of a reply, after one acknowledgement. The quick model gets the brain's
own context blocks (`brain.context_parts`) plus today's transcript. Every fast exchange goes into the brain's next
prompt (`<fast_replies>`). The lane is off without an API key. Timing fields: `route`, `fast_first_token_ms`,
`fast_ms`, `context_tokens`, `smart_turn_p`, `smart_turn_ms`.

**Expected latency now** (est., not measured on his laptop): chit-chat on the fast lane is about 1-1.5 s from the end
of speech to the first word. Plain chat on the brain is about 2.5-4.5 s, down from 3-6 s: a smaller prompt, and on
the server path an end of turn 0.3-0.9 s earlier. Tool turns gain the same 1-2 s.

## 6. Live measurements, sandbox (2026-11-15)

**Where this was measured.** A Linux cloud box running the real Claude Code CLI (2.1.325, Agent SDK 0.2.181) on the
plan login, **not** Blake's Windows laptop. There was no browser, no mic, no browser STT/TTS and no Kokoro. There was
**no fast lane**, because no API key was set (`VOICE_FAST_LANE` off), and no fast search key. Setup: the real
`uvicorn server:app` with a `seed_demo.py` data dir, `DRY_RUN=true`, `ASSISTANT_BACKEND=claude_code`, sonnet at low
effort (checked: the request carries `output_config.effort=low`, adaptive thinking), lean voice session, self-edit
off. Each typed utterance was POSTed to `/voice/api/ask` the way the page does, and the answer was read from
`/voice/api/stream`. Times are the brain's own `timing` fields, in ms from the moment the utterance arrived. The
client-side SSE time was 20-60 ms later, because the stream polls every 50 ms. One run per row, so n is small: read
the numbers as indications, not statistics.

**Cold start:** the server is up in about 0.5 s. The brain connects (CLI spawn plus MCP) **1.2-1.5 s** after that,
at app start, before the page opens (`server.py` already pre-warms). The first turn after connecting took 1.2-1.6 s
to the first sentence.

| Category (n) | Before: first sentence med / p90 | Before: total med | After: first sentence med / p90 | After: total med |
|---|---|---|---|---|
| Plain chat (3) | 1337 / 1583 | 1648 | **985 / 1149** | 1334 |
| Status: "what's going on today", "how much did I make" (2) | 3270 / 3332 | 3298 | **1169 / 1176** | 1348 |
| Quick action: voice to Lewis, voice back, open url (3) | 2099 / 4352 | 2120 | **1897 / 3733** | 1928 |
| Memory question (1) | 2219 | 2248 | 2087 | 2132 |
| Web: "latest NASA news" (1) | 11368 | 11404 | 8974 and 11102 (two runs) | — |
| Rapid dictation, answer after the 2nd utterance (1) | 3227 | 3255 | 2268 | 2298 |
| Interrupt mid-answer (1) | done 49 ms after the interrupt, 0 stale sentences | | done 45-50 ms after the interrupt, 0 stale sentences | |
| Context on the first turn (tokens) | 42.4k | | **36.2k** | |

Rapid dictation ("On Monday I worked nine hours." and then, 1.5 s later, "Tuesday eight."): in every run the first
turn was cancelled with nothing spoken, one merged answer came back, and nothing froze. The turn after an interrupt
answered normally in about 1 s.

**Where the time goes in a turn with a tool** (from `first_tool_ms`, `tool_ms` and `first_token_ms`):
- The first model call takes about 0.75-0.9 s until it starts the tool call.
- Streaming the tool's input takes about 0.3 s. That cost is inside the `tool_ms` figure: our tools themselves run in
  20-60 ms (measured directly: `studio_snapshot` 9-14 ms, `work_lines` 20-40 ms, an MCP round trip of about 50 ms).
- The second model call then takes about 1-1.3 s.
- In this environment the CLI delivers the text of a reply after a tool **in one burst**, so the first sentence
  arrives with the whole answer. This was confirmed with plain `claude -p --include-partial-messages`, and plain chat
  without a tool still streams.

Each tool round therefore costs about 1.3-2 s, and the model's own calls dominate. Plain chat is one model call:
time to first token is 0.7-0.8 s on a warm cache, and the stream comes in bursts of about 350 ms.

**Fixes and their measured effect**
1. **Status and money questions needed a tool round.** The model called `studio_status` / `ledger_summary` first:
   3.2-3.4 s. Now `brain.context_parts` adds a short "At a glance" block (Shorts today, failed sources, approvals,
   this week's hours and pay) **only** when the utterance looks like a status or money question
   (`status.wants_glance`, about 40 ms). The answer needs no tool: **3270 → 1169 ms** to the first sentence.
   Source: this is the "context, not a tool call" pattern from section 1 (Stream's speculative tool calling,
   Full-Duplex-Bench: tool rounds dominate voice latency). The block sits in the per-turn user message, so it does
   not touch the cached prefix ("tools → system → messages; changes at each level invalidate that level and all
   subsequent levels", [prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)).
2. **A keyless `fast_search` wasted a model round.** Without a key it only returns a dated query, then WebSearch runs:
   one extra round of about 1.5-2 s. Without a key the tool is no longer offered, and the prompt says "one WebSearch
   call with the month and year". Web: **11.4 → 9.0 s**, the same answer. The second run was 11.1 s because
   WebSearch itself took 8.5 s instead of 6. Source: every tool round is another model call; see the Anthropic
   [streaming](https://platform.claude.com/docs/en/build-with-claude/streaming) and tool-use flow. Section 1
   (Stream, DevX) says the same: do fewer rounds and keep slow searches off the voice path.
3. **About 4k tokens of skills on every turn.** Claude Code's bundled skills and the claude.ai-synced skills were in
   the listing. The lean voice session now passes `skills=claude_code.native_skill_names()` (our own skills only;
   background tasks keep all). `/context` Skills went from 6389 to 1162 tokens, and the first-turn context from
   42.4k to 36.2k. Plain-chat first sentence: **1337 → 985 ms** median (noisy). Source: the Agent SDK skills docs
   say "the model doesn't see unlisted skills" when a `skills` list is passed
   ([Extend agents with skills](https://code.claude.com/docs/en/agent-sdk/skills)). Each listed skill costs context
   on every turn: about 75-150 tokens each, by community estimates ([skill listing budget](https://pilot-shell.com/blog/skill-listing-budget)).
4. **"Switch your voice back" took 3 lookups (4.9 s).** `settings_set` did not say what the old value was. It now
   returns `was` (a value it accepts back), and the tool description says to use it. In between: 4.9 → 3.4 s. After
   that, the first call tried the human description, which was not a settable value. That was fixed: `was` is now
   e.g. "browser" or "Lewis". The final re-measure of this one fix was cut off by the plan's 5-hour limit. Its first
   call ran with `was: "browser"`, but its timing is unmeasured.
5. **The last sentence waited for the message to close.** `SentenceStream` only ended a sentence when more text
   followed, about 50 ms later for the final sentence. It now ends a sentence at once when the buffer ends on a word
   plus `.`, `?` or `!`, still guarding abbreviations, decimals and initials. That saves about 50 ms on one-sentence
   replies.

**What remains, and is not ours to fix here**
- **WebSearch** is a server-side search of 6-8.5 s per call. A `BRAVE_SEARCH_API_KEY` or `EXA_API_KEY` turns
  `fast_search` on, at about 0.4-0.7 s, which should bring news and score questions to about 3 s.
- **A model round** costs about 0.8-1.3 s per call on the plan. Tool turns stay at about 2 s at best, and the page's
  instant acknowledgement covers that gap.
- **An API key** would turn the fast lane on, so chit-chat goes to Haiku at an estimated 0.5-0.8 s to the first
  word (not measured here).
- **The post-tool burst** (the whole answer arrives at once) may be specific to this environment. Check it on the
  laptop in the D panel: `first_token_ms` close to `total_ms` on tool turns.
- **On Windows** expect a slower cold connect (5-15 s; section 2) and the browser/Kokoro TTS start on top of these
  figures.

**Other findings from the web research**
- **Keeping the session warm is the right model.** The SDK has no pool or warm-up call in Python. The documented
  pattern is one long-lived `ClaudeSDKClient`, as the brain uses
  ([hosting docs](https://code.claude.com/docs/ja/agent-sdk/hosting.md)). Windows reports CLI start-up of about
  11 s, and up to 80 s
  ([issue 16005](https://claudeissues.com/issue/16005-claude-agent-sdk-80-second-startup-time-on-windows)). Here
  the connect took 1.2-1.5 s, so the app-start pre-warm matters more on his laptop than here.
- **Prompt cache: 5-minute TTL, refreshed on every hit**
  ([prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)). After more than 5 minutes
  of silence, the first turn pays a full prompt read. That is another reason to keep the fixed prompt small (fix 3).
  A keep-warm ping remains out, for the reasons in section 5.
- **Built-in tool definitions are sent whether or not they are allowed.** The `tools` list (lean session, section
  5) is the way to drop them; `allowed_tools` is not (claudeissues issue 21773, seen in search results only; the
  site is blocked here).
- **The post-tool text burst** (not fixed). No matching Claude Code issue was found. The nearest report says
  interactive text "only appears all at once when a response completes" while `-p --include-partial-messages`
  streams ([issue 80364](https://claudeissues.com/issue/80364-bug-interactive-text-streaming-never-paints-incrementally-on-windows-reproduces)).
  Measure it on the laptop before acting on it.
- **Fast front model.** Other Claude voice assistants put Haiku in front for quick replies and keep a bigger model
  for deep work ([ethanplusai/jarvis](https://github.com/ethanplusai/jarvis)), which is the fast lane of section 5.
  It needs the API key.
- **Browser TTS on Windows.** Chrome and Edge load voices asynchronously, so `getVoices()` is empty until
  `voiceschanged` fires ([Readium notes](https://readium.org/speech/docs/WebSpeech.html),
  [monotonous.org](https://blog.monotonous.org/2021/speechsynthesis-getvoices/)). Some Windows 11 Chrome Google
  voices stop after the first sentence ([TTSReader](https://ttsreader.com/blog/new-bug-in-google-chrome-voices/)),
  so prefer Edge's "Online (Natural)" voices. No source confirms that a silent warm-up utterance helps, so the page's
  self-test (a real spoken line) stays the check.

