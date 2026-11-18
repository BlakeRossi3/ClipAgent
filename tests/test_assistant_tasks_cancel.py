"""Cancel / retry-in-place for background tasks (tools + the x on the Jarvis rail)."""

from __future__ import annotations

import json

import pytest

import db
from assistant import tasks, tools
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")


def _row(tid, status, task="check the giants score"):
    tasks._save({"id": tid, "task": task, "status": status, "started": db.now_iso(), "steps": [],
                 "next_try": db.now_iso() if status == "retrying" else None}, publish=False)


def _call(name, **a):
    tools.load_extensions()
    res = tools.dispatch(name, a, tools.ToolContext(channel="voice", agent="main", source="t"), allowed={name})
    assert res.ok, res.content
    return json.loads(res.content)


def test_cancel_by_words_removes_a_retrying_task_for_good():
    _row("t1", "retrying")
    out = _call("task_cancel", task="giants")
    assert out["status"] == "cancelled" and tasks.get_task("t1")["status"] == "cancelled"
    assert not [t for t in tasks.list_tasks() if t["status"] == "retrying"]


def test_retry_reuses_the_same_task(monkeypatch):
    _row("t2", "error")
    started = []
    monkeypatch.setattr(tasks, "retry_due", lambda *a, **k: started.append(1) or 1)
    out = _call("task_retry", task="t2")
    assert out["task"] == "t2" and started and len(tasks.list_tasks()) == 1


def test_cancel_while_running_is_not_undone_by_its_own_progress_or_failure():
    """The worker thread keeps saving its own (stale "running") copy of the row on every step and at the
    end; a cancel in between must win, so the row never drops back to "retrying"."""
    import threading

    go, stepped = threading.Event(), threading.Event()

    def runner(brief, context, on_step):
        go.wait(5)
        on_step("still working")
        stepped.set()
        return "it ran out of time", "error"

    tasks.set_runner(runner)
    try:
        tid = tasks.start("send the whatsapp reminders", origin="voice")
        tasks.cancel(tid)
        go.set()
        assert tasks.wait(tid, 5) and stepped.is_set()
    finally:
        tasks.set_runner(None)
    row = tasks.get_task(tid)
    assert row["status"] == "cancelled" and not row.get("next_try")
    assert not [t for t in tasks.list_tasks() if t["status"] in tasks.UNFINISHED]


def test_retry_due_never_restarts_a_cancelled_task():
    _row("t4", "retrying")
    tasks.cancel("t4")
    assert tasks.retry_due(force=True) == 0 and tasks.get_task("t4")["status"] == "cancelled"


def test_retry_now_can_still_revive_a_cancelled_task(monkeypatch):
    _row("t6", "retrying")
    tasks.cancel("t6")
    monkeypatch.setattr(tasks, "retry_due", lambda *a, **k: 1)
    assert tasks.retry_now("t6")["status"] == "retrying"


def test_asking_again_reuses_the_unfinished_task_instead_of_a_second_copy(monkeypatch):
    tasks._save({"id": "t5", "task": "Send the WhatsApp reminders", "status": "retrying", "retry": True,
                 "started": db.now_iso(), "steps": [], "next_try": db.now_iso()}, publish=False)
    _row("t5b", "error", task="something else")
    revived = []
    monkeypatch.setattr(tasks, "retry_now", lambda tid: revived.append(tid))
    started = []
    monkeypatch.setattr(tasks, "_start", lambda row: started.append(row["id"]))
    tid = tasks.start("send the  whatsapp reminders", origin="voice")
    assert tid == "t5" and revived == ["t5"] and not started and len(tasks.list_tasks()) == 2
    # a finished or cancelled one with the same words does not block a fresh run
    tasks.cancel("t5")
    tid2 = tasks.start("send the whatsapp reminders", origin="voice")
    assert tid2 != "t5" and started == [tid2]


def test_x_button_route_cancels(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from voice import web

    _row("t3", "retrying")

    async def ok(request):
        return None, {}

    monkeypatch.setattr(web, "_ask_guard", ok)
    app = FastAPI()
    app.include_router(web.router)
    r = TestClient(app).post(f"{web.router.prefix}/api/task/t3/cancel")
    assert r.status_code == 200 and tasks.get_task("t3")["status"] == "cancelled"
