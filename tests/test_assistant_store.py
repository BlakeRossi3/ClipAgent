"""assistant.store: own migrations in the shared DB, typed helpers."""

from __future__ import annotations

from datetime import date

import pytest

import db
from assistant import store
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")


def test_migrations_idempotent_and_versioned():
    assert store.init_store() == store.SCHEMA_VERSION
    assert store.init_store() == store.SCHEMA_VERSION
    assert store.schema_version() == store.SCHEMA_VERSION
    assert db.kv_get(store.VERSION_KEY) == store.SCHEMA_VERSION
    with db.transaction() as c:
        names = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("as_messages", "as_checkins", "as_jobs", "as_work_log", "as_income", "as_meals", "as_goals",
              "as_agent_runs", "as_lessons", "as_vault_index", "as_vault_fts"):
        assert t in names
    assert "sources" in names  # same database as the pipeline
    assert db.schema_version() >= 1  # phase-1 schema untouched by us


def test_helpers_migrate_lazily():
    assert store.schema_version() == 0
    store.add_message("in", "sms", "hello")
    assert store.schema_version() == store.SCHEMA_VERSION


def test_messages(clock):
    a = store.add_message("in", "sms", "How did the channel do?")
    b = store.add_message("out", "sms", "Well, sir.", kind="reply", agent="main", cost_usd=0.01, reply_to=a,
                          meta={"x": 1})
    clock.advance(minutes=1)
    store.add_message("out", "sms", "Brief", kind="morning_brief")
    msgs = store.list_messages(10)
    assert [m.body for m in msgs][-1] == "How did the channel do?"
    got = store.get_message(b)
    assert got.reply_to == a and got.meta == {"x": 1} and got.cost_usd == 0.01
    start, end = db.local_day_bounds()
    assert store.count_outbound_between(start, end) == 1  # replies excluded
    with pytest.raises(ValueError):
        store.add_message("sideways", "sms", "x")
    with pytest.raises(ValueError):
        store.add_message("in", "carrier-pigeon", "x")


def test_checkins_lifecycle(clock):
    cid = store.add_checkin("weekly_hours", "How many hours this week?", "2026-11-13T22:00:00Z")
    later = store.add_checkin("meal", "Lunch?", "2026-11-14T16:00:00Z")
    assert store.due_checkins() == []
    clock.set(db.parse_iso("2026-11-13T22:05:00Z"))
    assert [c.id for c in store.due_checkins()] == [cid]
    mid = store.add_message("out", "sms", "How many hours this week?", kind="checkin")
    store.mark_checkin_sent(cid, message_id=mid)
    assert store.due_checkins() == []
    assert store.open_checkin().id == cid
    store.answer_checkin(cid, "38", {"hours": 38})
    c = store.get_checkin(cid)
    assert c.status == "answered" and c.parsed == {"hours": 38} and c.answered_at
    assert store.open_checkin() is None
    store.set_checkin_status(later, "skipped")
    assert [x.id for x in store.list_checkins("skipped")] == [later]
    with pytest.raises(ValueError):
        store.add_checkin("x", "q", "tomorrow")


def test_jobs_hours_income(clock):
    j = store.upsert_job("Warehouse", hourly_rate=24, est_tax_rate=0.2)
    assert store.upsert_job("Warehouse", hourly_rate=25, est_tax_rate=0.2) == j
    assert store.get_job(j).hourly_rate == 25
    assert store.get_job_by_name("warehouse").id == j
    w1 = store.log_hours(j, 38)                      # Friday 2026-11-13 -> week of Monday 11-09
    w2 = store.log_hours(j, 40, week_start="2026-11-12")  # normalised to the same Monday: upsert
    assert w1 == w2
    wl = store.get_work_log(j)
    assert wl.week_start == "2026-11-09" and wl.hours == 40
    store.log_hours(j, 30, week_start=date(2026, 11, 2))
    assert [w.week_start for w in store.list_work_log(job_id=j)] == ["2026-11-09", "2026-11-02"]
    with pytest.raises(ValueError):
        store.log_hours(j, 200)
    assert store.add_income(960.0, source="job", ref="pay-2026-11-13", note="paycheck") is not None
    assert store.add_income(960.0, source="job", ref="pay-2026-11-13") is None  # idempotent
    store.add_income(12.5, source="reels", day="2026-11-10")
    store.add_income(12.5, source="reels", day="2026-11-10")  # no ref: both kept
    assert store.income_total("2026-11-01", "2026-11-30") == 985.0
    assert store.income_total(source="reels") == 25.0
    with pytest.raises(ValueError):
        store.add_income(1, source="lottery")
    assert store.week_start_of("2026-11-15") == date(2026, 11, 9)


def test_meals_goals_lessons():
    m = store.add_meal("lunch", "Chicken rice bowl")
    store.update_meal(m, status="eaten", feedback="good")
    assert store.get_meal(m).status == "eaten"
    assert store.list_meals(slot="lunch")[0].feedback == "good"
    with pytest.raises(ValueError):
        store.add_meal("brunch", "x")
    g = store.add_goal("Save $5,000", metric="usd", target=5000, current=1200, due="2026-12-31")
    store.update_goal(g, current=1500)
    assert store.get_goal(g).current == 1500
    store.update_goal(g, status="done")
    assert store.list_goals() == [] and len(store.list_goals(None)) == 1
    with pytest.raises(ValueError):
        store.update_goal(g, owner="me")
    with pytest.raises(db.NotFound):
        store.update_goal(999, current=1)
    lid = store.add_lesson("sms", "Keep morning briefs to one sentence", evidence="replied 'too long' twice")
    store.set_lesson_status(lid, "adopted")
    assert store.list_lessons("adopted")[0].lesson.startswith("Keep")


def test_agent_runs_and_assistant_spend(clock):
    rid = store.record_agent_run("main", "hello", model="claude-fable-5-2", duration_ms=0, cost_usd=0,
                                 status="running")
    store.finish_agent_run(rid, status="ok", duration_ms=120, cost_usd=0.02, summary="done",
                           tool_calls=[{"name": "memory_search"}])
    r = store.get_agent_run(rid)
    assert r.status == "ok" and r.tool_calls == [{"name": "memory_search"}] and r.cost_usd == 0.02
    assert store.list_agent_runs(agent="main")[0].id == rid
    db.insert_llm_usage(model="claude-fable-5-2", purpose="assistant.main", tier="assistant", input_tokens=1,
                        output_tokens=1, cost_usd=0.30)
    db.insert_llm_usage(model="claude-haiku-5-5", purpose="pick.score_window", tier="haiku", input_tokens=1,
                        output_tokens=1, cost_usd=0.05)
    assert store.assistant_spent_today() == pytest.approx(0.30)
