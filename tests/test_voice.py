"""voice/: state machine, local commands, fillers, barge-in, device choice/loss, history, settings form.

Offline, no audio hardware: synthetic frames and fake wake/VAD/STT/TTS/player engines. The real
engines (openwakeword, kokoro_onnx, sounddevice, faster_whisper...) are never imported.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from concurrent.futures import Future

import numpy as np
import pytest
from fastapi.testclient import TestClient

import config_store
import server
from assistant import store
from assistant_fixtures import assistant_env  # noqa: F401
from voice import agent as va
from voice import app as vapp
from voice import audio, commands, control, speech
from voice.settings import VoiceSettings, parse_form

pytestmark = pytest.mark.usefixtures("assistant_env")

WAKE, LOUD, QUIET = 7777, 3000, 0


def frame(kind: int = QUIET) -> np.ndarray:
    f = np.zeros(audio.FRAME, dtype=np.int16)
    if kind == WAKE:
        f[0] = WAKE
    elif kind == LOUD:
        f[:] = LOUD
    return f


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


class FakeWake:
    def process(self, f):
        return "hey_jarvis" if f[0] == WAKE else None

    def reset(self):
        pass


class FakeVad:
    def is_speech(self, f):
        return bool(np.abs(f.astype(np.int32)).mean() > 1000)


class FakeSTT:
    def __init__(self, *texts):
        self.texts = list(texts)

    def transcribe(self, clip):
        return self.texts.pop(0) if self.texts else ""


class FakeTTS:
    name = "fake"

    def __init__(self):
        self.said = []

    def synthesize(self, text):
        self.said.append(text)
        return np.zeros(100, dtype=np.float32), 1000      # 0.1 s


class FakePlayer:
    def __init__(self, clock):
        self.clock, self.end, self.played, self.stops = clock, 0.0, 0, 0

    def play(self, samples, rate):
        self.played += 1
        self.end = self.clock() + samples.size / rate

    def busy(self):
        return self.clock() < self.end

    def stop(self):
        self.stops += 1
        self.end = 0.0


class ManualExecutor:
    def __init__(self):
        self.calls = []

    def submit(self, fn, text):
        fut: Future = Future()
        self.calls.append((text, fut))
        return fut


def make(*texts, **kw):
    clock = Clock()
    s = VoiceSettings(wake_mode=kw.pop("wake_mode", "hey_jarvis"), end_silence_sec=0.3, followup_sec=8.0,
                      filler_delay_sec=0.7, listen_timeout_sec=6.0, **kw.pop("settings", {}))
    ex = ManualExecutor()
    history = []
    ctl = control.MemoryControl()
    tts = FakeTTS()
    a = va.VoiceAgent(wake=FakeWake(), vad=FakeVad(), stt=FakeSTT(*texts), tts=tts, player=FakePlayer(clock),
                      brain=lambda t: "unused", control=ctl, settings=s, clock=clock, executor=ex,
                      history=lambda h, r: history.append((h, r)), name_stt=kw.pop("name_stt", None))
    return a, clock, ex, tts, ctl, history


def run(a, clock, kind=QUIET, n=1):
    for _ in range(n):
        clock.t += audio.FRAME_SEC
        a.feed(frame(kind))


def say_utterance(a, clock):
    run(a, clock, QUIET, 3)      # let the chime finish
    run(a, clock, LOUD, 5)
    run(a, clock, QUIET, 5)      # end of speech


def test_full_cycle_wake_think_speak_followup_idle():
    a, clock, ex, tts, ctl, _ = make("what is on today")
    run(a, clock, WAKE)
    assert a.state == va.LISTENING
    say_utterance(a, clock)
    assert a.state == va.THINKING and ex.calls[0][0] == "what is on today"
    run(a, clock, QUIET, 12)                                      # ~1 s: one filler line, not two
    assert len(tts.said) == 1 and tts.said[0] in [speech.fill(x, "sir") for x in speech.WORKING]
    ex.calls[0][1].set_result("You have two meetings, sir. The first is at ten.")
    run(a, clock, QUIET)
    assert a.state == va.SPEAKING
    run(a, clock, QUIET, 10)
    assert tts.said[1:] == ["You have two meetings, sir.", "The first is at ten."]
    assert a.state == va.FOLLOWUP
    run(a, clock, QUIET, int(8 / audio.FRAME_SEC) + 2)
    assert a.state == va.IDLE
    assert [p["state"] for p in ctl.published][:3] == ["listening", "thinking", "speaking"]


def test_followup_needs_no_wake_word():
    a, clock, ex, tts, ctl, _ = make("first question", "and the second one please")
    run(a, clock, WAKE)
    say_utterance(a, clock)
    ex.calls[0][1].set_result("Answer.")
    run(a, clock, QUIET, 6)
    assert a.state == va.FOLLOWUP
    run(a, clock, LOUD, 5)
    run(a, clock, QUIET, 25)   # ends on a real word -> sent
    assert [c[0] for c in ex.calls] == ["first question", "and the second one please"]


def test_fast_answer_gets_no_filler():
    a, clock, ex, tts, ctl, _ = make("hello")
    run(a, clock, WAKE)
    say_utterance(a, clock)
    ex.calls[0][1].set_result("Good evening, sir.")
    run(a, clock, QUIET, 5)
    assert tts.said == ["Good evening, sir."]


def test_second_filler_after_long_wait():
    a, clock, ex, tts, *_ = make("long job")
    run(a, clock, WAKE)
    say_utterance(a, clock)
    clock.t += speech.STILL_AFTER_SEC + 1
    run(a, clock, QUIET, 3)
    assert len(tts.said) == 2 and tts.said[1] in [speech.fill(x, "sir") for x in speech.STILL]


def test_barge_in_stops_speech_and_listens():
    a, clock, ex, tts, *_ = make("tell me a story", "stop")
    run(a, clock, WAKE)
    say_utterance(a, clock)
    ex.calls[0][1].set_result(" ".join(f"Sentence number {i}." for i in range(4)))
    a.tts.synthesize = lambda text: (tts.said.append(text) or (np.zeros(3000, dtype=np.float32), 1000))  # 3 s each
    run(a, clock, QUIET, 6)
    assert a.state == va.SPEAKING
    run(a, clock, LOUD, 4)
    assert a.state == va.LISTENING and a.diag.barge_ins == 1 and a.player.stops >= 1
    run(a, clock, QUIET, 6)                                   # "stop": local command, nothing to Claude
    assert len(ex.calls) == 1 and a.state == va.FOLLOWUP


def test_cancel_during_thinking_discards_answer():
    a, clock, ex, tts, ctl, history = make("book a table", "never mind")
    run(a, clock, WAKE)
    say_utterance(a, clock)
    run(a, clock, LOUD, 4)
    run(a, clock, QUIET, 5)
    assert history == [("never mind", "Very good, sir.") ] or history[0][0] == "never mind"
    ex.calls[0][1].set_result("Booked.")
    run(a, clock, QUIET, 20)
    assert "Booked." not in tts.said and a.state == va.IDLE


def test_mute_command_is_hard_mute_and_toggle_unmutes():
    a, clock, ex, tts, ctl, history = make("stop listening")
    run(a, clock, WAKE)
    say_utterance(a, clock)
    run(a, clock, QUIET, 10)
    assert a.state == va.MUTED and ctl.muted() and ctl.mute_log[-1] == (True, "voice")
    assert history[0][0] == "stop listening" and "Microphone off" in history[0][1]
    run(a, clock, WAKE, 3)                                     # no wake word while muted
    assert a.state == va.MUTED
    a.post("toggle_mute")
    clock.t += 1
    a.tick()
    assert a.state == va.IDLE and not ctl.muted()


def test_dashboard_mute_is_picked_up_by_polling():
    a, clock, ex, tts, ctl, _ = make()
    ctl.set_muted(True, via="dashboard")
    clock.t += 1
    a.tick()
    assert a.state == va.MUTED
    ctl.set_muted(False, via="dashboard")
    clock.t += 1
    a.tick()
    assert a.state == va.IDLE


def test_push_to_talk_unmutes_and_listens():
    a, clock, ex, tts, ctl, _ = make()
    ctl.set_muted(True, via="tray")
    a.post("talk")
    clock.t += 1
    a.tick()
    assert a.state == va.LISTENING and not ctl.muted()


def test_name_spotter_one_breath_request():
    a, clock, ex, tts, ctl, _ = make("Sterling, what's the weather like", wake_mode="name",
                                     name_stt=FakeSTT("Sterling, what's the weather like"))
    run(a, clock, LOUD, 6)
    run(a, clock, QUIET, 5)
    assert a.state == va.THINKING and ex.calls[0][0] == "what's the weather like"


def test_echo_of_own_voice_is_dropped():
    a, clock, ex, tts, *_ = make("hi", "sentence number one sentence number two")
    run(a, clock, WAKE)
    say_utterance(a, clock)
    ex.calls[0][1].set_result("Sentence number one. Sentence number two.")
    run(a, clock, QUIET, 8)
    assert a.state == va.FOLLOWUP
    run(a, clock, LOUD, 5)
    run(a, clock, QUIET, 5)
    assert len(ex.calls) == 1 and a.diag.dropped >= 1


def test_commands_and_speech_helpers():
    assert commands.parse("Sterling, stop listening please") == commands.MUTE
    assert commands.parse("never mind") == commands.CANCEL
    assert commands.parse("quiet") == commands.STOP
    assert commands.parse("stop the upload") is None
    assert commands.addressed("hey stirling what time is it", "Sterling").rest == "what time is it"
    assert commands.addressed("good morning", "Sterling") is None
    assert commands.looks_unfinished("remind me to call the")
    assert speech.split_sentences("Mr. Stark is here. Shall I? Yes.") == ["Mr. Stark is here.", "Shall I?", "Yes."]
    spoken, long = speech.prepare_reply("Short answer, sir.", polish=lambda t: t)
    assert spoken == "Short answer, sir." and not long
    spoken, long = speech.prepare_reply("One. Two. Three. Four. Five. Six.", polish=lambda t: t)
    assert long and spoken == "One. Two. Three." and "dashboard" not in spoken


def test_choose_device_prefers_setting_then_default_and_skips_loopback():
    devs = [audio.Device(0, "Stereo Mix (Realtek)", "MME", 2), audio.Device(1, "Microphone Array (Realtek)", "MME", 2,
            is_default=True), audio.Device(2, "Headset Microphone (HyperX)", "MME", 1)]
    assert audio.choose_device(devs).index == 1
    assert audio.choose_device(devs, "hyperx").index == 2
    assert audio.choose_device(devs, "gone").index == 1
    assert audio.choose_device([devs[0]]) is None
    gate = audio.EnergyGate()
    assert not any(gate.loud(frame(QUIET)) for _ in range(5)) and gate.loud(frame(LOUD))


def test_device_loss_reselects():
    a, clock, *_ = make()
    opened = []

    class Mic:
        def __init__(self, name, frames):
            self.device = audio.Device(len(opened), name)
            self.frames = frames

        def read(self, timeout=0.5):
            if not self.frames:
                raise audio.DeviceLost("unplugged")
            return self.frames.pop(0)

        def close(self):
            pass

    def open_mic():
        if len(opened) >= 2:
            a.post("quit")
        m = Mic("Headset" if not opened else "Laptop mic", [frame()] * 3)
        opened.append(m)
        return m

    vapp.run_loop(a, a.s.replace(rescan_sec=0), open_mic=open_mic, sleep=lambda s: None)
    assert [m.device.name for m in opened][:2] == ["Headset", "Laptop mic"]


def test_voice_history_saved():
    va.store_history("stop listening", "Microphone off, sir.")
    reply = va.assistant_brain("what's on today")   # assistant disabled: canned reply, still recorded
    assert "switched off" in reply
    msgs = store.list_messages(10, channel="voice")
    assert {m.body for m in msgs} >= {"stop listening", "Microphone off, sir.", "what's on today", reply}
    assert {m.kind for m in msgs} >= {"command", "reply"}


def test_settings_form_parsing():
    errors, changes = parse_form({"voice_present": "1", "voice_enabled": "1", "voice_device": "HyperX",
                                  "voice_wake": "both", "voice_tts": "kokoro", "voice_voice": "bm_lewis"})
    assert not errors and changes["VOICE_ENABLED"] == "true" and changes["VOICE_TTS_VOICE"] == "bm_lewis"
    assert parse_form({"voice_present": "1"})[1] == {"VOICE_ENABLED": "false"}
    errors, _ = parse_form({"voice_tts": "elevenlabs"})
    assert "voice_tts" in errors
    errors, _ = parse_form({"voice_wake": "nope"})
    assert "voice_wake" in errors


def test_web_status_mute_and_settings_page(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPAGENT_CONFIG_FILE", str(tmp_path / "cfg" / "config.env"))
    monkeypatch.setenv("SETUP_COMPLETE", "true")
    config_store.reload()
    c = TestClient(server.create_app(), base_url="http://127.0.0.1:8000")
    st = c.get("/voice/api/status", headers={"Accept": "application/json"}).json()
    assert st["state"] == "offline" and st["muted"] is False
    html = c.get("/setup/power").text
    assert 'name="voice_enabled"' in html and "data-voice-test" in html
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1)
    r = c.post("/voice/api/mute", data={"csrf": csrf, "muted": "1"}, headers={"Accept": "application/json"})
    assert r.json() == {"ok": True, "muted": True}
    assert control.status()["muted"]
    # the ring turning back on unmutes the laptop loop; a forged request is refused
    r = c.post("/voice/api/mute", data={"csrf": csrf, "muted": "0"}, headers={"Accept": "application/json"})
    assert r.json() == {"ok": True, "muted": False} and not control.status()["muted"]
    r = c.post("/voice/api/mute", data={"csrf": "bad", "muted": "1"}, headers={"Accept": "application/json"})
    assert r.status_code == 403 and not control.status()["muted"]


def test_no_heavy_imports():
    for mod in ("sounddevice", "openwakeword", "kokoro_onnx", "webrtcvad", "pystray", "pynput"):
        assert mod not in sys.modules


# --------------------------------------------------------------------------- in-browser voice (Jarvis view)

class _FakeSTT:
    def __init__(self, text):
        self.text, self.calls = text, 0

    def transcribe(self, samples):
        self.calls += 1
        return self.text


class _FakeTTS:
    name = "fake"

    def synthesize(self, text):
        return np.zeros(2400, dtype=np.float32), 24000


@pytest.fixture
def talk(tmp_path, monkeypatch):
    import home
    from assistant import settings as asettings
    from voice import web

    import config

    monkeypatch.setenv("CLIPAGENT_CONFIG_FILE", str(tmp_path / "cfg" / "config.env"))
    monkeypatch.setenv("SETUP_COMPLETE", "true")
    monkeypatch.setenv("ASSISTANT_ENABLED", "true")
    monkeypatch.setenv("FAKE_ASSISTANT", "true")
    config_store.reload()
    config.reset_settings()
    asettings.reset_assistant_settings()
    monkeypatch.setattr(home, "_hits", {})
    stt = _FakeSTT("what is on today")
    web.set_engines(stt=stt, tts=_FakeTTS())
    c = TestClient(server.create_app(), base_url="http://127.0.0.1:8000")
    yield c, stt, web
    web.set_engines()


def _wav(seconds: float = 1.0, rate: int = 16000) -> bytes:
    return web_encode(np.full(int(seconds * rate), 0.1, dtype=np.float32), rate)


def web_encode(samples, rate):
    from voice import web

    return web.encode_wav(samples, rate)


def _csrf_of(c) -> str:
    return re.search(r'name="csrf" value="([0-9a-f]+)"', c.get("/dashboard/").text).group(1)


def _talk(c, csrf, data, **headers):
    return c.post("/voice/api/talk", data={"csrf": csrf} if csrf else {}, files={"audio": ("speech.wav", data, "audio/wav")},
                  headers={"Accept": "application/json", **headers})


def test_browser_talk_is_speech_to_text_only(talk):
    c, stt, web = talk
    csrf = _csrf_of(c)
    r = _talk(c, csrf, _wav())
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] and j["heard"] == "what is on today" and "job" not in j and "turn" not in j
    assert not store.list_messages(10, channel="voice")        # nothing asked yet: the page asks next
    stt.text = "stop listening"
    assert _talk(c, csrf, _wav()).json()["command"] == "mute"
    stt.text = "Thank you."
    j = _talk(c, csrf, _wav()).json()
    assert j["error"] == "no_speech" and j["quiet"]
    assert _talk(c, csrf, _wav(0.1)).json()["error"] == "too_short"


def _sse(c, after=0, **headers):
    import json as _json

    r = c.get(f"/voice/api/stream?after={after}&once=1", headers=headers)
    out = []
    for block in r.text.split("\n\n"):
        lines = dict(ln.split(": ", 1) for ln in block.splitlines() if ": " in ln and not ln.startswith(":"))
        if "event" in lines:
            out.append((lines["event"], _json.loads(lines["data"])))
    return r, out


def test_ask_streams_the_turn_over_sse(talk):
    from assistant import brain

    c, stt, web = talk
    csrf = _csrf_of(c)
    r = c.post("/voice/api/ask", data={"csrf": csrf, "text": "what is on today"}, headers={"Accept": "application/json"})
    assert r.status_code == 200, r.text
    tid = r.json()["turn"]
    for _ in range(300):
        if any(e[0] == "done" for e in _sse(c)[1]):
            break
        import time as _t

        _t.sleep(0.02)
    resp, evs = _sse(c)
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert evs[0][0] == "hello" and "seq" in evs[0][1]
    kinds = [k for k, _ in evs]
    assert kinds.index("turn") < kinds.index("sentence") < kinds.index("done")
    turn = next(d for k, d in evs if k == "turn")
    assert turn["turn"] == tid and turn["origin"] == "browser" and turn["kind"] == "chat"
    sent = next(d for k, d in evs if k == "sentence")
    assert sent["turn"] == tid and sent["text"] and isinstance(sent["id"], int) and "ts" in sent
    done = next(d for k, d in evs if k == "done")
    assert done["status"] == "ok" and done["text"]
    last = evs[-1][1]["id"]
    assert [k for k, _ in _sse(c, after=last)[1]] == ["hello"]               # resumes after the last seen id
    act = c.get("/voice/api/activity").json()
    assert act["turns"][-1]["id"] == tid and act["voice"]["web_tts"] == "browser"
    # noise / fillers get no reply at all
    r = c.post("/voice/api/ask", data={"csrf": csrf, "text": "Maybe, um."}, headers={"Accept": "application/json"})
    assert r.json() == {"ok": False, "error": "no_speech", "quiet": True, "reason": "filler"}
    assert sum(1 for k, _ in _sse(c)[1] if k == "turn") == 1
    assert any(k == "ignored" for k, _ in _sse(c)[1])
    assert brain.get_brain().connects == 1


def test_sse_and_ask_need_auth(talk, monkeypatch):
    import config

    c, stt, web = talk
    csrf = _csrf_of(c)
    assert c.post("/voice/api/ask", data={"text": "hi"}, headers={"Accept": "application/json"}).status_code == 403
    assert c.post("/voice/api/interrupt", data={"csrf": "x"}, headers={"Accept": "application/json"}).status_code == 403
    assert c.post("/voice/api/interrupt", data={"csrf": csrf}, headers={"Accept": "application/json"}).json()["ok"]
    monkeypatch.setenv("DASHBOARD_TOKEN", "tok")
    config.reset_settings()
    assert c.get("/voice/api/stream?once=1").status_code == 401
    assert c.get("/voice/api/activity").status_code == 401
    assert c.get("/voice/api/stream?once=1", headers={"Authorization": "Bearer tok"}).status_code == 200


def test_voice_process_asks_with_its_local_key_and_streams_sentences(talk):
    import json as _json

    c, stt, web = talk
    key = web.local_key()
    r = c.post("/voice/api/ask", data={"text": "what is on today", "origin": "voice"},
               headers={"X-Voice-Key": key, "Accept": "application/x-ndjson"})
    assert r.status_code == 200
    lines = [_json.loads(x) for x in r.text.splitlines() if x.strip()]
    assert lines[0]["sentence"] and lines[-1]["done"] and lines[-1]["status"] == "ok"
    assert c.post("/voice/api/ask", data={"text": "hi"}, headers={"X-Voice-Key": "wrong",
                                                                   "Accept": "application/json"}).status_code == 403
    from voice import agent as vagent

    class Resp:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_lines(self):
            yield from r.text.splitlines()

    heard = []
    out = vagent.remote_reply("what is on today", heard.append, post=lambda **kw: Resp())
    assert heard == [x["sentence"] for x in lines if "sentence" in x] and out["status"] == "ok"


def test_page_tts_endpoint_and_voice_list(talk):
    c, stt, web = talk
    csrf = _csrf_of(c)
    r = c.post("/voice/api/tts", data={"csrf": csrf, "text": "Good evening, sir."})
    assert r.status_code == 200 and r.content[:4] == b"RIFF" and r.headers["x-voice"] == "fake"
    web.set_engines(stt=stt, tts=None)
    assert c.post("/voice/api/tts", data={"csrf": csrf, "text": "Hello."}).status_code == 503
    r = c.post("/voice/api/voices", data={"csrf": csrf, "voices": '["Google UK English Male", "Daniel"]'})
    from assistant import live_settings

    assert r.json()["count"] == 2 and live_settings.browser_voices() == ["Google UK English Male", "Daniel"]


def test_browser_talk_guards_csrf_size_and_bad_audio(talk, monkeypatch):
    c, stt, web = talk
    csrf = _csrf_of(c)
    assert _talk(c, None, _wav()).status_code == 403
    assert _talk(c, "0" * 64, _wav()).status_code == 403
    assert _talk(c, csrf, _wav(), Origin="https://evil.example").status_code == 403
    assert c.get("/voice/api/talk").status_code == 405
    assert _talk(c, csrf, _wav(web.TALK_MAX_SEC + 5)).status_code == 413
    monkeypatch.setattr(web, "TALK_MAX_BYTES", 1000)
    assert _talk(c, csrf, _wav()).status_code == 413
    monkeypatch.setattr(web, "TALK_MAX_BYTES", 4 * 1024 * 1024)
    web.set_engines(stt=stt, tts=None, decoder=lambda b: (_ for _ in ()).throw(web.AudioDecodeError("x")))
    assert _talk(c, csrf, b"not audio").status_code == 400
    assert stt.calls == 0
    monkeypatch.setenv("DASHBOARD_TOKEN", "tok")
    import config

    config.reset_settings()
    assert _talk(c, csrf, _wav()).status_code == 401


def test_jarvis_home_renders_with_and_without_voice_process(talk):
    c, _stt, _web = talk
    store.add_message("in", "voice", '<script>alert("x")</script>')
    html = c.get("/dashboard/").text
    assert "data-jarvis" in html and "data-jv-core" in html and "jarvis.js" in html and 'id="history"' in html
    assert "<script>alert" not in html and "&lt;script&gt;" in html and "chan-voice" in html
    # one mic control: the ring is a toggle; no separate "always listen" switch or Mute button;
    # History / Stop / Undo sit in the quiet "..." menu; a first-visit tip
    assert 'data-jv-ptt aria-pressed="false"' in html and "Tap to talk" in html
    assert "data-jv-always" not in html and "data-jv-mute" not in html and "Always listen" not in html
    assert "data-jv-menu" in html and "data-jv-history" in html and "data-jv-tip" in html
    assert "data-jv-rail" in html and "data-jv-steps" in html and "data-jv-tasks" in html and "data-jv-panels" in html
    assert c.get("/voice/api/status").json()["state"] == "offline"
    control.Control().publish("speaking", heard="hi", said="Good evening, sir.")
    st = c.get("/voice/api/status").json()
    assert st["state"] == "speaking" and st["said"] == "Good evening, sir."
    assert c.get("/dashboard/").status_code == 200
    csp = c.get("/dashboard/").headers["content-security-policy"]
    assert "script-src 'self'" in csp


def test_mic_falls_back_to_another_windows_audio_api(monkeypatch):
    """Regression: MME refused the G733 headset mic ("Unanticipated host error"); the same mic via
    WASAPI must be tried before giving up."""
    import types
    from voice import audio

    mme = audio.Device(index=1, name="Microphone (G733 Gaming Headset", hostapi="MME")
    wasapi = audio.Device(index=7, name="Microphone (G733 Gaming Headset)", hostapi="Windows WASAPI")
    opened = []

    class FakeStream:
        def __init__(self, *, device, **kw):
            if device == 1:
                raise RuntimeError("Error opening InputStream: Unanticipated host error [MME error 1]")
            opened.append(device)
            self.active = True

        def start(self):
            pass

    monkeypatch.setattr(audio, "_sd", lambda: types.SimpleNamespace(InputStream=FakeStream))
    monkeypatch.setattr(audio, "list_input_devices", lambda: [mme, wasapi])
    mic = audio.MicSource(mme).open()
    assert opened == [7]
    assert mic.device.hostapi == "Windows WASAPI"


class _StreamExecutor(ManualExecutor):
    def submit(self, fn, text, *rest):
        fut: Future = Future()
        self.calls.append((text, fut, rest))
        return fut


def test_streamed_sentences_are_spoken_before_the_answer_finishes_and_skip_the_filler():
    a, clock, _, tts, ctl, _ = make("what is on today")
    ex = _StreamExecutor()
    a.executor = ex
    brain = lambda text, on_sentence=None: "unused"  # noqa: E731
    brain.streams = True
    a.brain = brain
    run(a, clock, WAKE)
    say_utterance(a, clock)
    text, fut, (put,) = ex.calls[0]
    put("Quiet morning, sir.")
    run(a, clock, QUIET)
    assert a.state == va.SPEAKING and tts.said == ["Quiet morning, sir."]   # first sentence, no filler
    run(a, clock, QUIET, 20)
    assert a.state == va.SPEAKING                    # the answer is still being generated
    put("One approval is waiting.")
    fut.set_result("Quiet morning, sir. One approval is waiting.")
    run(a, clock, QUIET, 10)
    assert tts.said == ["Quiet morning, sir.", "One approval is waiting."]
    assert a.state == va.FOLLOWUP


def test_announcer_speaks_finished_tasks_when_idle():
    a, clock, ex, tts, ctl, _ = make()
    pending = [["Sir, the report is done."]]
    a.announcer = lambda: pending.pop() if pending else []
    run(a, clock, QUIET, 40)
    assert tts.said == ["Sir, the report is done."]


def test_laptop_voice_reloads_when_the_voice_setting_changes():
    a, clock, ex, tts, ctl, _ = make()
    new = FakeTTS()
    new.name = "kokoro:bm_lewis"
    calls = []
    a.tts_reloader = lambda: (calls.append(1), new if len(calls) == 2 else None)[1]
    run(a, clock, QUIET, 200)              # ~16 s of ticks: the reloader is polled every few seconds
    assert len(calls) >= 3 and a.tts is new


def test_laptop_voice_ignores_fillers_and_the_reload_reads_config(tmp_path, monkeypatch):
    a, clock, ex, tts, ctl, _ = make("Maybe, um.")
    run(a, clock, WAKE)
    say_utterance(a, clock)
    assert ex.calls == [] and a.diag.dropped == 1
    import config
    from voice import settings as vs

    cfg = tmp_path / "config.env"
    cfg.write_text("VOICE_TTS_VOICE=bm_george\n", encoding="utf-8")
    monkeypatch.setenv("CLIPAGENT_CONFIG_FILE", str(cfg))
    config.reset_settings()
    vs.reset_voice_settings()
    monkeypatch.setattr(vs, "_file_mtime", None)
    assert vs.reload_if_changed() is None                 # first look: remembers the file
    import os

    cfg.write_text("VOICE_TTS_VOICE=bm_lewis\n", encoding="utf-8")
    os.utime(cfg, (1e9, 2e9))
    assert vs.reload_if_changed().tts_voice == "bm_lewis"


def test_laptop_stitches_a_second_breath_onto_the_turn_in_flight():
    a, clock, ex, tts, ctl, _ = make("On Monday I worked nine hours. On Wednesday I worked eight.",
                                     "And Thursday eight as well, please log those.")
    run(a, clock, WAKE)
    say_utterance(a, clock)
    assert a.state == va.THINKING and len(ex.calls) == 1
    run(a, clock, LOUD, 4)                     # he keeps talking before any answer has started
    run(a, clock, QUIET, 5)
    assert [c[0] for c in ex.calls] == ["On Monday I worked nine hours. On Wednesday I worked eight.",
                                        "And Thursday eight as well, please log those."]   # the server merges
    ex.calls[0][1].set_result("")             # the replaced turn settles: nothing of it is spoken
    ex.calls[1][1].set_result("Logged, sir.")
    run(a, clock, QUIET, 30)
    assert tts.said == ["Logged, sir."]


def test_dangling_clause_holds_the_turn_open():
    assert commands.looks_unfinished("On Monday, I worked nine hours. On Tuesday, I worked eight hours. On Wednesday.")
    assert commands.looks_unfinished("I worked eight hours and")
    assert not commands.looks_unfinished("On Wednesday I worked eight hours.")
    assert not commands.looks_unfinished("So can you log the rest of those?")


def test_browser_speech_recognition_path_asks_with_text_and_reports_timing(talk, monkeypatch):
    """VOICE_STT: in Chrome/Edge the page transcribes itself and sends text to /ask (no upload, no Whisper)."""
    from assistant import events

    c, stt, _web = talk
    a = c.get("/voice/api/activity").json()
    assert a["voice"]["stt"] == "auto"
    csrf = _csrf_of(c)
    r = c.post("/voice/api/ask", data={"csrf": csrf, "text": "show me where Big Ben is", "origin": "browser"},
               headers={"Accept": "application/json"})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["turn"]
    assert stt.calls == 0                                            # no server speech-to-text involved
    after = events.BUS.seq
    r = c.post("/voice/api/timing", data={"csrf": csrf, "turn": r.json()["turn"], "first_word_ms": "640",
                                         "stt": "browser"}, headers={"Accept": "application/json"})
    assert r.json()["ok"] and events.BUS.since(after, 10)[-1]["timing"]["speech_to_first_word_ms"] == 640
    js = (Path(__file__).resolve().parent.parent / "static" / "jarvis.js").read_text(encoding="utf-8")
    for needle in ("window.SpeechRecognition || window.webkitSpeechRecognition", "rec.interimResults = true",
                   "if (idle < 15000) return;", "diag.restarts++", "err === 'network'", "/voice/api/timing",
                   "if (speaker.busy()) speaker.pause();"):
        assert needle in js, needle


def test_voice_stt_setting(monkeypatch, tmp_path):
    from voice import settings as vs

    for raw, want in (("browser", "browser"), ("SERVER", "server"), ("nonsense", "auto"), ("", "auto")):
        assert vs.load_voice_settings({"VOICE_STT": raw, "CLIPAGENT_CONFIG_FILE": str(tmp_path / "none.env"),
                                       "CLIPAGENT_DATA_DIR": str(tmp_path)}).web_stt == want
    errors, changes = vs.parse_form({"voice_stt": "server"})
    assert not errors and changes == {"VOICE_STT": "server"}
    assert vs.parse_form({"voice_stt": "lasers"})[0]
