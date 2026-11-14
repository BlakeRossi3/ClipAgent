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
- On the Jarvis page replies are spoken by the browser's own voice by default (instant; the best British
  male voice installed: "Microsoft Ryan Online (Natural)" in Edge, "Google UK English Male" in Chrome).
  Settings -> Power & privacy -> Voice picks another browser voice, Kokoro (`bm_george`, `bm_lewis`,
  `bm_fable`, `bm_daniel`; on this laptop) or ElevenLabs (voice `JBFqnCBsd6RMkjVDRZzb`, as in
  adewaskar/jarvis; needs `ELEVENLABS_API_KEY`; then the reply **text** goes to ElevenLabs). The laptop
  voice process has no browser: it uses Kokoro, Windows (SAPI) voices or ElevenLabs.
- Say "switch to Lewis" (or "use the browser voice", "use Ryan"): Sterling changes it himself and the
  next sentence is in the new voice. A change in Settings also applies at once (no restart).
- Every exchange is saved in the chat history (channel "voice"). Answers are spoken, never "see the dashboard".

## One brain (`assistant/brain.py`)
Modelled on adewaskar/jarvis (MIT): one warm Claude Agent SDK session in the server does the talking
and the work, with every power and the whole day's conversation (details in docs/ASSISTANT.md).
- The page streams everything live from `GET /voice/api/stream` (server-sent events): the text, each
  finished sentence (spoken as soon as it completes), tool steps ("Reading home.py", "Running tests"),
  background tasks with elapsed time and result, and panels Sterling draws with `display`. The
  **activity rail** beside the ring shows them.
- Long work: "That'll take a few minutes, sir. I'll tell you when it's done." It runs in a parallel
  session; when it finishes Sterling says so once, on the page or laptop that asked.
- Barge-in: speaking over him pauses him at once; if it was a real request he stops (and the answer in
  flight is cancelled), if it was noise or his own echo he carries on. A filler line ("Running the
  tests.") plays only when the first tool starts and nothing has been said yet.
- Fillers and noise ("um", "maybe, um", "thank you" from Whisper's imagination) get no reply; a sentence
  that trails off ("remind me to...") waits a moment for the rest.
- The laptop voice process asks the same brain over `POST /voice/api/ask` (loopback, a local key from
  the database) and speaks the sentences as they stream; if the server is down it runs its own.

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
