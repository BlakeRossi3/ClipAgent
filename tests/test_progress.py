"""Live pipeline progress for the dashboard's Reels panel (progress.py)."""

from __future__ import annotations

import db
import progress


def _lane(name: str) -> dict:
    return next(x for x in progress.lanes() if x["lane"] == name)


def test_lanes_start_idle(clock):
    assert [x["state"] for x in progress.lanes()] == ["idle", "idle"]


def test_run_steps_and_finish(clock):
    progress.start_run("process")
    assert _lane("process")["state"] == "running" and _lane("process")["label"] == "Starting"
    progress.step("process", "score", source_id=3, title="Webb briefing", done=10, total=40)
    x = _lane("process")
    assert (x["state"], x["label"], x["source_id"], x["pct"], x["title"]) == ("running", "Scoring moments", 3, 25, "Webb briefing")
    clock.advance(minutes=2)
    progress.end_run("process", {"processed": 2, "failed": 0, "junk": [1]})
    x = _lane("process")
    assert x["state"] == "finished" and x["summary"] == {"processed": 2, "failed": 0} and x["error"] == ""


def test_a_silent_run_turns_quiet(clock):
    progress.step("upload", "upload", clip_id=7, done=0, total=1)     # implicit start
    clock.advance(minutes=progress.QUIET_AFTER.total_seconds() / 60 + 1)
    x = _lane("upload")
    assert x["state"] == "quiet" and x["quiet_min"] >= 10


def test_progress_never_breaks_the_pipeline(clock, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(db, "kv_set", boom)
    monkeypatch.setattr(db, "kv_get", boom)
    progress.start_run("process")
    progress.step("process", "fetch", source_id=1)
    progress.end_run("process", {"processed": 1})
    assert [x["state"] for x in progress.lanes()] == ["idle", "idle"]
