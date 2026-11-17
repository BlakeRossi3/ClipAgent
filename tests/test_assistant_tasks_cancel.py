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
