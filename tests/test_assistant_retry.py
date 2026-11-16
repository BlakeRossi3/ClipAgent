"""assistant.retry: which outcomes are retried, when, and what the retried brief says."""

from __future__ import annotations

from datetime import timedelta

import pytest

import db
from assistant import retry
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")


@pytest.mark.parametrize("status,expected", [("budget", True), ("error", True), ("stopped", True),
                                             ("rounds", True), ("denied", False), ("ok", False)])
def test_only_refusals_and_successes_are_final(status, expected):
    assert retry.should_retry(status) is expected


def test_backoff_then_hourly_forever():
    now = db.utcnow()
    waits = [retry.next_try("error", n, "error", now) - now for n in (1, 2, 3, 4, 5, 50)]
    assert waits == [timedelta(minutes=m) for m in (5, 15, 30, 60, 60, 60)]
    assert retry.next_try("rounds", 1, "", now) - now == timedelta(minutes=5)


def test_stopped_and_paused_wait_the_fallback():
    now = db.utcnow()
    assert retry.next_try("stopped", 1, "", now) == now + retry.WAIT_FALLBACK
    assert retry.next_try("budget", 1, "paused", now) == now + retry.WAIT_FALLBACK


def test_daily_cap_waits_until_after_local_midnight():
    now = db.utcnow()
    at = retry.next_try("budget", 1, "daily", now)
    assert at > now and at.astimezone(db.local_tz()).date() == db.local_today() + timedelta(days=1)


def test_describe_and_note():
    assert "more steps" in retry.describe("rounds")
    assert retry.describe("error", "timeout") == "it ran out of time"
    note = retry.retry_note(3, "it ran out of time")
    assert "<retry>" in note and "attempt 3" in note and "carry on" in note


def test_repeated_failures_nag_at_three_then_every_tenth():
    nags = [n for n in range(1, 31) if retry.blocker("error", "error", n, "Build the thing")]
    assert nags == [3, 10, 20, 30]
    assert retry.blocker("rounds", "", 1, "x") is None


# -- background tasks (assistant/tasks.py) -------------------------------------------------------

@pytest.fixture
def enabled(monkeypatch):
    from assistant import settings as asettings
    from assistant import tasks

    monkeypatch.setenv("ASSISTANT_ENABLED", "true")
    import config

    config.reset_settings()
    asettings.reset_assistant_settings()
    yield tasks
    tasks.set_runner(None)


def test_a_failed_conversation_task_is_retried_until_it_finishes(enabled, clock):
    tasks = enabled
    briefs: list[str] = []

    def runner(brief, ctx):
        briefs.append(brief)
        return ("It timed out.", "error", "timeout") if len(briefs) == 1 else ("All done: three files fixed.", "ok")

    tasks.set_runner(runner)
    tid = tasks.start("fix the three files", heard="fix those files")
    assert tasks.wait(tid, 5)
    row = tasks.get_task(tid)
    assert row["status"] == "retrying" and row["attempts"] == 1 and row["last_error"] == "it ran out of time"
    assert db.parse_iso(row["next_try"]) == db.utcnow() + timedelta(minutes=5)
    assert tasks.retry_due() == 0                                   # not due yet
    clock.advance(minutes=6)
    assert tasks.retry_due() == 1
    assert tasks.wait(tid, 5)
    row = tasks.get_task(tid)
    assert row["status"] == "done" and row["attempts"] == 2 and row["result"].startswith("All done")
    assert "<retry>" not in briefs[0] and "attempt 2" in briefs[1] and "ran out of time" in briefs[1]


def test_owned_budget_and_ok_runs_are_not_retried(enabled):
    tasks = enabled
    tasks.set_runner(lambda brief, ctx: ("broke", "error"))
    seen = []
    tid = tasks.start("scheduled job", on_finish=lambda row, full, st: seen.append(st))
    assert tasks.wait(tid, 5)
    assert tasks.get_task(tid)["status"] == "error" and seen == ["error"]     # its owner decides
    tasks.set_runner(lambda brief, ctx: ("limit", "budget"))
    tid = tasks.start("plan ran out")
    assert tasks.wait(tid, 5)
    assert tasks.get_task(tid)["status"] == "budget"                           # handoff resumes it instead


def test_orphans_are_requeued_and_unfinished_rows_are_never_trimmed(enabled, clock):
    tasks = enabled
    db.kv_set(tasks.KV_TASKS, [{"id": "orph", "task": "cut off", "status": "running", "retry": True,
                                "started": db.now_iso(), "steps": []},
                               {"id": "sched", "task": "scheduled", "status": "running",
                                "started": db.now_iso(), "steps": []}])
    clock.advance(hours=2)
    assert tasks._requeue_orphans() == 1
    assert tasks.get_task("orph")["status"] == "retrying" and tasks.get_task("sched")["status"] == "running"
    for i in range(tasks.TASK_KEEP + 3):
        tasks._save({"id": f"d{i}", "task": "x", "status": "done"}, publish=False)
    ids = [t["id"] for t in tasks.list_tasks()]
    assert "orph" in ids and "sched" in ids and len(ids) == tasks.TASK_KEEP + 2
