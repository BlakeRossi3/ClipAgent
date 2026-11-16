"""assistant/handoff.py: hand work over to the next Claude usage window (fake clock, fake usage signals)."""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from assistant_fixtures import assistant_env  # noqa: F401

import db
from assistant import claude_code, handoff, schedule, store, tasks, tools, usage, vault
from assistant import settings as asettings

pytestmark = pytest.mark.usefixtures("assistant_env")


@pytest.fixture
def on(monkeypatch, clock):
    monkeypatch.setenv("ASSISTANT_ENABLED", "true")
    asettings.reset_assistant_settings()
    store.init_store()
    yield
    tasks.set_runner(None)


def _signal(util=0.93, status="allowed_warning", in_hours=2):
    resets = int((db.utcnow() + timedelta(hours=in_hours)).timestamp())
    usage.record_window({"status": status, "utilization": util, "resetsAt": resets, "rateLimitType": "five_hour"})
    return db.parse_iso(db.to_iso(db.utcnow() + timedelta(hours=in_hours)))


def _blocking_task():
    gate, started = threading.Event(), threading.Event()
    result = {"text": "Finished the refactor.", "status": "ok"}

    def runner(brief, context, on_step):
        on_step("Read assistant/usage.py")
        started.set()
        gate.wait(10)
        return result["text"], result["status"]

    tasks.set_runner(runner)
    tid = tasks.start("Refactor the usage module and run the tests")
    assert started.wait(5)
    return tid, gate, result


def test_low_usage_with_work_in_flight_writes_one_handoff_and_one_resume(on, clock):
    tid, gate, _res = _blocking_task()
    assert handoff.on_usage_signal() is None                       # plenty left: nothing happens
    reset = _signal(0.93)                                          # 93% of the 5-hour window
    pend = handoff.pending_resume()
    assert pend is not None and pend.kind == "handoff_resume"
    assert db.parse_iso(pend.run_after) == reset + usage.RESET_GRACE
    path = pend.meta["notes"][0]
    assert path.startswith("Agent/Handoffs/") and pend.prompt == f"Resume from handoff {path}"
    body = vault.read_note(path).body
    assert "Refactor the usage module" in body and "Next steps" in body and "Read assistant/usage.py" in body
    _signal(0.95)                                                  # same window: no second handoff
    assert len([d for d in store.list_deferred("queued") if d.kind == "handoff_resume"]) == 1
    assert len(vault.list_notes("Agent/Handoffs")) == 1
    gate.set()
    tasks.wait(tid)


def test_tool_handoff_joins_the_pending_resume_and_resume_runs_after_reset(on, clock):
    tools.load_extensions()
    reset = _signal(0.91)
    ctx = tools.ToolContext(channel="voice", agent="main", source="test")
    r1 = tools.dispatch("usage_handoff", {"summary": "Building the budget sheet", "next_steps": ["Add March totals"],
                                          "done": ["Jan and Feb"], "files": ["Notes/Budget.md"]}, ctx,
                        allowed={"usage_handoff"})
    assert r1.ok, r1.content
    clock.advance(minutes=1)
    r2 = tools.dispatch("usage_handoff", {"summary": "Researching flights", "next_steps": ["Compare BA and Delta"]},
                        ctx, allowed={"usage_handoff"})
    assert r2.ok
    resumes = [d for d in store.list_deferred("queued") if d.kind == "handoff_resume"]
    assert len(resumes) == 1 and len(resumes[0].meta["notes"]) == 2
    assert "Resume from handoff Agent/Handoffs/" in resumes[0].prompt and resumes[0].title == "Resume from 2 handoffs"
    snap = schedule.snapshot()
    assert snap["handoff"]["id"] == resumes[0].id and any(x["kind"] == "handoff_resume" for x in snap["upcoming"])
    with pytest.raises(ValueError):
        handoff.write("", ["x"])

    briefs = []
    tasks.set_runner(lambda brief, context: (briefs.append(brief), ("Picked it all back up.", "ok"))[1])
    assert usage.run_due() == 0                                    # before the reset: waits
    clock.set(reset + timedelta(minutes=6))
    assert usage.run_due() == 1
    for t in tasks.list_tasks():
        tasks.wait(t["id"])
    assert "Add March totals" in briefs[0] and "Compare BA and Delta" in briefs[0] and "<untrusted" in briefs[0]
    assert store.get_deferred(resumes[0].id).status == "done" and handoff.pending_resume() is None


def test_limit_hit_without_reset_time_falls_back_to_five_hours(on, clock, monkeypatch):
    monkeypatch.setattr(usage, "next_reset", lambda: None)
    out = handoff.write("Halfway through the clip audit", "Finish clips 40-60\nThen summarise", by="Cecil")
    d = store.get_deferred(out["resume_id"])
    assert db.parse_iso(d.run_after) == db.utcnow() + handoff.FALLBACK_WAIT
    assert "2. Then summarise" in vault.read_note(out["path"]).body


def test_task_stopped_by_the_limit_is_handed_over_once(on, clock):
    db.kv_set(claude_code.KV_BACKOFF, {"until": db.to_iso(db.utcnow() + timedelta(hours=1)), "reason": "limit"})
    tasks.set_runner(lambda brief, context: ("I hit the Claude plan's limit before finishing.", "budget"))
    tid = tasks.start("Summarise this week's analytics")
    tasks.wait(tid)
    pend = handoff.pending_resume()
    assert pend is not None and "analytics" in vault.read_note(pend.meta["notes"][0]).body
    assert db.parse_iso(pend.run_after) == claude_code.backoff_until() + usage.RESET_GRACE
    assert handoff.on_task_stopped(tasks.get_task(tid)) is None   # once per task
