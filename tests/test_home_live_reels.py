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
