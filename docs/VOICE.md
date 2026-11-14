# Voice: talking to Sterling out loud

`python -m voice` is an always-on process on the laptop. `main.py schedule` (the process the
ClipAgent shortcut / `windows/ClipAgent.ps1` launcher starts) starts it as a child process when
`VOICE_ENABLED=true`, and stops it on shutdown or restart. A lock (`data/voice.lock`) keeps it to a
single copy. Logs go to `data/logs/voice.log`; startup crashes go to `data/logs/voice-console.log`.
Turn it on under **Settings -> Power & privacy -> Voice**, which also has the microphone choice,
wake word, voice, barge-in mode and a "Test my mic" meter.

## What it hears, and what is sent where
- The microphone stays open, but nothing leaves the laptop until a wake word triggers. "Hey Jarvis"
  is detected by openWakeWord on the CPU. The assistant's name ("Sterling") is spotted by a small
  local Whisper model (`tiny.en`) run on each short burst of speech. Neither stores the audio.
- After the wake word (or within 8 s of a reply: follow-up mode), what you say is turned into text
  by faster-whisper on this laptop (the GPU when it works, otherwise the CPU). Only that **text**
  goes to Claude. Audio is never sent.
- Replies are spoken by Kokoro (a free local voice, `bm_george`). Windows' own voices are the
  fallback. ElevenLabs is used only if you choose it and add a key; then the reply **text** goes to ElevenLabs.
- Every exchange is saved in the chat history (channel "voice"). Answers are spoken, never "see the dashboard".

## Two layers: a fast voice, a slow worker (`assistant/voice_front.py`)
Modelled on adewaskar/jarvis (MIT), which keeps a quick conversational loop apart from the Claude Code worker.
- **Fast front.** Each utterance is one streaming Anthropic API call on `VOICE_FRONT_MODEL` (default
  `claude-haiku-5-5`; `claude-sonnet-5-5` also works). It runs at low effort with at most 300 output tokens,
  using the saved `ANTHROPIC_API_KEY`, which must have credit. Its costs count against
  `ASSISTANT_DAILY_BUDGET_USD`. The context is kept small: the persona, a plain-code status snapshot (background
  tasks, approvals, today's check-ins, money this week, Shorts, plan usage, recent events) and the last ten
  voice exchanges. It may call only quick tools: memory_search, ledger_summary, usage_status, open_url,
  desktop_mode_run, status_snapshot and `delegate_task`. Sentence one goes to the voice while the rest is still
  being generated. "What's going on" is answered straight from the snapshot. A filler line ("One moment, sir.")
  plays only if nothing has been said after `VOICE_FILLER_DELAY_SEC`.
- **Slow worker.** Real work (code, files, research, the browser, self-edits) is handled in three steps:
  1. The front says "Right away, sir." and calls `delegate_task`.
  2. The full agent then runs in the background (`run_turn(channel="voice-task")`). Each task gets a fresh
     Claude Code session and a short brief, plus the status context.
  3. When it finishes, the front model turns the result into one to three sentences and speaks them. The
     browser picks this up from `/voice/api/status`; the laptop voice process announces it when idle. The full
     text goes to History.

  While a task runs, the Jarvis status line reads "Working on: ...". If you ask "how's that going?", the
  answer comes from the snapshot.
- **No API key saved** (and not in fake mode): the front falls back to Claude Code with model `haiku`, no
  tools, no resume and one turn. That costs a process start (a few seconds), but it is still far faster than
  the full agent. Delegation still works.

## Local commands (no Claude involved)
| Say | Does |
|---|---|
| "stop listening", "mute" | **hard mute**: the microphone is closed, so there is no wake word either. Saying "Sterling, start listening" cannot work while muted. Turn it back on with the dashboard pill, the tray icon or **Ctrl+Alt+M**. |
| "stop", "quiet" | stops talking (barge-in), keeps listening for a follow-up |
| "never mind", "cancel" | drops the request in progress (the answer still lands in the history, unspoken) |
| "start listening" | answers "I'm listening" and opens a follow-up window |

Hotkeys: **Ctrl+Alt+Space** = talk now (push-to-talk; also unmutes), **Ctrl+Alt+M** = mute/unmute.

Barge-in: speak over it and it stops. With laptop speakers (no headset) its own voice can trigger
this; choose "Stops only for the wake word" in Settings (`VOICE_BARGE_IN=wake_only`).

## Picking a microphone
Automatic by default: the Windows default input, never "Stereo Mix"/loopback devices. If the mic
disappears (headset unplugged) it re-selects within a few seconds; while idle it re-scans every
minute so a newly plugged headset is picked up. To force one, type part of its name in Settings
(`VOICE_INPUT_DEVICE`); `python -m voice --list-devices` shows the names (`*` = chosen).

## First run
The first start downloads the models into `data/models` (about 125 MB: openWakeWord + Kokoro) and
faster-whisper `tiny.en` (about 75 MB) into the Hugging Face cache; progress is in the log. To do it
up front: `.venv\Scripts\python -m voice --download-models`.

## A custom "Hey Sterling" wake word
openWakeWord can train one from synthetic speech (its `automatic_model_training` notebook on
Google Colab, about an hour). Put the resulting `hey_sterling.onnx` in `data/models/wakewords/`;
it is loaded next to `hey_jarvis` automatically. Until then the name spotter handles "Sterling".

## Troubleshooting
- **It never hears anything**: Windows Settings -> Privacy & security -> Microphone -> turn on
  "Microphone access" and "Let desktop apps access your microphone". Then use "Test my mic".
- **The pill says "Voice off"**: `VOICE_ENABLED` is false, or the process stopped; see `data/logs/voice.log`.
- **No voice / robotic voice**: Kokoro failed to load (see the log); it fell back to Windows voices.
- **Linux**: the voice packages in requirements.txt are Windows-only. Install by hand:
  `pip install sounddevice kokoro-onnx webrtcvad-wheels onnxruntime scipy scikit-learn tqdm && pip install --no-deps openwakeword`
  (openwakeword's Linux dependency, tflite-runtime, has no Python 3.12+ wheels).
