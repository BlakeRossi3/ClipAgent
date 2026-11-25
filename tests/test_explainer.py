"""explainer.py: whiteboard explainer Shorts drawn in code (backlog #37)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

import db
import explainer as ex

SCRIPT = [
    {"title": "Why is the sky blue?", "lines": [], "doodle": "circle", "say": "Why is the sky blue?"},
    {"title": "Air scatters blue", "lines": ["short waves bounce", "everywhere"], "doodle": "arrow", "say": "x"},
    {"title": "Sunsets", "lines": ["red survives"], "doodle": "check", "say": ""},
]


def test_validate_script_flags_bad_scenes():
    assert ex.validate_script(SCRIPT) == []
    assert ex.validate_script([]) == ["script must be a non-empty list of scenes"]
    errs = ex.validate_script([{"title": ""}, {"title": "a", "doodle": "spiral"}, "x"])
    assert len(errs) == 3 and "no title" in errs[0] and "doodle" in errs[1]
    with pytest.raises(ex.ExplainerError):
        ex.plan([{"title": ""}])


def test_plan_is_contiguous_and_writes_in_order():
    scenes = ex.plan(SCRIPT)
    assert scenes[0].start == 0
    for a, b in zip(scenes, scenes[1:]):
        assert b.start == pytest.approx(a.end)
    for s in scenes:
        starts = [e.start for e in s.elements]
        assert starts == sorted(starts)
        assert all(e.end > e.start for e in s.elements)
        assert s.elements[-1].end <= s.erase_start < s.end
    assert [e.kind for e in scenes[1].elements] == ["title", "line", "line", "doodle"]
    assert ex.total_duration(scenes) == scenes[-1].end


def test_plan_waits_for_narration():
    short = ex.plan(SCRIPT)
    long = ex.plan(SCRIPT, [10.0, 0.0, 0.0])
    assert long[0].erase_start >= 10.0 > short[0].erase_start
    assert long[1].start - short[1].start == pytest.approx(long[0].end - short[0].end)


def test_wrap_respects_width():
    font = ex.ImageFont.load_default(20)
    lines = ex.wrap("the quick brown fox jumps over the lazy dog again", font, 120)
    assert len(lines) > 1 and all(font.getlength(l) <= 120 or " " not in l for l in lines)
    assert " ".join(lines) == "the quick brown fox jumps over the lazy dog again"


def test_frames_animate_writing_and_erase():
    board = ex.Board(216, 384)
    scenes = ex.plan(SCRIPT)
    title = scenes[0].elements[0]
    blank = np.asarray(ex.render_frame(board, scenes, 0.0), dtype=int)
    mid = np.asarray(ex.render_frame(board, scenes, (title.start + title.end) / 2), dtype=int)
    done = np.asarray(ex.render_frame(board, scenes, scenes[0].erase_start - 0.01), dtype=int)
    assert ex.render_frame(board, scenes, 0.0).size == (216, 384)
    assert np.abs(mid - blank).sum() > 0
    assert np.abs(done - blank).sum() > np.abs(mid - blank).sum() * 0.5
    # every doodle kind produces a path
    for kind in ("circle", "underline", "arrow", "check"):
        assert len(board.doodle_points(scenes[1], kind)) >= 3


def test_mix_track_places_narration_at_scene_start():
    scenes = ex.plan(SCRIPT, [1.0, 1.0, 0.0])
    clips = [np.full(100, 0.5, np.float32), np.full(100, 0.25, np.float32), None]
    track = ex.mix_track(scenes, clips, 1000)
    a = int((scenes[1].start + 0.15) * 1000)
    assert track[a] == pytest.approx(0.25) and track[150] == pytest.approx(0.5)
    assert len(track) >= ex.total_duration(scenes) * 1000


def test_ffmpeg_cmd_is_vertical_with_audio(tmp_path: Path):
    cmd = ex.ffmpeg_cmd("ffmpeg", tmp_path / "o.mp4", tmp_path / "n.wav")
    assert cmd[cmd.index("-s") + 1] == "1080x1920" and "-shortest" in cmd and cmd[-1].endswith("o.mp4")


def test_render_streams_every_frame_and_logs(tmp_path: Path):
    got = {}

    def runner(cmd, images):
        got["cmd"] = cmd
        got["n"] = sum(1 for _ in images)
        assert Path(cmd[cmd.index("-i", cmd.index("-i") + 1) + 1]).exists()   # narration wav written

    def tts(text):
        return np.zeros(2400, np.float32), 24000

    res = ex.render(SCRIPT[:1], tmp_path / "e.mp4", tts=tts, runner=runner, width=108, height=192, fps=5)
    assert res["narrated"] is True and res["scenes"] == 1
    assert got["n"] == int(np.ceil(res["duration_sec"] * 5)) or abs(got["n"] - res["duration_sec"] * 5) <= 1
    assert any(e.module == "explainer" for e in db.list_events(limit=5))


def test_render_without_voice_is_silent(tmp_path: Path):
    res = ex.render(SCRIPT[:1], tmp_path / "e.mp4", tts=None, runner=lambda c, i: None, width=108, height=192)
    assert res["narrated"] is False


# ---------------------------------------------------------------------------
# second channel (backlog #45): explainers go to their own YouTube account, never Daily Orbit
# ---------------------------------------------------------------------------

import json  # noqa: E402

import youtube_auth  # noqa: E402


def test_channel_token_path_is_a_sibling_file(settings):
    base = youtube_auth.token_path()
    assert youtube_auth.token_path(channel=None) == base
    assert youtube_auth.token_path(channel="lemo") == base.with_name("youtube_token.lemo.json")
    for bad in ("../x", "a b", "", "LEMO/1"):
        with pytest.raises(ValueError):
            youtube_auth.token_path(channel=bad)


def test_missing_second_channel_token_does_not_page_about_main_channel(clock, settings):
    with pytest.raises(youtube_auth.YouTubeAuthError, match="--channel lemo"):
        youtube_auth.load_credentials(channel="lemo")
    assert db.list_notifications() == []          # Daily Orbit is fine; no call about it


def test_channel_token_loads_independently(clock, settings):
    tok = youtube_auth.token_path(channel="lemo")
    youtube_auth.save_credentials(type("C", (), {"to_json": lambda self: json.dumps({
        "refresh_token": "r", "client_id": "c", "client_secret": "s", "token": "t",
        "scopes": list(youtube_auth.SCOPES)})})(), tok)
    assert youtube_auth.has_token(channel="lemo") and not youtube_auth.has_token()
    creds = youtube_auth.load_credentials(channel="lemo", refresh=False)
    assert creds.refresh_token == "r"


class _FakeInsert:
    def __init__(self, calls):
        self.calls = calls

    def insert(self, **kw):
        self.calls.append(kw)
        return type("R", (), {"execute": lambda self: {"id": "vid123", "status": {"privacyStatus": "private"}}})()


class _FakeYT:
    def __init__(self):
        self.calls: list[dict] = []

    def videos(self):
        return _FakeInsert(self.calls)


def test_upload_goes_private_to_named_channel(clock, settings, tmp_path, monkeypatch):
    mp4 = tmp_path / "e.mp4"
    mp4.write_bytes(b"\0" * 10)
    yt = _FakeYT()
    seen: list = []
    monkeypatch.setattr(ex, "_channel_service", lambda channel: seen.append(channel) or yt)
    before = youtube_auth.quota_used()
    res = ex.upload(mp4, title="Why is the sky blue? " + "x" * 120, description="d", tags=["science"])
    assert seen == [ex.EXPLAINER_CHANNEL] and ex.EXPLAINER_CHANNEL
    assert res == {"video_id": "vid123", "channel": ex.EXPLAINER_CHANNEL, "privacy": "private",
                   "url": "https://youtube.com/shorts/vid123"}
    body = yt.calls[0]["body"]
    assert body["status"]["privacyStatus"] == "private"
    assert len(body["snippet"]["title"]) <= 100 and "#Shorts" in body["snippet"]["description"]
    assert youtube_auth.quota_used() - before == youtube_auth.QUOTA_COSTS["videos.insert"]


def test_upload_refuses_main_channel_and_missing_file(settings, tmp_path):
    mp4 = tmp_path / "e.mp4"
    mp4.write_bytes(b"\0")
    with pytest.raises(ex.ExplainerError, match="Daily Orbit"):
        ex.upload(mp4, title="t", channel="")
    with pytest.raises(ex.ExplainerError, match="not found"):
        ex.upload(tmp_path / "nope.mp4", title="t")
    with pytest.raises(ex.ExplainerError, match="privacy"):
        ex.upload(mp4, title="t", privacy="public")
