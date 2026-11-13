"""The always-listening state machine.

::

    idle --wake word / "Sterling ..." / hotkey--> listening --end of speech--> thinking
      ^                                               |  (local command: handled here)   |
      |                                               v                                  v
      +------- no speech for followup_sec ------- followup <---- reply finished ---- speaking
                                                     ^                                   |
                                                     +-- he talks over it (barge-in) ----+
    any state --"stop listening" / dashboard / tray / Ctrl+Alt+M--> muted (mic closed) --toggle--> idle

Single-threaded and clock-driven so it can be tested frame by frame: the app (or a test) calls
:meth:`VoiceAgent.feed` with each 80 ms frame and :meth:`VoiceAgent.tick` when no audio arrives.
Only the brain (Claude Code, several seconds) runs on a worker thread; the tray and hotkey post
requests with :meth:`VoiceAgent.post`, which the loop picks up on its own thread.

Interaction details adapted from adewaskar/jarvis ``src/lib/voice.ts`` (MIT License, Copyright
(c) 2026 Aditya Dewaskar): a filler line the moment work starts and then quiet until the answer,
barge-in that keeps the mic open while speaking with a raised threshold and a 350 ms self-guard at
the start of each sentence, echo filtering of what is heard against what was just said, and
holding a sentence that trails off on "and"/"to" a moment longer before sending it.
"""

from __future__ import annotations

import logging
import queue
import random
import time
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from voice import audio, commands, speech
from voice.control import Control
from voice.settings import VoiceSettings

log = logging.getLogger(__name__)

IDLE, LISTENING, THINKING, SPEAKING, FOLLOWUP, MUTED = "idle", "listening", "thinking", "speaking", "followup", "muted"
SELF_GUARD_SEC = 0.35          # no barge-in this soon after a sentence starts (its own first syllable)
ECHO_TAIL_SEC = 1.8            # the speakers keep reaching the mic this long after a sentence ends
HOLD_SEC = 1.6                 # wait this much longer for the rest of an unfinished sentence
BARGE_FRAMES = 3               # 240 ms of loud speech while talking = he is interrupting
NAME_MAX_SEC = 10.0            # longest burst the name spotter transcribes
HEARTBEAT_SEC = 5.0
MUTE_POLL_SEC = 0.5

Brain = Callable[[str], str]
History = Callable[[str, str], None]


class Recorder:
    """One utterance: keeps a little audio from before the onset, confirms speech over two frames,
    ends after ``end_frames`` of quiet or ``max_frames`` in total."""

    def __init__(self, end_frames: int, max_frames: int, preroll: int = 4, confirm: int = 2):
        self.end_frames, self.max_frames, self.preroll, self.confirm = end_frames, max_frames, preroll, confirm
        self.reset()

    def reset(self) -> None:
        self.frames: list[np.ndarray] = []
        self.started = False
        self.run = 0
        self.quiet = 0

    def prime(self, frames: list[np.ndarray]) -> None:
        """Start already in speech with these frames (barge-in: his first words are not lost)."""
        self.reset()
        self.frames = list(frames)
        self.started = bool(frames)

    def push(self, frame: np.ndarray, is_speech: bool) -> bool:
        """Add a frame; True when the utterance is complete."""
        self.frames.append(frame)
        if not self.started:
            self.run = self.run + 1 if is_speech else 0
            if self.run >= self.confirm:
                self.started = True
            else:
                del self.frames[:-(self.preroll + self.confirm)]
            return False
        self.quiet = 0 if is_speech else self.quiet + 1
        return self.quiet >= self.end_frames or len(self.frames) >= self.max_frames

    def take(self) -> np.ndarray:
        out = np.concatenate(self.frames) if self.frames else np.zeros(0, dtype=np.int16)
        self.reset()
        return out


@dataclass
class Turn:
    text: str
    future: Future[str]
    started: float
    fillers: int = 0
    cancelled: bool = False


@dataclass
class _Say:
    text: str
    kind: str          # reply | filler | ack | chime


@dataclass
class Diag:
    wakes: int = 0
    heard: int = 0
    dropped: int = 0
    barge_ins: int = 0
    commands: int = 0
    last_heard: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class VoiceAgent:
    def __init__(self, *, wake: Any, vad: Any, stt: Any, tts: Any, player: Any, brain: Brain, control: Control,
                 settings: VoiceSettings, name_stt: Any = None, history: History | None = None,
                 clock: Callable[[], float] = time.monotonic, executor: Executor | None = None,
                 rng: random.Random | None = None):
        self.wake, self.vad, self.stt, self.tts, self.player = wake, vad, stt, tts, player
        self.name_stt = name_stt if settings.wake_mode in ("both", "name") or name_stt is not None else None
        self.brain, self.control, self.s = brain, control, settings
        self.history = history or (lambda heard, reply: None)
        self.clock = clock
        self.executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="voice-brain")
        rng = rng or random.Random()
        self.pick_working = speech.Picker(speech.WORKING, rng)
        self.pick_still = speech.Picker(speech.STILL, rng)
        self.pick_ack = speech.Picker(speech.ACKNOWLEDGE, rng)
        end = max(2, round(settings.end_silence_sec / audio.FRAME_SEC))
        mx = max(10, round(settings.max_utterance_sec / audio.FRAME_SEC))
        self.rec = Recorder(end, mx)
        self.side = Recorder(end, round(6 / audio.FRAME_SEC))
        self.idle_rec = Recorder(end, round(NAME_MAX_SEC / audio.FRAME_SEC))
        self.barge_gate = audio.EnergyGate()
        self.barge_gate.guard = True
        self.state = IDLE
        self.device_name = ""
        self.diag = Diag()
        self.turn: Turn | None = None
        self._queue: list[_Say] = []
        self._playing: _Say | None = None
        self._sentence_at = 0.0
        self._recent_spoken = ""
        self._recent_until = 0.0
        self._after = FOLLOWUP
        self._deadline = 0.0
        self._held = ""
        self._ring: list[np.ndarray] = []
        self._barge_run = 0
        self._posted: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._mute_checked = -1e9
        self._beat = -1e9
        self._conversing = False
        self.quit = False

    # ------------------------------------------------------------------ public
    @property
    def h(self) -> str:
        return self.s.honorific

    def post(self, request: str) -> None:
        """Thread-safe: "talk" (push-to-talk), "toggle_mute", "mute", "unmute", "quit"."""
        self._posted.put(request)

    def feed(self, frame: np.ndarray) -> None:
        self._drain()
        if self.state == MUTED or self.quit:
            return
        is_speech = bool(self.vad.is_speech(frame))
        self._ring = (self._ring + [frame])[-6:]
        handler = {IDLE: self._on_idle, LISTENING: self._on_listen, FOLLOWUP: self._on_listen,
                   THINKING: self._on_thinking, SPEAKING: self._on_speaking}[self.state]
        handler(frame, is_speech)
        self.tick()

    def tick(self) -> None:
        now = self.clock()
        self._drain()
        if now - self._mute_checked >= MUTE_POLL_SEC:
            self._mute_checked = now
            muted = self.control.muted()
            if muted and self.state != MUTED:
                self._enter_muted()
            elif not muted and self.state == MUTED:
                self._set(IDLE)
        if self.state == MUTED:
            self._heartbeat(now)
            return
        if self.turn is not None and self.state == THINKING:
            self._thinking_tick(now)
        self._speak_tick(now)
        if self.state == SPEAKING and not self.speaking:
            self._finished_speaking()
        if self.state in (LISTENING, FOLLOWUP) and not self.rec.started and now > self._deadline \
                and not self._chime_playing():
            if self._held:
                held, self._held = self._held, ""
                self._submit(held)
            else:
                self._set(IDLE)
        self._heartbeat(now)

    @property
    def speaking(self) -> bool:
        return bool(self._queue) or self.player.busy()

    # ------------------------------------------------------------------ states
    def _set(self, state: str, **extra: Any) -> None:
        if state in (IDLE, FOLLOWUP, LISTENING):
            self.rec.reset()
        if state == IDLE:
            self.idle_rec.reset()
            self._held = ""
            self._conversing = False
            self.wake.reset()
        changed = state != self.state
        self.state = state
        if changed:
            log.debug("state -> %s", state)
            self._publish(**extra)

    def _publish(self, **extra: Any) -> None:
        try:
            self.control.publish(self.state, device=self.device_name, voice=getattr(self.tts, "name", ""),
                                 wake=self.s.wake_mode, heard=self.diag.last_heard[:120], wakes=self.diag.wakes,
                                 barge_ins=self.diag.barge_ins, **extra)
        except Exception as exc:  # a locked DB must never stop the loop
            log.warning("status not published: %s", exc)
        self._beat = self.clock()

    def _heartbeat(self, now: float) -> None:
        if now - self._beat >= HEARTBEAT_SEC:
            self._publish()

    def _drain(self) -> None:
        while True:
            try:
                req = self._posted.get_nowait()
            except queue.Empty:
                return
            if req == "quit":
                self.quit = True
                self._stop_speech()
            elif req in ("mute", "unmute", "toggle_mute"):
                on = (req == "mute") or (req == "toggle_mute" and not self.control.muted())
                self.control.set_muted(on, via="hotkey" if req == "toggle_mute" else "tray")
                self._mute_checked = -1e9
                if on:
                    self._enter_muted()
                elif self.state == MUTED:
                    self._set(IDLE)
                    self._chime(660)
            elif req == "talk":
                if self.control.muted():
                    self.control.set_muted(False, via="hotkey")
                self._stop_speech()
                if self.turn is not None:
                    self.turn.cancelled = True
                    self.turn = None
                self._wake("hotkey")

    def _enter_muted(self) -> None:
        self._stop_speech()
        self.turn = None  # the answer still lands in the history; it is just not spoken
        self.rec.reset()
        self.side.reset()
        self.idle_rec.reset()
        self._set(MUTED)

    # ------------------------------------------------------------------ audio per state
    def _on_idle(self, frame: np.ndarray, is_speech: bool) -> None:
        hit = self.wake.process(frame)
        if hit:
            self.diag.wakes += 1
            self._wake(hit)
            return
        if self.name_stt is None:
            return
        if not self.idle_rec.push(frame, is_speech):
            return
        clip = self.idle_rec.take()
        text = self._transcribe(self.name_stt, clip)
        found = commands.addressed(text, self.s.assistant_name)
        if found is None:
            return
        self.diag.wakes += 1
        cmd = commands.parse(found.rest, self.s.assistant_name) if found.rest else None
        if cmd:
            self._command(cmd, text)
        elif len(found.rest.split()) >= 2:
            # "Sterling, what's on today?" in one breath: the whole thing is the request. Re-run it
            # through the full model for accuracy.
            full = self._transcribe(self.stt, clip) or text
            rest = commands.addressed(full, self.s.assistant_name)
            self.diag.last_heard = full
            self._submit(rest.rest if rest and rest.rest else found.rest)
        else:
            self._wake("name")

    def _wake(self, source: str) -> None:
        log.info("awake (%s)", source)
        self._set(LISTENING, woke=source)
        self.rec.reset()
        self._held = ""
        self._deadline = self.clock() + self.s.listen_timeout_sec
        self._chime(880)

    def _on_listen(self, frame: np.ndarray, is_speech: bool) -> None:
        if self._chime_playing():
            return  # do not record our own chime
        if self.rec.push(frame, is_speech):
            self._utterance(self._transcribe(self.stt, self.rec.take()))

    def _on_thinking(self, frame: np.ndarray, is_speech: bool) -> None:
        # Only local commands count while the brain works ("never mind", "stop", "stop listening").
        if self.side.push(frame, is_speech):
            text = self._transcribe(self.stt, self.side.take())
            if not text or self._is_echo(text):
                return
            cmd = commands.parse(text, self.s.assistant_name)
            if cmd in (commands.STOP, commands.CANCEL, commands.MUTE):
                self._command(cmd, text)
            else:
                self.diag.dropped += 1
                log.info("ignored while thinking: %r", text[:80])

    def _on_speaking(self, frame: np.ndarray, is_speech: bool) -> None:
        if self.s.barge_in == "off":
            return
        if self.wake.process(frame):
            self.diag.wakes += 1
            self._barge()
            return
        if self.s.barge_in != "auto":
            return
        loud = self.barge_gate.loud(frame)
        if self.clock() - self._sentence_at < SELF_GUARD_SEC:
            self._barge_run = 0
            return
        self._barge_run = self._barge_run + 1 if (is_speech and loud) else 0
        if self._barge_run >= BARGE_FRAMES:
            self._barge()

    def _barge(self) -> None:
        log.info("barge-in")
        self.diag.barge_ins += 1
        self._barge_run = 0
        self._stop_speech()
        self._set(LISTENING, woke="barge_in")
        self.rec.prime(self._ring[-4:])
        self._deadline = self.clock() + self.s.listen_timeout_sec

    # ------------------------------------------------------------------ words
    def _transcribe(self, engine: Any, clip: np.ndarray) -> str:
        if clip.size < audio.FRAME * 2:
            return ""
        try:
            return (engine.transcribe(clip) or "").strip()
        except Exception as exc:
            log.warning("speech recognition failed: %s", exc)
            return ""

    def _is_echo(self, text: str) -> bool:
        return self.clock() < self._recent_until and commands.is_echo(text, self._recent_spoken)

    def _back_to_waiting(self) -> None:
        if self._conversing:
            self._set(FOLLOWUP)
            self._deadline = self.clock() + max(self.s.followup_sec, 2.0)
        else:
            self._set(IDLE)

    def _utterance(self, text: str) -> None:
        if commands.is_noise(text) or self._is_echo(text):
            self.diag.dropped += 1
            if self._held:
                held, self._held = self._held, ""
                self._submit(held)
            else:
                self._back_to_waiting()
            return
        self.diag.heard += 1
        self.diag.last_heard = text
        cmd = commands.parse(text, self.s.assistant_name)
        if cmd and not self._held:
            self._command(cmd, text)
            return
        full = f"{self._held} {text}".strip()
        if commands.looks_unfinished(full) and not self._held:
            self._held = full
            self.rec.reset()
            self._deadline = self.clock() + HOLD_SEC
            return
        self._held = ""
        self._submit(full)

    def _submit(self, text: str) -> None:
        log.info("request: %r", text[:120])
        self._conversing = True
        self.turn = Turn(text, self.executor.submit(self.brain, text), self.clock())
        self.side.reset()
        self._set(THINKING)

    def _command(self, cmd: str, heard: str) -> None:
        self.diag.commands += 1
        log.info("local command %s (%r)", cmd, heard[:60])
        reply = ""
        if cmd == commands.MUTE:
            self._stop_speech()
            if self.turn is not None:
                self.turn.cancelled = True
                self.turn = None
            reply = (f"Microphone off, {self.h}. The dashboard, the tray icon or Control Alt M "
                     f"turns me back on.")
            self._say(reply, "ack", after=MUTED)
        elif cmd == commands.STOP:
            self._stop_speech()
            if self.turn is not None:
                self.turn.cancelled = True
                self.turn = None
            self._conversing = True
            self._back_to_waiting()
        elif cmd == commands.CANCEL:
            self._stop_speech()
            if self.turn is not None:
                self.turn.cancelled = True
                self.turn = None
            reply = self.pick_ack(self.h)
            self._say(reply, "ack", after=IDLE)
        elif cmd == commands.START:
            reply = f"I'm listening, {self.h}."
            self._conversing = True
            self._say(reply, "ack", after=FOLLOWUP)
        try:
            self.history(heard, reply)
        except Exception as exc:
            log.warning("voice history not saved: %s", exc)

    # ------------------------------------------------------------------ thinking / speaking
    def _thinking_tick(self, now: float) -> None:
        turn = self.turn
        assert turn is not None
        if turn.future.done():
            self.turn = None
            try:
                reply = turn.future.result()
            except Exception as exc:
                log.exception("brain failed")
                reply = f"Something went wrong on my side, {self.h}. The details are on your dashboard."
                self._publish(error=str(exc)[:200])
            if turn.cancelled:
                return
            if reply:
                self._say(reply, "reply", after=FOLLOWUP)
            else:
                self._back_to_waiting()
            return
        waited = now - turn.started
        if turn.fillers == 0 and waited >= self.s.filler_delay_sec:
            turn.fillers = 1
            self._queue.append(_Say(self.pick_working(self.h), "filler"))
        elif turn.fillers == 1 and waited >= self.s.filler_delay_sec + speech.STILL_AFTER_SEC:
            turn.fillers = 2
            self._queue.append(_Say(self.pick_still(self.h), "filler"))

    def _say(self, text: str, kind: str, *, after: str) -> None:
        self._queue.extend(_Say(t, kind) for t in speech.split_sentences(text))
        self._after = after
        self.barge_gate = audio.EnergyGate()
        self.barge_gate.guard = True
        self._set(SPEAKING)

    def _chime(self, freq: float) -> None:
        try:
            self.player.play(audio.tone(freq), 24000)
            self._playing = _Say("", "chime")
        except Exception as exc:
            log.debug("chime failed: %s", exc)

    def _chime_playing(self) -> bool:
        return self._playing is not None and self._playing.kind == "chime" and self.player.busy()

    def _speak_tick(self, now: float) -> None:
        if self.player.busy() or not self._queue:
            return
        item = self._queue.pop(0)
        try:
            samples, rate = self.tts.synthesize(item.text)
        except Exception as exc:
            log.warning("could not speak %r: %s", item.text[:40], exc)
            return
        self.player.play(samples, rate)
        self._playing = item
        self._sentence_at = self.clock()
        dur = samples.size / float(rate or 1)
        self._recent_spoken = (self._recent_spoken + " " + item.text)[-400:] if now < self._recent_until else item.text
        self._recent_until = self._sentence_at + dur + ECHO_TAIL_SEC

    def _stop_speech(self) -> None:
        self._queue.clear()
        try:
            self.player.stop()
        except Exception:  # pragma: no cover
            pass
        self._playing = None

    def _finished_speaking(self) -> None:
        after, self._after = self._after, FOLLOWUP
        if after == MUTED:
            self.control.set_muted(True, via="voice")
            self._mute_checked = self.clock()
            self._enter_muted()
        elif after == FOLLOWUP and self.s.followup_sec > 0:
            self._set(FOLLOWUP)
            self.wake.reset()
            self._deadline = self.clock() + self.s.followup_sec
        else:
            self._set(IDLE)


# ---------------------------------------------------------------------------
# the real brain and history (ClipAgent's assistant)
# ---------------------------------------------------------------------------

def assistant_brain(text: str) -> str:
    """Hand the transcript to the assistant (``run_turn(channel="voice")``, which records both
    messages) and return what to say. A long answer is also saved in full for the dashboard."""
    from assistant import persona, runtime, store
    from assistant.settings import get_assistant_settings

    a = get_assistant_settings()
    if not a.enabled:
        reply = f"I'm switched off at the moment, {a.owner_honorific}. You can turn me on in Settings."
        in_id = store.add_message("in", "voice", text)
        store.add_message("out", "voice", reply, kind="reply", reply_to=in_id)
        return reply
    res = runtime.run_turn(text, channel="voice")
    spoken, long = speech.prepare_reply(res.raw_text or res.text, honorific=a.owner_honorific)
    if long and res.status == "ok":
        store.add_message("out", "dashboard", persona.polish(res.raw_text or res.text, "dashboard"),
                          kind="voice_detail", agent="main", reply_to=res.message_in_id)
    return spoken or res.text


def store_history(heard: str, reply: str) -> None:
    """Local commands ("stop listening", "never mind"...) go into the same history, kind "command"."""
    from assistant import store

    in_id = store.add_message("in", "voice", heard, kind="command")
    if reply:
        store.add_message("out", "voice", reply, kind="command", reply_to=in_id)
