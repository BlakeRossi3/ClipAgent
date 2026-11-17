"""The rail's task list: kv is the truth for Cecil's own tasks, so a deleted retry does not linger."""

from __future__ import annotations

import pytest
from assistant_fixtures import assistant_env  # noqa: F401

import db
from assistant import events, tasks

pytestmark = pytest.mark.usefixtures("assistant_env")


def _row(tid: str, status: str, **kw):
    return {"id": tid, "task": f"task {tid}", "status": status, "started": db.now_iso(), "finished": None,
            "summary": "", "steps": [], **kw}


def test_deleted_retrying_rows_leave_the_rail_and_kv_wins():
    from voice import web

    events.reset()
    keep = _row("keep1", "retrying", retry=True)
    gone = _row("gone1", "retrying", retry=True)
    db.kv_set(tasks.KV_TASKS, [keep, gone])
    events.publish("task", task=tasks.public(keep))
    events.publish("task", task=tasks.public(gone))
    events.publish("task", task={"id": "sdk1", "task": "ls", "status": "stopped", "started": db.now_iso()})

    db.kv_set(tasks.KV_TASKS, [{**keep, "status": "cancelled"}])   # one removed, one cancelled behind the bus

    by_id = {t["id"]: t for t in web.activity()["tasks"]}
    assert "gone1" not in by_id
    assert by_id["keep1"]["status"] == "cancelled"
    assert by_id["sdk1"]["status"] == "stopped"        # Claude Code's own agents stay as they were
