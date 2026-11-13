"""assistant.reels: download (fake yt-dlp), real ffmpeg keyframes, fake whisper, vault note, errors."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import transcribe
from assistant import reels, tools, vault
from assistant import toolkit_settings as ts
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env", "toolkit_env")
HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
URL = "https://www.instagram.com/reel/ABC123/?igsh=xyz"


@pytest.fixture
def toolkit_env(monkeypatch):
    import os

    for k in list(os.environ):
        if k.startswith(ts.ENV_PREFIXES):
            monkeypatch.delenv(k, raising=False)
    ts.reset_toolkit_settings()
    yield
    ts.reset_toolkit_settings()
    transcribe.set_backend(None)


def make_video(path: Path, seconds: int = 6, audio: bool = True) -> None:
    """Three solid colours (two hard scene cuts) + a tone."""
    path.parent.mkdir(parents=True, exist_ok=True)
    third = seconds / 3
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"color=c=red:s=360x640:d={third}:r=10",
           "-f", "lavfi", "-i", f"color=c=blue:s=360x640:d={third}:r=10",
           "-f", "lavfi", "-i", f"testsrc=s=360x640:d={third}:r=10"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += ["-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]"]
    if audio:
        cmd += ["-map", "3:a", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True, capture_output=True)


class FakeWhisper:
    name = "fake"

    def transcribe(self, media_path):
        assert Path(media_path).suffix == ".wav"
        return {"language": "en", "segments": [
            {"start": 0.0, "end": 2.0, "text": "This agent writes its own tools.", "words": []},
            {"start": 2.0, "end": 5.0, "text": "Watch it plan, then execute.", "words": []}]}


def fake_ydl(calls, *, info=None, fail=None):
    def extract(url, opts, download):
        calls.append({"url": url, "download": download, "cookies": opts.get("cookiesfrombrowser")})
        if fail:
            r = fail(opts)
            if r:
                raise RuntimeError(r)
        base = {"title": "An AI agent that builds itself", "uploader": "someone", "duration": 6,
                "description": "caption #ai", "webpage_url": URL.split("?")[0], "extractor_key": "Instagram"}
        base.update(info or {})
        if download:
            make_video(Path(opts["paths"]["home"]) / "video.mp4")
        return base
    return extract


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_watch_end_to_end(monkeypatch):
    calls = []
    monkeypatch.setattr(reels, "ydl_extract", fake_ydl(calls))
    transcribe.set_backend(FakeWhisper())
    rep = reels.watch(URL)
    assert [c["download"] for c in calls] == [False, True]
    assert all(c["cookies"] is None for c in calls)  # cookies off by default
    assert 3 <= len(rep.frames) <= 10
    ts_ = [f["t"] for f in rep.frames]
    assert ts_ == sorted(ts_) and ts_[0] < 0.5
    # scene cuts at ~2 s and ~4 s are represented
    assert any(1.9 <= t <= 2.6 for t in ts_) and any(3.9 <= t <= 4.6 for t in ts_)
    for f in rep.frames:
        p = Path(f["path"])
        assert p.is_file() and p.read_bytes()[:2] == b"\xff\xd8"  # JPEG
    assert "writes its own tools" in rep.transcript and rep.language == "en"
    assert rep.metadata["platform"] == "instagram" and rep.metadata["description"] == "caption #ai"
    assert rep.note_path and rep.note_path.startswith("Inbox/Reels/") and "an-ai-agent-that-builds-itself" in rep.note_path
    note = vault.read_note(rep.note_path)
    assert "writes its own tools" in note.body and "## Keyframes" in note.body
    # cached: no new download
    again = reels.watch(URL.replace("igsh=xyz", "igsh=other"))
    assert again.id == rep.id and len(calls) == 2
    m = rep.for_model()
    assert m["frames"] and "Read each frame" in m["how_to_watch"]


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_no_audio_still_gives_frames(monkeypatch):
    def extract(url, opts, download):
        if download:
            make_video(Path(opts["paths"]["home"]) / "video.mp4", audio=False)
        return {"title": "silent", "duration": 6}
    monkeypatch.setattr(reels, "ydl_extract", extract)
    transcribe.set_backend(FakeWhisper())
    rep = reels.watch("https://youtube.com/shorts/xyz")
    assert rep.frames and rep.transcript == "" and rep.metadata["platform"] == "youtube"


def test_choose_frame_times_caps_and_spreads():
    t = reels.choose_frame_times(60.0, [5.0, 5.1, 20.0, 40.0], 10)
    assert len(t) == 10 and t == sorted(t) and t[0] == 0.3
    assert all(b - a >= reels.MIN_FRAME_GAP_SEC for a, b in zip(t, t[1:]))
    assert len(reels.choose_frame_times(3.0, [], 10)) <= 4
    assert reels.choose_frame_times(0, [], 10) == [0.0]


@pytest.mark.parametrize("msg,code", [
    ("ERROR: [Instagram] ABC: This content is private", "private"),
    ("ERROR: [Instagram] ABC: Requested content is not available, rate-limit reached or login required", "login_required"),
    ("ERROR: Video unavailable. This video has been removed by the uploader", "removed"),
    ("ERROR: Unsupported URL: https://example.com/", "unsupported"),
    ("weird failure", "download_failed"),
])
def test_error_classification(monkeypatch, msg, code):
    monkeypatch.setattr(reels, "ydl_extract", lambda u, o, d: (_ for _ in ()).throw(RuntimeError(msg)))
    with pytest.raises(reels.ReelError) as ei:
        reels.watch(URL)
    assert ei.value.code == code and str(ei.value)
    assert not (reels.reels_root() / reels.reel_id(URL)).exists()


def test_cookie_fallback(monkeypatch):
    monkeypatch.setenv("REELS_BROWSER_COOKIES", "chrome")
    ts.reset_toolkit_settings()
    calls = []

    def fail(opts):
        if opts.get("cookiesfrombrowser"):
            return "Could not copy Chrome cookie database. See https://github.com/yt-dlp/yt-dlp/issues/7271"
        return "This content is private"
    monkeypatch.setattr(reels, "ydl_extract", fake_ydl(calls, fail=fail))
    with pytest.raises(reels.ReelError) as ei:
        reels.watch(URL)
    assert [c["cookies"] for c in calls] == [("chrome",), None]
    assert ei.value.code == "private"


def test_bad_urls_and_long_videos(monkeypatch):
    for bad in ("notaurl", "ftp://x/y", "http://localhost/reel", "http://192.168.1.2/x"):
        with pytest.raises(reels.ReelError):
            reels.watch(bad)
    calls = []
    monkeypatch.setattr(reels, "ydl_extract", fake_ydl(calls, info={"duration": 4000}))
    with pytest.raises(reels.ReelError) as ei:
        reels.watch(URL)
    assert ei.value.code == "too_long" and [c["download"] for c in calls] == [False]


def test_tool_registered_and_errors_are_tool_errors(monkeypatch):
    tools.load_extensions()
    assert "reel_watch" in tools.REGISTRY
    monkeypatch.setattr(reels, "ydl_extract", lambda u, o, d: (_ for _ in ()).throw(RuntimeError("private video")))
    res = tools.dispatch("reel_watch", {"url": URL}, tools.ToolContext(), allowed={"reel_watch"})
    assert not res.ok and "private" in res.content


def test_canonical_ids():
    assert reels.reel_id("https://www.instagram.com/reel/A/?igsh=1") == reels.reel_id("https://instagram.com/reel/A")
    assert reels.slugify("Héllo, World!!") == "h-llo-world"
