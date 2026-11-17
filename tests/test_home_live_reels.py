"""The live Reels panel on the Jarvis page (home_live.reels, templates/cecil_parts.html reels)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import db
import home_live
import progress
import server
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")


@pytest.fixture(autouse=True)
def _no_git():
    home_live.set_git_log(lambda n: [])
    yield
    home_live.set_git_log(None)


def test_reels_lists_work_in_flight_busiest_first(clock, make_source, make_clip):
    old_done = make_source(status="done", title="Old and done")
    clock.advance(days=2)
    waiting = make_source(status="transcribed", title="Waiting to score")
    failed = make_source(status="failed", title="Broke")
    db.set_source_status(failed, "failed", "transcription failed: TypeError", force=True)
    busy = make_source(status="transcribed", title="Being scored")
    make_clip(make_source(status="picked", title="Has clips"), status="rendered")
    progress.step("process", "score", source_id=busy, title="Being scored", done=3, total=12)
    r = home_live.reels()
    ids = [s["id"] for s in r["sources"]]
    assert ids[0] == busy and failed in ids and waiting in ids and old_done not in ids
    assert r["live"][0]["pct"] == 25 and r["clips"] == {"rendered": 1} and r["dry_run"] is True
    assert next(s for s in r["sources"] if s["id"] == failed)["reason"].startswith("transcription failed")


def test_reels_panel_renders_live_and_escaped(clock, make_source):
    evil = "<script>alert(1)</script>"
    sid = make_source(status="transcribed", title=evil)
    progress.step("process", "score", source_id=sid, title=evil, done=1, total=4)
    db.log_event("pick", "pick.score_window: structured output rejected " + evil, level="warning")
    j = TestClient(server.create_app(), base_url="http://testserver").get("/dashboard/api/cecil").json()
    html = j["html"]["reels"]
    assert "Scoring moments" in html and "1/4" in html and "dry run" in html and "structured output rejected" in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    page = TestClient(server.create_app(), base_url="http://testserver").get("/dashboard/").text
    assert 'data-cc-live="reels"' in page


def test_reels_idle_says_when_it_last_ran(clock):
    progress.start_run("process")
    progress.end_run("process", {"processed": 2, "failed": 0})
    html = TestClient(server.create_app(), base_url="http://testserver").get("/dashboard/api/cecil").json()["html"]["reels"]
    assert "Not running right now" in html and "processed 2" in html and "failed 0" not in html


def test_reel_clips_show_working_retrying_failed_and_done(clock, make_source, make_clip):
    sid = make_source(status="picked", title="Big source")
    rendering = make_clip(sid, title="Being rendered")
    retrying = make_clip(sid, title="Will retry")
    db.set_clip_status(retrying, "failed", "ffmpeg exited 1", force=True)
    dead = make_clip(sid, title="Gave up")
    db.set_clip_status(dead, "failed", "ffmpeg exited 1", force=True)
    db.kv_set(f"main:clip_retries:{dead}", 2)
    waiting = make_clip(sid, status="rendered", title="Ready to post")
    posted = make_clip(sid, status="uploaded", title="Posted one")
    progress.step("process", "render", source_id=sid, clip_id=rendering, title="Being rendered", done=0, total=3)
    by_id = {c["id"]: c for c in home_live.reels()["reel_clips"]}
    assert by_id[rendering]["state"] == "working" and by_id[rendering]["label"] == "Rendering"
    assert by_id[retrying]["state"] == "retrying" and by_id[retrying]["attempt"] == "0/2"
    assert by_id[dead]["state"] == "failed" and by_id[dead]["reason"] == "ffmpeg exited 1"
    assert by_id[waiting]["state"] == "queued" and by_id[waiting]["label"] == "Waiting to upload"
    assert by_id[posted]["state"] == "done"
    order = [c["id"] for c in home_live.reels()["reel_clips"]]
    assert order[0] == rendering and order[-1] == posted


def test_reel_clips_drop_old_finished_ones_and_render_live(clock, make_source, make_clip):
    sid = make_source(status="picked", title="Src")
    old = make_clip(sid, status="uploaded", title="Long ago")
    clock.advance(days=2)
    up = make_clip(sid, status="rendered", title="<b>Now uploading</b>")
    progress.step("upload", "upload", source_id=sid, clip_id=up, title="x", done=0, total=1)
    ids = [c["id"] for c in home_live.reels()["reel_clips"]]
    assert up in ids and old not in ids
    html = TestClient(server.create_app(), base_url="http://testserver").get("/dashboard/api/cecil").json()["html"]["reels"]
    assert "cc-reel-clips-list" in html and "Uploading" in html and "&lt;b&gt;Now uploading" in html


def test_retried_source_is_flagged(clock, make_source):
    sid = make_source(status="failed", title="Flaky")
    db.set_source_status(sid, "fetched", "retry 1/2 after: network down", force=True)
    row = next(s for s in home_live.reels()["sources"] if s["id"] == sid)
    assert row["retrying"] is True
