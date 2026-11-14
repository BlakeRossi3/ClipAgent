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
