"""assistant/usage.py: plan-usage readings from rate-limit events (all call paths) and the zero-token
``get_usage`` poll that keeps the home page's Claude plan meters current while nothing is talking."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

import db
import home_live
from assistant import brain, claude_code, usage
from assistant import settings as asettings
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")


@pytest.fixture
def plan(monkeypatch):
    """Assistant on the claude_code backend (still fake: nothing real is spawned) + a fake poller."""
    asettings.set_assistant_settings(asettings.get_assistant_settings().replace(
        enabled=True, backend="claude_code", fake=True))
    answers: list = []

    def poller():
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a
    usage.set_poller(poller)
    yield answers
    usage.set_poller(None)


def _iso_in(**kw) -> str:
    return db.to_iso(db.utcnow() + timedelta(**kw))


def _answer(five=None, week=None, *, available=True, **extra):
    limits = {"five_hour": five, "seven_day": week, "seven_day_opus": None, "seven_day_sonnet": None, **extra}
    return {"session": {"total_cost_usd": 0}, "subscription_type": "pro", "rate_limits_available": available,
            "rate_limits": limits if available else None, "behaviors": None}


def test_one_event_with_unified_windows_refreshes_both_meters(clock):
    """CLI 2.1.3xx puts every general window in each event: the non-binding one must update too."""
    now = int(db.utcnow().timestamp())
    usage.record_window({"status": "allowed", "rateLimitType": "seven_day", "utilization": 0.75,
                         "resetsAt": now + 86400 * 3,
                         "unifiedWindows": {"five_hour": {"utilization": 0.12, "resetsAt": now + 3600},
                                            "seven_day": {"utilization": 0.75, "resetsAt": now + 86400 * 3},
                                            "seven_day_overage_included": {"utilization": 0.5, "resetsAt": now}}})
    m = {x["key"]: x for x in home_live.plan_meters()["meters"]}
    assert m["five_hour"]["left_pct"] == 88 and m["five_hour"]["note"].startswith("resets")
    assert m["seven_day"]["left_pct"] == 25
    w = usage.usage_status()["windows"]["five_hour"]
    assert w["inferred_from"] is None and w["left_at_least_pct"] is None and w["source"] == "event"


def test_stream_json_transcript_carries_unified_windows():
    now = int(db.utcnow().timestamp())
    line = json.dumps({"type": "rate_limit_event", "uuid": "u", "session_id": "s", "rate_limit_info": {
        "status": "allowed", "rateLimitType": "five_hour", "resetsAt": now + 3600,
        "unifiedWindows": {"five_hour": {"utilization": 0.4, "resetsAt": now + 3600},
                           "seven_day": {"utilization": 0.2, "resetsAt": now + 86400}}}})
    assert usage.record_events('{"type":"system"}\n' + line + "\n") == 1
    win = usage.usage_status()["windows"]
    assert win["five_hour"]["left_pct"] == 60 and win["seven_day"]["left_pct"] == 80


def test_poll_fills_both_meters_from_get_usage_percentages(plan, clock):
    plan.append(_answer({"utilization": 37.0, "resets_at": _iso_in(hours=2)},
                        {"utilization": 61.5, "resets_at": _iso_in(days=4)}))
    assert usage.poll_plan_usage() == {"status": "ok", "windows": 2}
    st = usage.usage_status()
    assert st["windows"]["five_hour"]["left_pct"] == 63 and st["windows"]["seven_day"]["left_pct"] == 38
    assert st["windows"]["five_hour"]["source"] == "poll" and st["windows"]["five_hour"]["as_of_text"]
    assert st["poll_ok"] and st["polled_at"] == db.now_iso()
    m = {x["key"]: x for x in home_live.plan_meters()["meters"]}
    assert m["five_hour"]["left_pct"] == 63 and m["seven_day"]["level"] == "ok"
    # polled again within 4 minutes: skipped (the job and the startup run may overlap); force goes ahead
    assert usage.poll_plan_usage()["status"] == "skipped"
    clock.advance(minutes=5)
    plan.append(_answer({"utilization": 0.0, "resets_at": None}, {"utilization": 62.0, "resets_at": _iso_in(days=4)}))
    assert usage.poll_plan_usage()["status"] == "ok"
    m = {x["key"]: x for x in home_live.plan_meters()["meters"]}
    assert m["five_hour"]["left_pct"] == 100 and m["seven_day"]["left_pct"] == 38


def test_poll_replaces_a_reset_window_and_an_inferred_bound(plan, clock):
    now = int(db.utcnow().timestamp())
    usage.record_window({"status": "allowed", "rateLimitType": "five_hour", "utilization": 0.9, "resetsAt": now + 600})
    clock.advance(minutes=20)                      # the 5-hour window reset, no event since
    assert usage.usage_status()["windows"]["five_hour"]["left_pct"] is None
    plan.append(_answer({"utilization": 3.0, "resets_at": _iso_in(hours=5)}, None))
    usage.poll_plan_usage(force=True)
    w = usage.usage_status()["windows"]["five_hour"]
    assert w["left_pct"] == 97 and not w["stale"] and w["resets_text"]


def test_poll_unavailable_or_failing_keeps_event_readings(plan, clock):
    now = int(db.utcnow().timestamp())
    usage.record_window({"status": "allowed", "rateLimitType": "five_hour", "utilization": 0.5, "resetsAt": now + 3600})
    plan.append(_answer(available=False))
    assert usage.poll_plan_usage(force=True) == {"status": "unavailable", "windows": 0}
    assert usage.usage_status()["windows"]["five_hour"]["left_pct"] == 50
    plan.extend([RuntimeError("get_usage is not supported in this context")] * 2)
    assert usage.poll_plan_usage(force=True)["status"] == "error"
    assert usage.poll_plan_usage(force=True)["status"] == "error"
    st = usage.usage_status()
    assert st["windows"]["five_hour"]["left_pct"] == 50 and not st["poll_ok"] and "not supported" in st["poll_error"]
    warn = [e for e in db.list_events(limit=50) if "usage check failed" in e.message]
    assert len(warn) == 1                           # a repeated failure is logged once


def test_poll_at_the_limit_sets_the_backoff(plan, clock):
    plan.append(_answer({"utilization": 100.0, "resets_at": _iso_in(hours=1)},
                        {"utilization": 70.0, "resets_at": _iso_in(days=2)}))
    usage.poll_plan_usage(force=True)
    assert claude_code.backoff_until() is not None
    assert usage.constrained()[0]


def test_poll_skipped_when_assistant_off_or_on_the_api():
    assert usage.poll_plan_usage(force=True)["status"] == "skipped"          # disabled by default
    asettings.set_assistant_settings(asettings.get_assistant_settings().replace(enabled=True, backend="api"))
    assert usage.poll_plan_usage(force=True)["status"] == "skipped"


def test_background_worker_records_rate_limit_events(monkeypatch):
    """brain.run_worker (background tasks, initiative, surprises) used to drop these events."""
    asettings.set_assistant_settings(asettings.get_assistant_settings().replace(enabled=True))
    t = brain._sdk()
    now = int(db.utcnow().timestamp())

    class Client:
        def __init__(self, *a):
            pass

        async def connect(self, prompt=None):
            pass

        async def query(self, prompt, session_id="default"):
            pass

        async def receive_messages(self):
            yield t.RateLimitEvent(rate_limit_info=t.RateLimitInfo(status="allowed", raw={
                "status": "allowed", "rateLimitType": "five_hour", "utilization": 0.33, "resetsAt": now + 3600,
                "unifiedWindows": {"seven_day": {"utilization": 0.44, "resetsAt": now + 86400}}}),
                uuid="u", session_id="s")
            yield t.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                  num_turns=1, session_id="s", result="Done it.")

        async def disconnect(self):
            pass

    brain.set_client_factory(lambda options, tools: Client())
    try:
        text, status = brain.run_worker("do the thing")
    finally:
        brain.set_client_factory(None)
    assert (text, status) == ("Done it.", "ok")
    win = usage.usage_status()["windows"]
    assert win["five_hour"]["left_pct"] == 67 and win["seven_day"]["left_pct"] == 56


def test_poll_jobs_are_scheduled():
    import main

    asettings.set_assistant_settings(asettings.get_assistant_settings().replace(enabled=True))

    names = {j.id for j in main.assistant_job_specs()}
    assert {"assistant_usage_poll", "assistant_usage_poll_startup"} <= names


def _worker_client(t, events):
    class Client:
        interrupted = 0

        def __init__(self, *a):
            pass

        async def connect(self, prompt=None):
            pass

        async def query(self, prompt, session_id="default"):
            pass

        async def receive_messages(self):
            for e in events():
                yield e

        async def interrupt(self):
            Client.interrupted += 1

        async def disconnect(self):
            pass
    return Client


def test_background_task_pauses_after_using_half_the_window_not_after_n_steps(monkeypatch):
    asettings.set_assistant_settings(asettings.get_assistant_settings().replace(enabled=True))
    t = brain._sdk()
    now = int(db.utcnow().timestamp())
    monkeypatch.setattr(brain, "USAGE_CHECK_SEC", 0.0)
    monkeypatch.setattr(usage, "poll_plan_usage", lambda **k: {})
    usage.record_window({"status": "allowed", "rateLimitType": "five_hour", "utilization": 0.10, "resetsAt": now + 3600})

    def events():
        for i in range(30):   # many cheap steps: fine on their own
            yield t.AssistantMessage(content=[t.TextBlock(text=f"step {i}")], model="m")
        usage.record_window({"status": "allowed", "rateLimitType": "five_hour", "utilization": 0.65,
                             "resetsAt": now + 3600})
        yield t.AssistantMessage(content=[t.TextBlock(text="more")], model="m")
        yield t.AssistantMessage(content=[t.TextBlock(text="never reached")], model="m")

    Client = _worker_client(t, events)
    brain.set_client_factory(lambda options, tools: Client())
    try:
        text, status, kind = brain.run_worker("explore", with_kind=True)
    finally:
        brain.set_client_factory(None)
    assert status == "budget" and kind == "usage_cap" and "50%" in text and Client.interrupted == 1


def test_cancelled_task_stops_mid_run():
    asettings.set_assistant_settings(asettings.get_assistant_settings().replace(enabled=True))
    t = brain._sdk()
    flag = {"stop": False}

    def events():
        yield t.AssistantMessage(content=[t.TextBlock(text="working")], model="m")
        flag["stop"] = True
        yield t.AssistantMessage(content=[t.TextBlock(text="still working")], model="m")

    Client = _worker_client(t, events)
    brain.set_client_factory(lambda options, tools: Client())
    try:
        text, status, kind = brain.run_worker("x", with_kind=True, should_stop=lambda: flag["stop"])
    finally:
        brain.set_client_factory(None)
    assert status == "stopped" and Client.interrupted == 1
