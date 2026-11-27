from datetime import UTC, date, datetime

import pytest

import db
from assistant import checkins, store, vault
from assistant import settings as aset
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")


@pytest.fixture
def enabled():
    aset.set_assistant_settings(aset.get_assistant_settings().replace(enabled=True))
    yield
    aset.set_assistant_settings(None)


def test_routine_note_overrides(clock):
    checkins.ensure_routine_note()
    note = vault.read_note(checkins.ROUTINE_NOTE)
    vault.write_note(checkins.ROUTINE_NOTE, note.body.replace("- dinner_idea: 16:45 weekdays", "- dinner_idea: off")
                     + "\n- wellbeing: 25:99 daily\n", base_sha=note.sha)
    r = checkins.load_routine()
    assert r["dinner_idea"].at is None and r["wellbeing"].days == frozenset({1, 3})   # bad line ignored
    assert checkins.parse_days("mon-fri") == frozenset(range(5))


def test_plan_day_onboarding_pay_rate_first(clock):
    clock.set(datetime(2026, 11, 13, 5, 30, tzinfo=UTC))   # 00:30 Friday local
    checkins.plan_day()
    kinds = {c.kind for c in checkins.planned()}
    assert {"morning_brief", "onboarding:pay_rate", "weekly_hours", "evening_wrap"} <= kinds
    assert "wellbeing" not in kinds and checkins.plan_day() == []


def test_brief_reask_and_expiry(clock, enabled):
    clock.set(datetime(2026, 11, 13, 13, 10, tzinfo=UTC))   # 08:10 local
    b = store.add_checkin("morning_brief", "(morning brief)", "2026-11-13T13:05:00Z")
    q = store.add_checkin("weekly_hours", "How many hours did you work this week, sir?", "2026-11-13T13:05:00Z")
    checkins.send_due()
    assert store.get_checkin(b).status == "answered" and store.get_checkin(q).sent_at
    clock.advance(hours=25)                                     # 09:10 next day, outside quiet hours
    assert checkins.send_due()["reasked"] == 1
    clock.advance(hours=1)
    assert checkins.send_due()["reasked"] == 0                  # never twice
    clock.advance(hours=16)
    checkins.send_due()
    assert store.get_checkin(q).status == "expired"


def test_wellbeing_and_unrelated_reply(clock, enabled):
    c = store.add_checkin("wellbeing", "How are you, sir?", db.now_iso())
    store.mark_checkin_sent(c)
    assert checkins.handle_reply("what's the weather?") is False
    assert checkins.handle_reply("pretty stressed honestly") is True
    assert db.kv_get(f"checkins:gentle:{date(2026, 11, 14).isoformat()}")
    assert "stressed" in vault.read_note(vault.journal_path()).body


def test_hours_question_not_sent_when_already_logged_by_voice(clock, enabled):
    from assistant import ledger

    clock.set(datetime(2026, 11, 13, 23, 45, tzinfo=UTC))   # 18:45 Friday local
    ledger.set_job(hourly_rate=20)
    ledger.log_hours(38, week=date(2026, 11, 13), source="voice")
    q = store.add_checkin("weekly_hours", "How many hours did you work this week, sir?", "2026-11-13T23:40:00Z")
    checkins.send_due()
    assert store.get_checkin(q).status == "skipped" and not store.get_checkin(q).sent_at


def test_hours_reask_dropped_once_logged_by_voice(clock, enabled):
    from assistant import ledger

    clock.set(datetime(2026, 11, 13, 23, 45, tzinfo=UTC))
    ledger.set_job(hourly_rate=20)
    q = store.add_checkin("weekly_hours", "How many hours did you work this week, sir?", "2026-11-13T23:40:00Z")
    checkins.send_due()
    assert store.get_checkin(q).sent_at
    ledger.log_hours(40, week=date(2026, 11, 13), source="voice")   # told Cecil by voice instead
    clock.advance(hours=16)
    out = checkins.send_due()
    assert out["reasked"] == 0 and store.get_checkin(q).status == "answered"


def test_pay_answers_given_by_voice_are_not_asked_again(clock):
    from assistant import ledger

    ledger.set_job(hourly_rate=25)
    assert checkins.next_gap().key == "tax"
    ledger.set_job(est_tax_rate=0)                  # "no tax set-aside" is still an answer
    assert checkins.next_gap().key == "pay_schedule"
    ledger.set_job(pay_schedule="weekly")
    assert checkins.next_gap() is None or checkins.next_gap().key not in ("pay_rate", "tax", "pay_schedule")


def test_open_questions_only_asks_real_questions(clock):
    """Statements filed in Open Questions (third-person facts) must not be texted as '...?'."""
    vault.ensure_vault()
    vault.remember("Has a Twilio number blocked by carriers; he is not interested in fixing it.",
                   area="open_questions", source="test")
    vault.remember("Which Daily Orbit handle is correct, dailyorbit.shorts or DailyOrbit-t5n?",
                   area="open_questions", source="test")
    qs = [g.question for g in checkins._open_questions()]
    assert len(qs) == 1 and qs[0].endswith("DailyOrbit-t5n?")
    assert not any("Twilio" in q for q in qs)
