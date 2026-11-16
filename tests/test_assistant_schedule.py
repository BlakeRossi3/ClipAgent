"""assistant/schedule.py: Cecil's scheduled tasks, the Scheduled page, its JSON and the schedule_* tools."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from assistant_fixtures import assistant_env  # noqa: F401
from fastapi.testclient import TestClient

import db
import server
from assistant import schedule, store, tasks, tools, usage
from assistant import settings as asettings

pytestmark = pytest.mark.usefixtures("assistant_env")


@pytest.fixture
def on(monkeypatch, clock):
    monkeypatch.setenv("ASSISTANT_ENABLED", "true")
    asettings.reset_assistant_settings()
    store.init_store()
    runs: list[tuple[str, dict]] = []

    def runner(brief, context):
        runs.append((brief, context))
        return "TITLE: A tiny orrery\nDone: the thing is finished.", "ok"

    tasks.set_runner(runner)
    yield runs
    tasks.set_runner(None)


def _ctx():
    return tools.ToolContext(channel="dashboard", agent="main", source="test")


def _wait_all():
    for t in tasks.list_tasks():
        tasks.wait(t["id"], 10)


def local(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=db.local_tz()).astimezone(UTC)


def test_parse_when_forms(on, clock):
    now = db.utcnow()                                     # 2026-11-13 10:00 New York
    assert schedule.parse_when("after_reset") is None and schedule.parse_when("") is None
    assert schedule.parse_when("in 20 minutes") == now + timedelta(minutes=20)
    assert schedule.parse_when("in 2 hours") == now + timedelta(hours=2)
    assert schedule.parse_when("7:10am") == local(2026, 11, 14, 7, 10)        # passed today -> tomorrow
    assert schedule.parse_when("19:30") == local(2026, 11, 13, 19, 30)
    assert schedule.parse_when("tomorrow 9am") == local(2026, 11, 14, 9, 0)
    assert schedule.parse_when("2026-11-20 08:15") == local(2026, 11, 20, 8, 15)
    for bad in ("someday", "in 0 minutes", "25:00", "13pm"):
        with pytest.raises(schedule.ScheduleError):
            schedule.parse_when(bad)


def test_next_occurrence_and_words(on, clock):
    fri = local(2026, 11, 13, 7, 10)                       # a Friday
    assert schedule.next_occurrence(fri, "daily", after=fri) == local(2026, 11, 14, 7, 10)
    assert schedule.next_occurrence(fri, "weekdays", after=fri) == local(2026, 11, 16, 7, 10)   # Monday
    assert schedule.next_occurrence(fri, "weekly", after=fri) == local(2026, 11, 20, 7, 10)
    now = db.utcnow()
    assert schedule.when_words(now + timedelta(minutes=12)) == "in 12 min"
    assert schedule.when_words(local(2026, 11, 14, 7, 10)) == "tomorrow 7:10am"
    assert schedule.when_words(local(2026, 11, 13, 19, 0)) == "today 7pm"
    assert schedule.when_words(local(2026, 11, 17, 9, 30)) == "Tue 9:30am"
    assert schedule.when_words(None) == "after the plan resets"


def test_tool_schedules_and_due_row_runs_as_background_task(on, clock):
    tools.load_extensions()
    res = tools.dispatch("schedule_prompt", {"text": "Look up the launch date and tell him.", "at": "in 30 minutes",
                                             "title": "Launch date", "why": "he asked at breakfast"}, _ctx(),
                         allowed={"schedule_prompt"})
    assert res.ok, res.content
    d = store.list_deferred("queued")[0]
    assert d.kind == "task" and d.title == "Launch date" and d.meta["why"] == "he asked at breakfast"
    assert usage.run_due() == 0 and not on                 # not due yet
    clock.advance(minutes=31)
    assert usage.run_due() == 1
    _wait_all()
    brief, _ctx_ = on[0]
    assert "Look up the launch date" in brief and "<scheduled_task" in brief and "he asked at breakfast" in brief
    row = store.get_deferred(d.id)
    assert row.status == "done" and "finished" in row.result
    assert any(m.kind == "voice_detail" for m in store.list_messages(10, channel="dashboard"))   # reported back
    assert tasks.list_tasks()[-1]["meta"]["scheduled_id"] == d.id


def test_recurring_task_books_the_next_one(on, clock):
    did = schedule.schedule("Morning news summary", when=db.utcnow() + timedelta(minutes=5), recur="weekdays",
                            title="News")
    clock.advance(minutes=6)
    assert usage.run_due() == 1
    _wait_all()
    assert store.get_deferred(did).status == "done"
    nxt = [d for d in store.list_deferred("queued") if d.title == "News"]
    assert len(nxt) == 1 and nxt[0].recur == "weekdays" and nxt[0].meta["previous"] == did
    assert db.parse_iso(nxt[0].run_after) == local(2026, 11, 16, 10, 5)          # Fri -> Mon
    assert schedule.book_next(store.get_deferred(did)) is None                   # booked once only
    with pytest.raises(schedule.ScheduleError):
        schedule.schedule("x", when=None, recur="daily")


def test_list_and_cancel_tools(on, clock):
    tools.load_extensions()
    did = schedule.schedule("Check the hours log", when=db.utcnow() + timedelta(hours=3), title="Hours check")
    store.add_checkin("hours", "How many hours did you work this week?", db.to_iso(db.utcnow() + timedelta(hours=1)))
    out = tools.dispatch("schedule_list", {}, _ctx(), allowed={"schedule_list"})
    assert out.ok
    snap = schedule.snapshot()
    kinds = [x["kind"] for x in snap["upcoming"]]
    assert "checkin" in kinds and "task" in kinds
    ats = [x["at"] for x in snap["upcoming"] if x["at"]]
    assert ats == sorted(ats)                                                    # time order
    assert any(x["source"] == "job" for x in snap["upcoming"])                   # daily jobs listed
    assert any(r["id"] == "assistant_deferred" for r in snap["routine"])
    bad = tools.dispatch("schedule_cancel", {"id": 9999}, _ctx(), allowed={"schedule_cancel"})
    assert not bad.ok
    assert tools.dispatch("schedule_cancel", {"id": did}, _ctx(), allowed={"schedule_cancel"}).ok
    assert store.get_deferred(did).status == "cancelled"
    assert schedule.snapshot()["recent"][0]["outcome"] == "cancelled"


def test_stale_running_rows_fail(on, clock):
    did = schedule.schedule("long job", when=db.utcnow())
    assert store.set_deferred_status(did, "running")
    clock.advance(hours=2)
    assert schedule.sweep_stale() == 1 and store.get_deferred(did).status == "failed"


def _csrf(c):
    import re

    html = c.get("/dashboard/scheduled").text
    return re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1)


def test_scheduled_page_json_and_actions(on, clock):
    c = TestClient(server.create_app())
    did = schedule.schedule("Draft the weekly plan", when=db.utcnow() + timedelta(hours=2), title="Weekly plan",
                            by="Cecil", why="Sunday habit")
    page = c.get("/dashboard/scheduled")
    assert page.status_code == 200 and "Weekly plan" in page.text and "Sunday habit" in page.text
    assert "today 12pm" in page.text and 'href="/dashboard/scheduled"' in page.text     # nav entry
    j = c.get("/dashboard/api/scheduled").json()
    assert any(x["id"] == did and x["source"] == "scheduled" for x in j["upcoming"])
    csrf = _csrf(c)
    assert c.post(f"/dashboard/scheduled/{did}/cancel", data={}).status_code == 403          # CSRF guard
    r = c.post(f"/dashboard/scheduled/{did}/cancel", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and store.get_deferred(did).status == "cancelled"
    assert c.post(f"/dashboard/scheduled/{did}/run", data={"csrf": csrf}).status_code == 409
    r = c.post("/dashboard/scheduled/new", data={"csrf": csrf, "text": "Remind me about the dentist",
                                                 "at": "in 1 hour", "repeat": ""}, follow_redirects=False)
    assert r.status_code == 303
    new = [d for d in store.list_deferred("queued") if "dentist" in d.prompt][0]
    assert new.meta["by"].startswith("you")
    r = c.post(f"/dashboard/scheduled/{new.id}/run", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303
    for _ in range(100):
        if store.get_deferred(new.id).status == "done":
            break
        _wait_all()
        time.sleep(0.05)
    assert store.get_deferred(new.id).status == "done" and "dentist" in on[-1][0]
    assert c.post("/dashboard/scheduled/new", data={"csrf": csrf, "text": "x", "at": "whenever"}).status_code == 400
