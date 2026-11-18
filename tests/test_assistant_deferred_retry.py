"""Deferred work and background tasks: failures explain themselves, retry by themselves, run in parallel,
and start the moment plan usage frees up (Blake: "too many things are failing and then not retrying").

Covers assistant/usage.py (the deferred queue), assistant/tasks.py (retry engine) and their seam."""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

import db
from assistant import runtime, schedule, store, tasks, usage
from assistant import settings as asettings
from assistant_fixtures import assistant_env, fake_model  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")


@pytest.fixture
def on(monkeypatch, clock):
    monkeypatch.setenv("ASSISTANT_ENABLED", "true")
    asettings.reset_assistant_settings()
    yield
    tasks.set_runner(None)


def _boom(msg="Not logged in. Please run /login"):
    def step(req):
        raise RuntimeError(msg)
    return step


def _memory_pass() -> int:
    return usage.defer("Background memory pass (not from him directly). Remember things.", kind="auto_memory",
                       at=db.utcnow())


def test_a_failing_pass_says_why_and_retries_with_backoff_on_the_same_row(on, clock, fake_model):
    did = _memory_pass()
    fake_model.queue(_boom())
    assert usage.run_due() == 1
    d = store.get_deferred(did)
    assert d.status == "queued" and d.meta["attempts"] == 1
    assert "Not logged in" in d.meta["last_error"]                       # the real reason, not "unknown"
    row = tasks.get_task(d.meta["track_id"])
    assert row["status"] == "retrying" and "Not logged in" in row["last_error"]
    assert row["task"] == "Memory pass" and row["meta"]["deferred_id"] == did
    assert db.parse_iso(row["next_try"]) == db.utcnow() + timedelta(minutes=5)
    assert usage.run_due() == 0                                          # not due yet
    clock.advance(minutes=6)
    fake_model.queue("Remembered two things.")
    assert usage.run_due() == 1
    d = store.get_deferred(did)
    assert d.status == "done"
    row = tasks.get_task(d.meta["track_id"])
    assert row["status"] == "done" and row["attempts"] == 2 and not row["last_error"]
    assert len([t for t in tasks.list_tasks() if (t.get("meta") or {}).get("deferred_id") == did]) == 1


def test_a_pass_that_keeps_failing_stops_but_his_requests_never_do(on, clock, fake_model):
    did = _memory_pass()
    for _ in range(usage.MAX_ATTEMPTS):
        fake_model.queue(_boom("model unavailable"))
        usage.retry_deferred(did)
    d = store.get_deferred(did)
    assert d.status == "failed" and "Gave up after 6 attempts: " in d.result and "model unavailable" in d.result
    req = usage.defer("book the dentist", kind="owner_request", channel="dashboard", at=db.utcnow())
    for _ in range(usage.MAX_ATTEMPTS + 2):
        fake_model.queue(_boom())
        usage.retry_deferred(req)
    assert store.get_deferred(req).status == "queued"                      # still going


def test_deferred_rows_run_in_parallel(on, fake_model, monkeypatch):
    started = []
    monkeypatch.setattr(usage, "_spawn", lambda fn: started.append(fn))   # capture instead of running
    ids = [_memory_pass() for _ in range(6)]
    assert usage.run_due() == usage.MAX_PARALLEL == 4                       # four at once, each in its own thread
    assert [store.get_deferred(i).status for i in ids].count("running") == 4
    assert usage.run_due() == 0                                            # slots full until they finish


def test_plan_limit_waits_for_the_reset_and_freed_usage_starts_it_at_once(on, clock, fake_model, monkeypatch):
    did = _memory_pass()

    def limited(req):
        raise runtime.AssistantBudgetExceeded("limit", "Claude plan limit", when="4pm")

    fake_model.queue(limited)
    usage.run_due()
    d = store.get_deferred(did)
    assert d.status == "queued" and d.after_reset and d.meta["last_status"] == "budget"
    tight = {"v": True}
    monkeypatch.setattr(usage, "constrained", lambda threshold=0.8: (tight["v"], None))
    usage._check_freed()                                                   # still limited: nothing starts
    assert store.get_deferred(did).status == "queued"
    tight["v"] = False
    fake_model.queue("Done now.")
    usage._check_freed()                                                   # usage is back: it runs right away
    assert store.get_deferred(did).status == "done"
    assert any("starting waiting work" in e.message for e in db.list_events(limit=20))


def test_retry_asked_for_on_a_deferred_row_runs_it_now_even_while_another_task_runs(on, fake_model):
    did = _memory_pass()
    fake_model.queue(_boom())
    usage.run_due()
    tid = store.get_deferred(did).meta["track_id"]
    gate = threading.Event()
    tasks.set_runner(lambda brief, ctx: (gate.wait(5), ("surprise made", "ok"))[1])
    busy = tasks.start("Morning surprise", announce=False)               # a long task is running
    fake_model.queue("Remembered.")
    tasks.retry_now(tid)                                                   # what Cecil's task_retry does
    assert store.get_deferred(did).status == "done" and tasks.get_task(tid)["status"] == "done"
    gate.set()
    assert tasks.wait(busy, 5)


def test_retries_start_together_not_one_at_a_time(on, clock):
    gate = threading.Event()
    calls = []

    def runner(brief, ctx):
        calls.append(brief)
        if len(calls) <= 3:
            return "broke", "error", "error"
        gate.wait(5)
        return "fixed", "ok"

    tasks.set_runner(runner)
    ids = [tasks.start(f"job {i}") for i in range(3)]
    for t in ids:
        assert tasks.wait(t, 5)
    assert all(tasks.get_task(t)["status"] == "retrying" for t in ids)
    blocker = tasks.start("long unrelated work")
    clock.advance(minutes=6)
    assert tasks.retry_due() == 3                                          # all three, despite the running task
    gate.set()
    for t in ids + [blocker]:
        assert tasks.wait(t, 5)
    assert all(tasks.get_task(t)["status"] == "done" for t in ids)


def test_retry_of_a_failed_text_goes_back_as_his_request(on, fake_model):
    tid = tasks.track("what's on my calendar", origin="sms", heard="what's on my calendar",
                      meta={"via": "sms", "label": "Text"})
    tasks.track_done(tid, "", "error", error="blocked: Claude Code could not start")
    fake_model.queue("You have two meetings.")
    tasks.retry_now(tid)
    d = store.get_deferred(tasks.get_task(tid)["meta"]["deferred_id"])
    assert d.kind == "owner_request" and d.channel == "sms" and d.status == "done"


def test_a_failed_scheduled_task_is_requeued_with_its_error(on, clock):
    did = schedule.schedule("check the reel stats", when=db.utcnow(), kind="task", title="Reel stats",
                            by="Cecil", why="he asked")
    tasks.set_runner(lambda brief, ctx: ("The work failed: yt-dlp crashed", "error", "error"))
    usage.run_due()
    tid = store.get_deferred(did).meta["task_id"]
    assert tasks.wait(tid, 5)
    d = store.get_deferred(did)
    assert d.status == "queued" and d.meta["attempts"] == 1 and "yt-dlp crashed" in d.meta["last_error"]
    assert schedule.MAX_RUNNING == 4


def test_reopen_failed_deferred_row(on):
    did = usage.defer("x", kind="prompt", at=db.utcnow())
    assert store.set_deferred_status(did, "running")
    store.set_deferred_status(did, "failed", result="boom")
    assert store.reopen_deferred(did) and store.get_deferred(did).status == "queued"
    assert not store.reopen_deferred(did)
