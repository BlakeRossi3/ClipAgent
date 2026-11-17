"""voice/smart_turn.py: Whisper features, the wait rule, the voice loop's endpointing and the page's /talk path.
A fake detector stands in for the ONNX model (no download, no onnxruntime session)."""

from __future__ import annotations

import io
import wave

import numpy as np
import pytest

import config
from voice import audio, smart_turn
from voice.settings import load_voice_settings


class FakeDetector:
    def __init__(self, prob):
        self.prob, self.calls = prob, []

    def predict(self, x):
        self.calls.append(len(x))
        return self.prob(x) if callable(self.prob) else self.prob


def test_features_shape_and_padding_at_the_front():
    short = np.ones(16000, dtype=np.float32) * 0.1
    x = smart_turn.prepare(short)
    assert x.shape == (smart_turn.N_SAMPLES,) and not x[:1000].any() and x[-1] == pytest.approx(0.1)
    long = np.arange(20 * 16000, dtype=np.float32)
    assert smart_turn.prepare(long)[0] == long[-smart_turn.N_SAMPLES]              # the LAST 8 s are kept
    assert smart_turn.prepare(np.array([16384], dtype=np.int16))[-1] == pytest.approx(0.5)
    f = smart_turn.features(np.random.default_rng(1).standard_normal(48000).astype(np.float32) * 0.1)
    assert f.shape == (1, 80, 800) and f.dtype == np.float32 and np.isfinite(f).all()


def test_features_match_whisper_feature_extraction():
    fw = pytest.importorskip("faster_whisper.feature_extractor")
    fe = fw.FeatureExtractor(feature_size=80, chunk_length=8)
    assert np.abs(fe.mel_filters.T - smart_turn.mel_filters()).max() < 1e-6
    a = (np.random.default_rng(0).standard_normal(16000 * 3) * 0.1).astype(np.float32)
    x = smart_turn.prepare(a).astype(np.float64)
    x = (x - x.mean()) / np.sqrt(x.var() + 1e-7)
    ref = fe(x.astype(np.float32), padding=0)[:, :800]
    assert np.abs(ref - smart_turn.features(a)[0]).max() < 1e-4


def test_end_wait_rule():
    assert smart_turn.end_wait_ms(None, 1200) == 1200                         # no model: the timer alone
    assert smart_turn.end_wait_ms(0.9, 1200) == smart_turn.EARLY_MS          # finished: end early
    assert smart_turn.end_wait_ms(0.0, 1200) == smart_turn.EXTEND_MS         # not finished: up to the cap
    assert 1200 < smart_turn.end_wait_ms(0.3, 1200) < smart_turn.EXTEND_MS
    assert smart_turn.end_wait_ms(0.0, 2500) == 2500                         # never shorter than the timer


def test_setting_and_missing_model_fall_back_to_the_timer(tmp_path, monkeypatch):
    assert load_voice_settings({"VOICE_SMART_TURN": "off"}).smart_turn == "off"
    assert load_voice_settings({}).smart_turn == "auto"
    assert smart_turn.get_detector() is None                                 # not downloaded: no model
    assert smart_turn.predict(np.zeros(16000, dtype=np.float32)) == (None, 0)
    smart_turn.set_detector(FakeDetector(0.9))
    p, ms = smart_turn.predict(np.zeros(16000, dtype=np.float32))
    assert p == pytest.approx(0.9) and ms >= 0
    monkeypatch.setenv("VOICE_SMART_TURN", "off")
    config.reset_settings()
    assert smart_turn.predict(np.zeros(16000, dtype=np.float32)) == (None, 0)


def test_broken_detector_never_raises():
    class Boom:
        def predict(self, x):
            raise RuntimeError("bad model")

    smart_turn.set_detector(Boom())
    p, _ms = smart_turn.predict(np.zeros(160, dtype=np.float32))
    assert p is None


def test_download_lands_in_models_dir_with_progress(tmp_path, monkeypatch):
    from voice import models

    got = []
    monkeypatch.setattr(models, "download", lambda url, dest, say: (got.append(url), dest.parent.mkdir(parents=True),
                                                                   dest.write_bytes(b"x"), say("100%")))
    lines = []
    assert smart_turn.ensure_model(tmp_path, lines.append) == []
    assert got == [smart_turn.MODEL_URL] and smart_turn.model_path(tmp_path).is_file()
    assert any("one time" in m for m in lines) and smart_turn.ensure_model(tmp_path, lines.append) == []
    assert len(got) == 1                                                     # never twice


# -- the laptop voice loop: Recorder endpointing --------------------------------------------------

def _frames(n, loud):
    return [np.full(audio.FRAME, 3000 if loud else 0, dtype=np.int16) for _ in range(n)]


def _run(rec, speech, quiet_max=60):
    for f in _frames(speech, True):
        assert not rec.push(f, True)
    for i, f in enumerate(_frames(quiet_max, False), 1):
        if rec.push(f, False):
            return i
    return None


def test_recorder_ends_early_when_smart_turn_says_finished():
    from voice.agent import Recorder

    end = round(1.2 / audio.FRAME_SEC)
    det = FakeDetector(0.95)
    rec = Recorder(end, 400, endpointer=lambda x: (det.predict(x), 7))
    quiet = _run(rec, 10)
    assert quiet == rec.short_frames < end and det.calls                     # ~300 ms instead of 1.2 s
    assert rec.eot == (0.95, 7)


def test_recorder_waits_longer_when_he_has_not_finished_and_rejudges_after_more_speech():
    from voice.agent import Recorder

    end = round(1.2 / audio.FRAME_SEC)
    det = FakeDetector(0.05)
    rec = Recorder(end, 400, endpointer=lambda x: (det.predict(x), 5))
    quiet = _run(rec, 10)
    assert quiet > end and quiet * audio.FRAME_SEC * 1000 <= smart_turn.EXTEND_MS + 100
    det2 = FakeDetector(0.05)
    rec2 = Recorder(end, 400, endpointer=lambda x: (det2.predict(x), 5))
    for f in _frames(10, True):
        rec2.push(f, True)
    for f in _frames(rec2.short_frames, False):
        assert not rec2.push(f, False)
    for f in _frames(5, True):                    # he carried on: the next pause is judged again, on more audio
        assert not rec2.push(f, True)
    det2.prob = 0.9
    for f in _frames(rec2.short_frames, False):
        done = rec2.push(f, False)
    assert done and len(det2.calls) == 2 and det2.calls[1] > det2.calls[0]


def test_recorder_without_a_model_uses_the_timer():
    from voice.agent import Recorder

    end = round(1.2 / audio.FRAME_SEC)
    assert _run(Recorder(end, 400), 10) == end
    assert _run(Recorder(end, 400, endpointer=lambda x: (None, 0)), 10) == end


# -- the Jarvis page's server speech-to-text path ----------------------------------------------------

def _wav(seconds=1.0):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
        w.writeframes((np.ones(int(16000 * seconds)) * 1000).astype(np.int16).tobytes())
    return buf.getvalue()


@pytest.fixture
def page(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import config_store
    import home
    import server
    from assistant import settings as asettings
    from voice import web

    monkeypatch.setenv("CLIPAGENT_CONFIG_FILE", str(tmp_path / "cfg" / "config.env"))
    monkeypatch.setenv("ASSISTANT_ENABLED", "true")
    monkeypatch.setenv("FAKE_ASSISTANT", "true")
    config_store.reload()
    config.reset_settings()
    asettings.reset_assistant_settings()
    monkeypatch.setattr(home, "_hits", {})

    class STT:
        def transcribe(self, samples):
            return "what time is it"

    web.set_engines(stt=STT())
    c = TestClient(server.create_app(), base_url="http://127.0.0.1:8000")
    yield c, web
    web.set_engines()
    web._turn_clip.update(at=0.0, samples=None)


def _csrf(c):
    import re

    return re.search(r'name="csrf" value="([0-9a-f]+)"', c.get("/dashboard/").text).group(1)


def test_talk_returns_the_end_of_turn_verdict_on_the_whole_turn(page):
    c, web = page
    det = FakeDetector(lambda x: 0.2 if len(x) < 30000 else 0.9)
    smart_turn.set_detector(det)
    csrf = _csrf(c)
    r = c.post("/voice/api/talk", data={"csrf": csrf}, files={"audio": ("a.wav", _wav(1.0), "audio/wav")},
               headers={"Accept": "application/json"})
    j = r.json()
    assert j["ok"] and j["eot"] == pytest.approx(0.2) and j["eot_ms"] >= 0
    r = c.post("/voice/api/talk", data={"csrf": csrf, "cont": "1"}, files={"audio": ("a.wav", _wav(1.0), "audio/wav")},
               headers={"Accept": "application/json"})
    assert r.json()["eot"] == pytest.approx(0.9) and det.calls[-1] == 32000     # both clips: the whole turn
    smart_turn.set_detector(None)
    r = c.post("/voice/api/talk", data={"csrf": csrf}, files={"audio": ("a.wav", _wav(1.0), "audio/wav")},
               headers={"Accept": "application/json"})
    assert r.json()["ok"] and r.json()["eot"] is None                         # no model: the page's timer decides


def test_activity_says_whether_the_page_may_trust_smart_turn(page):
    _c, web = page
    assert web.activity()["voice"]["smart_turn_ready"] is False
    smart_turn.set_detector(FakeDetector(0.5))
    assert web.activity()["voice"]["smart_turn_ready"] is True


def test_page_script_shortens_silence_and_holds_on_not_finished():
    js = (config.REPO_ROOT / "static" / "jarvis.js").read_text(encoding="utf-8")
    assert "smart_turn_ready) return 550" in js and "fd.append('cont', '1')" in js
    assert "function waitFor(text, eot)" in js and "smart_turn_p" in js


def test_voice_loop_timing_reaches_the_brain_over_ask(page):
    import json as _json

    from assistant import brain
    from voice import agent as vagent

    c, web = page
    sent = {}

    class Resp:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_lines(self):
            r = c.post("/voice/api/ask", data=sent["data"],
                       headers={"X-Voice-Key": web.local_key(), "Accept": "application/x-ndjson"})
            yield from r.text.splitlines()

    def post(**kw):
        sent.update(kw)
        return Resp()

    out = vagent.remote_reply("are you there", post=post, timing={"smart_turn_p": 0.88, "smart_turn_ms": 14})
    assert sent["data"]["smart_turn_p"] == "0.88" and out["status"] == "ok"
    row = brain.get_brain().timing_for(out["turn"])
    assert row["smart_turn_p"] == 0.88 and row["smart_turn_ms"] == 14
    assert _json.dumps(row)


def test_voice_agent_passes_the_verdict_with_the_request():
    from voice.agent import VoiceAgent

    calls = []

    def brain_fn(text, on_sentence=None, *, timing=None):
        calls.append((text, timing))
        return "ok"

    brain_fn.accepts_timing = True

    class Ex:
        def submit(self, fn, *a, **kw):
            from concurrent.futures import Future

            f = Future()
            f.set_result(fn(*a, **kw))
            return f

    from voice.settings import VoiceSettings

    class Ctl:
        def publish(self, *a, **k):
            pass

        def muted(self):
            return False

    class W:
        def reset(self):
            pass

    va = VoiceAgent(wake=W(), vad=None, stt=None, tts=None, player=None, brain=brain_fn, control=Ctl(),
                    settings=VoiceSettings(), executor=Ex())
    va.rec.eot = (0.91, 9)
    va._submit("hello there")
    assert calls == [("hello there", {"smart_turn_p": 0.91, "smart_turn_ms": 9})] and va.rec.eot is None
