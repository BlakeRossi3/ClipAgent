"""End-to-end, offline: a synthetic 16:9 video goes through the real pipeline into a dry-run Short.

What is real: ffmpeg audio extraction, the licence gate, windowing, post-processing, ASS
captions, the 1080x1920 render + ffprobe verification, slot scheduling, DB state machines.
What is faked: yt-dlp (``fetch.extract_info``), faster-whisper (a word-timed transcript
backend), Claude (``fake_llm``), the scout's network searcher. DRY_RUN stays on.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import budget
import db
import fetch
import main
import scout
import strategy as strategy_mod
import transcribe

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                reason="needs ffmpeg/ffprobe")

NASA_ID = "UCLA_DiR1FfKNvjuUpBHmylQ"
YT_URL = "https://www.youtube.com/watch?v=e2eNASA0001"
IMAGES_URL = "https://images-assets.nasa.gov/video/e2e-launch/e2e-launch~orig.mp4"
CSPAN_URL = "https://www.youtube.com/watch?v=e2eCSPAN001"

SENTENCES = [
    "Ten seconds to liftoff and all systems are go.",
    "The engines roar to life and the rocket clears the tower.",
    "Nobody on the team had ever seen a launch this smooth.",
    "Now the crew is heading to the Moon for the first time in fifty years.",
    "Mission control confirms the spacecraft is on the right path.",
    "That is the moment the whole program was waiting for.",
]


@pytest.fixture(scope="module")
def source_video(tmp_path_factory) -> Path:
    """30 s, 640x360 (16:9) test pattern with a speech-like warbling tone."""
    out = tmp_path_factory.mktemp("media") / "source.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=30",
         "-f", "lavfi", "-i", "sine=frequency=180:sample_rate=48000:duration=30,tremolo=f=5:d=0.8,volume=0.5",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out)],
        check=True, capture_output=True)
    return out


def _transcript() -> dict[str, Any]:
    segments, t = [], 0.4
    for text in SENTENCES:
        words, start = [], t
        for w in text.split():
            words.append({"start": round(t, 3), "end": round(t + 0.32, 3), "word": w, "probability": 0.97})
            t += 0.38
        segments.append({"start": start, "end": words[-1]["end"], "text": text, "words": words})
        t += 0.6
    return {"language": "en", "duration_sec": 30.0, "segments": segments}


class FakeWhisper:
    name = "fake-whisper"

    def __init__(self) -> None:
        self.calls: list[Path] = []

    def transcribe(self, media_path: Path) -> dict:
        assert media_path.name == "audio.wav" and media_path.stat().st_size > 44   # real ffmpeg extraction
        self.calls.append(media_path)
        return _transcript()


class FakeYtDlp:
    """``fetch.extract_info`` stand-in: metadata per URL; a "download" copies the synthetic video."""

    def __init__(self, video: Path) -> None:
        self.video = video
        self.downloads: list[str] = []
        self.infos: dict[str, dict[str, Any]] = {
            YT_URL: {"id": "e2eNASA0001", "webpage_url": YT_URL, "extractor_key": "Youtube", "channel": "NASA",
                     "channel_id": NASA_ID, "uploader": "NASA", "uploader_id": "@NASA", "duration": 30,
                     "title": "Artemis launch highlights", "description": "Launch of the Artemis mission.",
                     "upload_date": "20261101", "width": 640, "height": 360},
            IMAGES_URL: {"id": "e2e-launch~orig", "webpage_url": IMAGES_URL, "extractor_key": "Generic",
                         "title": "e2e-launch~orig", "duration": None},
            CSPAN_URL: {"id": "e2eCSPAN001", "webpage_url": CSPAN_URL, "extractor_key": "Youtube",
                        "channel": "C-SPAN", "channel_id": "UCb--64Gl51jIEVE-GLDAVTg", "uploader": "C-SPAN",
                        "duration": 1800, "title": "Senate hearing", "description": "C-SPAN coverage."},
        }

    def __call__(self, url: str, *, download: bool, outdir: Path | None = None) -> dict[str, Any]:
        info = dict(self.infos[url])
        if download:
            assert outdir is not None
            self.downloads.append(url)
            shutil.copy(self.video, outdir / "video.mp4")
        return info


@pytest.fixture
def pipeline(monkeypatch, clock, fake_llm, source_video, settings):
    """Real pipeline with the fakes above and a short-clip strategy (keeps ffmpeg renders quick)."""
    ytdlp = FakeYtDlp(source_video)
    monkeypatch.setattr(fetch, "extract_info", ytdlp)
    whisper = FakeWhisper()
    transcribe.set_backend(whisper)

    strategy_mod.record_initial_version()
    strategy_mod.save_strategy(strategy_mod.load_strategy().updated(
        clip_length_sec={"min": 8, "target": 10, "max": 12}))

    fake_llm.by_purpose["pick.score_window"] = {"score": 9, "reason": "Strong launch moment.",
                                                "hook": "Ten seconds to liftoff"}

    def finalize(req):
        assert "## Moment A" in req.user
        return {"clips": [{"start_sec": 0.3, "end_sec": 9.9, "title": "The smoothest launch they had ever seen",
                           "description": "The rocket clears the tower.", "hashtags": ["#Artemis", "#NASA"],
                           "hook_text": "Ten seconds to liftoff", "rationale": "Countdown hook, clear payoff."}]}
    fake_llm.by_purpose["pick.finalize"] = finalize
    yield {"ytdlp": ytdlp, "whisper": whisper, "llm": fake_llm}
    transcribe.set_backend(None)


def _probe(path: Path) -> dict[str, Any]:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def _assert_short(path: Path, expected_sec: float) -> None:
    info = _probe(path)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    assert (video["width"], video["height"]) == (1080, 1920) and video["codec_name"] == "h264"
    assert audio and audio[0]["codec_name"] == "aac"
    assert info["format"]["format_name"].startswith("mov,mp4")
    assert float(info["format"]["duration"]) == pytest.approx(expected_sec, abs=0.4)


def test_manual_one_url_to_one_short(pipeline, settings):
    clip_ids = main.manual(YT_URL)

    assert len(clip_ids) == 1
    src = db.get_source_by_url(YT_URL)
    assert src.status == "done" and src.source_type == "nasa"           # type inferred from the licence gate
    assert src.licence == "us_gov_public_domain" and src.licence_evidence["channel_id"] == NASA_ID
    assert Path(src.transcript_path).is_file() and Path(src.audio_path).is_file()

    clip = db.require_clip(clip_ids[0])
    assert clip.status == "uploaded"
    assert clip.licence == src.licence and clip.source_url == YT_URL     # copied for audit
    assert clip.credit_text == "Video: NASA (public domain)"
    assert clip.experiment_id == "exp-001-hook-question-vs-claim" and clip.experiment_variant == "control"
    assert 8 <= clip.duration_sec <= 12
    _assert_short(Path(clip.render_path), clip.duration_sec)
    ass = Path(clip.render_path).with_suffix(".ass").read_text()
    assert "LIFTOFF" in ass and "Video: NASA (public domain)" in ass

    [up] = db.list_uploads(clip_id=clip.id)
    assert up.dry_run and up.status == "uploaded" and up.youtube_video_id == f"dryrun-{clip.id}"
    assert up.quota_units == 0 and up.publish_at is not None

    usage = {u.purpose for u in db.list_llm_usage()}
    assert usage == {"pick.score_window", "pick.finalize"}
    assert all(u.source_id == src.id and u.fake for u in db.list_llm_usage())
    logged = {(e.module, e.message.split(":")[0]) for e in db.list_events(limit=500)}
    for module in ("fetch", "transcribe", "pick", "edit", "upload", "main"):
        assert any(m == module for m, _ in logged), module
    assert pipeline["ytdlp"].downloads == [YT_URL]

    # re-running the same URL resumes: nothing is downloaded, picked or uploaded twice
    assert main.manual(YT_URL) == clip_ids
    assert pipeline["ytdlp"].downloads == [YT_URL] and len(db.list_uploads()) == 1


def test_licence_rejected_url_never_downloads(pipeline):
    with pytest.raises(fetch.LicenceRejected):
        main.manual(CSPAN_URL, "congress")
    src = db.get_source_by_url(CSPAN_URL)
    assert src.status == "rejected" and "rebroadcaster" in src.status_reason
    assert src.licence is None and src.licence_evidence["channel"] == "C-SPAN"
    assert pipeline["ytdlp"].downloads == [] and db.list_clips() == []
    with pytest.raises(fetch.LicenceRejected):                    # stays rejected on a re-run
        main.manual(CSPAN_URL)


def _fake_nasa_searcher(query: str, limit: int, strategy: Any) -> list[scout.Candidate]:
    if query != "NASA Artemis":
        return []
    return [scout.Candidate(url=IMAGES_URL, source_type="nasa", platform="nasa_images", external_id="e2e-launch",
                            channel="NASA", channel_id=None, title="Artemis launch", duration_sec=30.0,
                            published_at="2026-11-01T00:00:00Z", topic=query,
                            metadata={"found_via": "nasa_images_api", "licence_verified": False},
                            description="Launch of the Artemis mission from Kennedy Space Center.")]


def test_run_once_scout_to_dry_run_upload_and_idempotent(pipeline, monkeypatch):
    for st in list(scout.SEARCHERS):
        monkeypatch.setitem(scout.SEARCHERS, st, lambda q, n, s: [])
    monkeypatch.setitem(scout.SEARCHERS, "nasa", _fake_nasa_searcher)

    first = main.run_once()
    assert first["scouted"] == 1 and first["processed"] == 1 and first["clips_rendered"] == 1
    assert first["uploaded"] == 1
    [src] = db.list_sources()
    assert src.url == IMAGES_URL and src.status == "done" and src.topic == "NASA Artemis"
    assert src.duration_sec == pytest.approx(30.0, abs=0.2)             # measured by ffprobe after download
    [clip] = db.list_clips()
    assert clip.status == "uploaded" and clip.source_url == IMAGES_URL and clip.licence == "us_gov_public_domain"
    _assert_short(Path(clip.render_path), clip.duration_sec)

    second = main.run_once()
    assert second["scouted"] == 0 and second.get("clips_rendered", 0) == 0 and second["uploaded"] == 0
    assert len(db.list_sources()) == 1 and len(db.list_clips()) == 1 and len(db.list_uploads()) == 1
    assert len(pipeline["whisper"].calls) == 1 and pipeline["ytdlp"].downloads == [IMAGES_URL]


def test_budget_exhausted_mid_run_keeps_source_transcribed(pipeline, settings):
    # $0.99 of the $1.00 cap is gone: the cheap Haiku window scores fit, the Sonnet finalize does not
    budget.record_usage(settings.sonnet_model, "earlier.work", input_tokens=0, output_tokens=99_000)
    sid = db.add_source(YT_URL, "nasa")
    counters = main.process_pending()
    assert counters["budget_stopped"] == 1
    src = db.require_source(sid)
    assert src.status == "transcribed" and db.list_clips() == []
    assert "pick.score_window" in pipeline["llm"].purposes() and "pick.finalize" not in pipeline["llm"].purposes()
    assert [n.kind for n in db.list_notifications("queued")] == ["budget_cap"]
    assert db.list_events(module="main", level="warning")[0].message.startswith("LLM budget exhausted")
    # the next run reuses the persisted window scores and is refused before any paid call
    calls = len(pipeline["llm"].calls)
    assert main.process_pending()["budget_stopped"] == 1
    assert len(pipeline["llm"].calls) == calls and db.require_source(sid).status == "transcribed"
    assert len(db.list_notifications()) == 1                          # the owner is told once per day
    # once the cap is fully used, transcribed sources are not even attempted
    budget.record_usage(settings.sonnet_model, "earlier.work", input_tokens=0, output_tokens=2_000)
    assert main.process_pending().get("waiting_for_budget") == 1


def test_scheduler_and_server_wiring():
    sched = main.build_scheduler()
    assert {j.id for j in sched.get_jobs()} == {"scout", "process", "upload", "send_queued", "expire_decisions",
                                                "analytics", "cleanup", "daily_summary", "weekly_review"}
    import server

    with TestClient(server.create_app()) as client:
        assert client.get("/healthz").json()["status"] == "ok"
