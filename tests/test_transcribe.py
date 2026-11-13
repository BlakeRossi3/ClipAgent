"""Tests for transcribe.py (fake backend; one real-ffmpeg audio extraction test)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import types
import wave
from pathlib import Path
from typing import Any

import pytest

import db
import transcribe
from transcribe import Backend, FasterWhisperBackend, TranscribeError, load_transcript, words_between

FFMPEG = shutil.which("ffmpeg")


class FakeBackend:
    def __init__(self, result: dict[str, Any] | None = None, error: Exception | None = None, name: str = "fake:tiny"):
        self.name = name
        self.result = result if result is not None else sample_raw()
        self.error = error
        self.calls: list[Path] = []

    def transcribe(self, media_path: Path) -> dict:
        self.calls.append(media_path)
        if self.error:
            raise self.error
        return self.result


def sample_raw() -> dict[str, Any]:
    return {
        "language": "en",
        "duration_sec": 12.34567,
        "segments": [
            {"start": 0.0, "end": 4.2001, "text": " Hello and welcome. ",
             "words": [{"start": 0.0, "end": 0.42, "word": " Hello", "probability": 0.98123},
                       {"start": 0.5, "end": 0.7, "word": " and", "prob": 0.9},
                       {"start": 0.75, "end": 1.3, "word": " welcome.", "prob": 0.95}]},
            {"start": 5.0, "end": 8.0, "text": "We have liftoff!",
             "words": [{"start": 5.0, "end": 5.3, "word": "We", "prob": 0.99},
                       {"start": 5.3, "end": 5.6, "word": " have", "prob": 0.99},
                       {"start": 5.6, "end": 6.4, "word": " liftoff!", "prob": 0.97}]},
        ],
    }


@pytest.fixture(autouse=True)
def _reset_backend():
    yield
    transcribe.set_backend(None)


@pytest.fixture
def backend():
    fake = FakeBackend()
    transcribe.set_backend(fake)
    yield fake
    transcribe.set_backend(None)


@pytest.fixture
def fake_extract(monkeypatch):
    calls: list[tuple[Path, Path]] = []

    def _extract(media: Path, out: Path) -> Path:
        calls.append((media, out))
        out.write_bytes(b"RIFF" + b"\x00" * 100)
        return out

    monkeypatch.setattr(transcribe, "extract_audio", _extract)
    return calls


@pytest.fixture
def fetched_source(make_source, settings):
    def _make(**kw: Any) -> int:
        sid = make_source(status="fetched", **kw)
        d = settings.downloads_dir / str(sid)
        d.mkdir(parents=True, exist_ok=True)
        video = d / "video.mp4"
        video.write_bytes(b"fake video")
        db.update_source(sid, video_path=str(video.resolve()))
        return sid
    return _make


# ---------------------------------------------------------------------------
# transcribe_source
# ---------------------------------------------------------------------------

class TestTranscribeSource:
    def test_happy_path_writes_contract_shape(self, fetched_source, backend, fake_extract, settings, clock):
        sid = fetched_source()
        path = transcribe.transcribe_source(sid)

        assert path == (settings.transcripts_dir / f"{sid}.json").resolve()
        data = json.loads(path.read_text(encoding="utf-8"))
        video = (settings.downloads_dir / str(sid) / "video.mp4").resolve()
        assert data["source_id"] == sid
        assert data["media_path"] == str(video)
        assert data["backend"] == "fake:tiny"
        assert data["language"] == "en"
        assert data["duration_sec"] == 12.346
        assert data["created_at"] == "2026-11-13T15:00:00Z"
        assert [s["id"] for s in data["segments"]] == [0, 1]
        seg0 = data["segments"][0]
        assert seg0 == {"id": 0, "start": 0.0, "end": 4.2, "text": "Hello and welcome.",
                        "words": [{"start": 0.0, "end": 0.42, "word": "Hello", "prob": 0.981},
                                  {"start": 0.5, "end": 0.7, "word": "and", "prob": 0.9},
                                  {"start": 0.75, "end": 1.3, "word": "welcome.", "prob": 0.95}]}

        # audio extracted from the video to downloads/<id>/audio.wav, backend fed the wav
        assert fake_extract == [(video, video.parent / "audio.wav")]
        assert backend.calls == [video.parent / "audio.wav"]

        src = db.require_source(sid)
        assert src.status == "transcribed"
        assert src.transcript_path == str(path)
        assert src.audio_path == str((video.parent / "audio.wav").resolve())
        assert any("transcribed source" in e.message for e in db.list_events(module="transcribe"))

    def test_load_transcript_roundtrip(self, fetched_source, backend, fake_extract):
        sid = fetched_source()
        t = load_transcript(transcribe.transcribe_source(sid))
        assert t["segments"][1]["words"][-1]["word"] == "liftoff!"
        assert load_transcript(str(transcribe.transcript_path_for(sid)))["source_id"] == sid

    def test_empty_transcript(self, fetched_source, fake_extract):
        transcribe.set_backend(FakeBackend({"language": "en", "segments": []}))
        try:
            sid = fetched_source(duration_sec=321.0)
            data = load_transcript(transcribe.transcribe_source(sid))
        finally:
            transcribe.set_backend(None)
        assert data["segments"] == [] and data["duration_sec"] == 321.0
        assert db.require_source(sid).status == "transcribed"
        warn = db.list_events(module="transcribe", level="warning")
        assert warn and "no speech" in warn[0].message

    def test_idempotent_when_already_transcribed(self, fetched_source, backend, fake_extract):
        sid = fetched_source()
        p1 = transcribe.transcribe_source(sid)
        p2 = transcribe.transcribe_source(sid)
        assert p1 == p2 and len(backend.calls) == 1

    def test_transcribed_but_file_missing(self, fetched_source, backend, fake_extract):
        sid = fetched_source()
        p = transcribe.transcribe_source(sid)
        p.unlink()
        with pytest.raises(TranscribeError, match="missing"):
            transcribe.transcribe_source(sid)
        assert db.require_source(sid).status == "transcribed"

    @pytest.mark.parametrize("status", ["candidate", "rejected", "failed"])
    def test_wrong_status_raises_without_change(self, make_source, backend, fake_extract, status):
        sid = make_source(status=status)
        with pytest.raises(TranscribeError, match="only fetched"):
            transcribe.transcribe_source(sid)
        assert db.require_source(sid).status == status
        assert backend.calls == [] and fake_extract == []

    def test_missing_video_marks_failed(self, make_source, backend, fake_extract):
        sid = make_source(status="fetched")
        db.update_source(sid, video_path="/nonexistent/video.mp4")
        with pytest.raises(TranscribeError, match="video file missing"):
            transcribe.transcribe_source(sid)
        src = db.require_source(sid)
        assert src.status == "failed" and "video file missing" in src.status_reason
        assert db.list_events(module="transcribe", level="error")

    def test_backend_error_marks_failed(self, fetched_source, fake_extract):
        transcribe.set_backend(FakeBackend(error=RuntimeError("CUDA out of memory")))
        try:
            sid = fetched_source()
            with pytest.raises(TranscribeError, match="CUDA out of memory"):
                transcribe.transcribe_source(sid)
        finally:
            transcribe.set_backend(None)
        src = db.require_source(sid)
        assert src.status == "failed" and "CUDA out of memory" in src.status_reason
        assert not transcribe.transcript_path_for(sid).exists()

    def test_backend_returning_garbage_marks_failed(self, fetched_source, fake_extract):
        transcribe.set_backend(FakeBackend(result="not a dict"))  # type: ignore[arg-type]
        try:
            sid = fetched_source()
            with pytest.raises(TranscribeError):
                transcribe.transcribe_source(sid)
        finally:
            transcribe.set_backend(None)
        assert db.require_source(sid).status == "failed"

    def test_audio_extraction_error_marks_failed(self, fetched_source, backend, monkeypatch):
        def boom(media: Path, out: Path) -> Path:
            raise TranscribeError("audio extraction failed: media has no audio stream")

        monkeypatch.setattr(transcribe, "extract_audio", boom)
        sid = fetched_source()
        with pytest.raises(TranscribeError, match="no audio stream"):
            transcribe.transcribe_source(sid)
        assert db.require_source(sid).status == "failed"
        assert backend.calls == []

    def test_existing_audio_reused(self, fetched_source, backend, fake_extract, settings):
        sid = fetched_source()
        (settings.downloads_dir / str(sid) / "audio.wav").write_bytes(b"RIFF" + b"\x01" * 200)
        transcribe.transcribe_source(sid)
        assert fake_extract == [] and len(backend.calls) == 1

    def test_retry_after_failure(self, fetched_source, fake_extract):
        transcribe.set_backend(FakeBackend(error=RuntimeError("flaky")))
        sid = fetched_source()
        with pytest.raises(TranscribeError):
            transcribe.transcribe_source(sid)
        db.set_source_status(sid, "fetched")  # failed -> fetched (retry a stage)
        transcribe.set_backend(FakeBackend())
        try:
            transcribe.transcribe_source(sid)
        finally:
            transcribe.set_backend(None)
        assert db.require_source(sid).status == "transcribed"

    def test_duration_falls_back_to_last_segment(self, fetched_source, fake_extract):
        raw = sample_raw()
        raw.pop("duration_sec")
        transcribe.set_backend(FakeBackend(raw))
        try:
            sid = fetched_source(duration_sec=None)
            data = load_transcript(transcribe.transcribe_source(sid))
        finally:
            transcribe.set_backend(None)
        assert data["duration_sec"] == 8.0


# ---------------------------------------------------------------------------
# normalisation & word queries
# ---------------------------------------------------------------------------

class TestNormalize:
    def test_drops_empty_words_and_segments_and_renumbers(self):
        segs = transcribe.normalize_segments([
            {"start": 3.0, "end": 4.0, "text": "  ", "words": []},
            {"start": 5.0, "end": 6.0, "text": "", "words": [{"start": 5.1, "end": 5.4, "word": " Go", "prob": 1}]},
            {"start": 1.0, "end": 2.0, "text": "First", "words": [{"start": 1.0, "end": 1.5, "word": "  ", "prob": 1},
                                                                  {"start": 1.0, "end": 1.5, "word": "First"}]},
        ])
        assert [s["text"] for s in segs] == ["First", "Go"]
        assert [s["id"] for s in segs] == [0, 1]
        assert segs[0]["words"] == [{"start": 1.0, "end": 1.5, "word": "First", "prob": None}]

    def test_clamps_inverted_and_negative_times(self):
        segs = transcribe.normalize_segments([
            {"start": -0.01, "end": 1.0, "text": "x", "words": [{"start": 0.9, "end": 0.8, "word": "x", "prob": 0.5}]},
        ])
        assert segs[0]["start"] == 0.0
        assert segs[0]["words"][0]["end"] == segs[0]["words"][0]["start"] == 0.9

    def test_accepts_attribute_objects(self):
        W = types.SimpleNamespace
        segs = transcribe.normalize_segments([W(start=0.0, end=1.0, text="hi", words=[
            W(start=0.0, end=0.5, word=" hi", probability=0.87654)])])
        assert segs[0]["words"][0] == {"start": 0.0, "end": 0.5, "word": "hi", "prob": 0.877}


class TestWordsBetween:
    @pytest.fixture
    def t(self) -> dict:
        return {"segments": transcribe.normalize_segments(sample_raw()["segments"])}

    def test_fully_inside(self, t):
        assert [w["word"] for w in words_between(t, 0.0, 1.3)] == ["Hello", "and", "welcome."]

    def test_straddling_words_excluded(self, t):
        # "Hello" [0, 0.42] straddles 0.2; "liftoff!" [5.6, 6.4] straddles 6.0
        assert [w["word"] for w in words_between(t, 0.2, 6.0)] == ["and", "welcome.", "We", "have"]

    def test_across_segments_in_order(self, t):
        assert [w["word"] for w in words_between(t, 0, 100)] == \
            ["Hello", "and", "welcome.", "We", "have", "liftoff!"]

    def test_float_noise_at_boundaries(self, t):
        assert [w["word"] for w in words_between(t, 5.0000001, 5.2999999)] == ["We"]

    def test_empty_and_inverted_ranges(self, t):
        assert words_between(t, 2.0, 4.9) == []
        assert words_between(t, 5.0, 1.0) == []
        assert words_between({"segments": []}, 0, 10) == []


class TestLoadTranscript:
    def test_missing_file(self, tmp_path):
        with pytest.raises(TranscribeError):
            load_transcript(tmp_path / "nope.json")

    def test_malformed(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not json")
        with pytest.raises(TranscribeError):
            load_transcript(p)
        p.write_text('{"segments": "nope"}')
        with pytest.raises(TranscribeError):
            load_transcript(p)


# ---------------------------------------------------------------------------
# backends
# ---------------------------------------------------------------------------

class TestBackends:
    def test_fake_satisfies_protocol(self):
        assert isinstance(FakeBackend(), Backend)
        assert isinstance(FasterWhisperBackend(), Backend)

    def test_default_backend_from_settings_is_lazy(self, settings, monkeypatch):
        transcribe.set_backend(None)
        monkeypatch.delitem(sys.modules, "faster_whisper", raising=False)
        b = transcribe.get_backend()
        assert isinstance(b, FasterWhisperBackend)
        assert b.name == f"faster-whisper:{settings.whisper_model}"
        assert (b.device, b.compute_type) == (settings.whisper_device, settings.whisper_compute_type)
        assert b.language == "en"  # *.en model
        assert transcribe.get_backend() is b  # cached
        assert "faster_whisper" not in sys.modules  # no model built yet

    def test_default_backend_rebuilt_when_settings_change(self, monkeypatch):
        import config

        transcribe.set_backend(None)
        first = transcribe.get_backend()
        monkeypatch.setenv("WHISPER_MODEL", "medium")
        config.reset_settings()
        second = transcribe.get_backend()
        assert second is not first and second.name == "faster-whisper:medium"
        assert second.language is None  # multilingual model -> auto-detect

    def test_set_backend_overrides(self):
        fake = FakeBackend()
        transcribe.set_backend(fake)
        try:
            assert transcribe.get_backend() is fake
        finally:
            transcribe.set_backend(None)
        assert transcribe.get_backend() is not fake

    def test_faster_whisper_backend_with_stub_module(self, monkeypatch, tmp_path):
        """Exercises the faster-whisper adapter against a stub WhisperModel (no model download)."""
        created: dict[str, Any] = {}
        W = types.SimpleNamespace

        class StubModel:
            def __init__(self, size, device, compute_type):
                created["init"] = (size, device, compute_type)

            def transcribe(self, path, **kw):
                created["kw"] = kw
                created["path"] = path

                def gen():
                    yield W(start=0.0, end=1.0, text=" Hi there.",
                            words=[W(start=0.0, end=0.4, word=" Hi", probability=0.9),
                                   W(start=0.4, end=1.0, word=" there.", probability=0.8)])
                return gen(), W(language="en", duration=1.5)

        monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=StubModel))
        b = FasterWhisperBackend("tiny.en", device="cpu", compute_type="int8")
        assert "init" not in created  # lazy
        raw = b.transcribe(tmp_path / "a.wav")
        assert created["init"] == ("tiny.en", "cpu", "int8")
        assert created["kw"]["word_timestamps"] is True and created["kw"]["vad_filter"] is True
        assert created["kw"]["language"] == "en"
        assert created["path"] == str(tmp_path / "a.wav")
        assert raw["language"] == "en" and raw["duration_sec"] == 1.5
        segs = transcribe.normalize_segments(raw["segments"])
        assert [w["word"] for w in segs[0]["words"]] == ["Hi", "there."]
        b.transcribe(tmp_path / "b.wav")  # model reused
        assert created["path"].endswith("b.wav")

    def test_import_does_not_load_faster_whisper(self):
        code = "import sys, transcribe; assert 'faster_whisper' not in sys.modules"
        subprocess.run([sys.executable, "-c", code], check=True, cwd=Path(transcribe.__file__).parent)


# ---------------------------------------------------------------------------
# real ffmpeg audio extraction (skipped when ffmpeg is unavailable)
# ---------------------------------------------------------------------------

def _synth(path: Path, *, audio: bool = True) -> None:
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10:duration=2"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=2", "-ac", "2"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest"]
    if audio:
        cmd += ["-c:a", "aac"]
    cmd.append(str(path))
    subprocess.run(cmd, check=True, capture_output=True)


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")
class TestRealAudioExtraction:
    def test_extracts_16k_mono_wav(self, tmp_path):
        video = tmp_path / "video.mp4"
        _synth(video)
        out = transcribe.extract_audio(video, tmp_path / "audio.wav")
        with wave.open(str(out)) as w:
            assert w.getnchannels() == 1
            assert w.getframerate() == 16_000
            assert w.getsampwidth() == 2
            assert 1.8 <= w.getnframes() / w.getframerate() <= 2.2
        assert not (tmp_path / "audio.partial.wav").exists()

    def test_no_audio_stream(self, tmp_path):
        video = tmp_path / "silent.mp4"
        _synth(video, audio=False)
        with pytest.raises(TranscribeError, match="no audio stream"):
            transcribe.extract_audio(video, tmp_path / "audio.wav")
        assert not (tmp_path / "audio.wav").exists()

    def test_end_to_end_with_real_ffmpeg_and_fake_backend(self, make_source, settings, backend):
        sid = make_source(status="fetched")
        d = settings.downloads_dir / str(sid)
        d.mkdir(parents=True)
        _synth(d / "video.mp4")
        db.update_source(sid, video_path=str((d / "video.mp4").resolve()))
        transcribe.transcribe_source(sid)
        assert backend.calls == [d.resolve() / "audio.wav"]
        with wave.open(str(backend.calls[0])) as w:
            assert w.getframerate() == 16_000

    def test_missing_ffmpeg_binary(self, tmp_path, monkeypatch):
        import config

        monkeypatch.setenv("FFMPEG_BIN", str(tmp_path / "no-such-ffmpeg"))
        config.reset_settings()
        with pytest.raises(TranscribeError, match="ffmpeg not found"):
            transcribe.extract_audio(tmp_path / "v.mp4", tmp_path / "a.wav")
