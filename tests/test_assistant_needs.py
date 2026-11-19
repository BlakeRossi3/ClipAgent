"""The one 'needs you' list: badge count == Cecil's count; seen failures clear; answers stick."""

from __future__ import annotations

import pytest

import db
from assistant import meals, needs, store, tools
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")


def _ctx():
    return tools.ToolContext(channel="voice", agent="main", source="test")


def _call(name, **args):
    tools.load_extensions()
    res = tools.dispatch(name, args, _ctx(), allowed={name})
    assert res.ok, res.content
    import json
    return json.loads(res.content)


def test_badge_and_cecil_count_the_same_four(clock):
    db.add_decision("strategy_change", "Try a new niche")
    store.add_checkin("onboarding:meals", "Anything you cannot eat?", db.now_iso())
    db.log_event("fetch", "download failed", level="error")
    store.record_agent_run("sentinel", "check", model="m", duration_ms=1, cost_usd=0.0, status="error",
                           summary="it broke")
    d = needs.data()
    assert d["count"] == 4 == len(needs.items()) == _call("needs_list")["count"]
    assert needs.briefing("sir").startswith("Four things need you, sir.")


def test_looking_at_failures_clears_them_until_new_ones(clock):
    for _ in range(13):
        db.log_event("fetch", "download failed", level="error")
    d = needs.data()                                   # 13 copies of one failure = ONE problem, said once
    assert d["failures_24h"] == 1 and d["problems"][0]["count"] == 13 and len(needs.items()) == 1
    clock.advance(seconds=5)
    _call("needs_dismiss", kind="failures")
    assert needs.data()["count"] == 0 and needs.briefing() == ""
    clock.advance(seconds=5)
    db.log_event("fetch", "another", level="error")
    assert needs.data()["failures_24h"] == 1


def test_every_turn_knows_what_the_pill_is(clock):
    from assistant import status

    assert "need you" not in status.turn_context()     # nothing pending: no line, no tokens
    db.log_event("notify", "call to owner failed (daily_summary)", level="error",
                 data={"error": "HTTP 400 error: Url is not a valid URL: http://localhost:8000/twilio/say/22"})
    ctx = status.turn_context()
    assert "'1 needs you' pill" in ctx and "call to owner failed" in ctx and "needs_dismiss" in ctx


def test_saying_he_has_seen_it_clears_the_pill(clock):
    db.log_event("notify", "call to owner failed (approval)", level="error")
    store.record_agent_run("sentinel", "check", model="m", duration_ms=1, cost_usd=0.0, status="error",
                           summary="it broke")
    assert needs.data()["count"] == 2
    clock.advance(seconds=5)
    out = _call("needs_dismiss", kind="all")
    assert out["remaining"] == 0 and needs.data()["count"] == 0


def test_meal_answer_by_voice_is_saved_and_never_asked_again(clock):
    cid = meals.request_preferences()
    assert any(i["kind"] == "checkin" for i in needs.items())
    out = _call("checkin_answer", checkin_id=cid, answer="no mushrooms, love spicy food, 30 minutes max")
    assert out["recorded"]
    assert store.get_checkin(cid).status == "answered" and meals.preferences()
    assert not needs.items() and meals.request_preferences() is None


def test_meals_set_preferences_closes_the_open_question(clock):
    cid = meals.request_preferences()
    _call("meals_set_preferences", preferences="no fish; quick dinners")
    assert store.get_checkin(cid).status == "answered"
    assert _call("meals_suggest", slot="dinner")["preferences_known"] is True


def test_open_url_never_opens_a_real_browser_in_fake_mode():
    tools.set_url_opener(None)
    out = _call("open_url", url="https://www.youtube.com")
    assert out["opened"] and "fake mode" in out["note"]
