"""End-to-end (offline) wiring: Friday hours question -> signed owner SMS -> ledger -> confirmation;
free-form texts go to the agent; pipeline commands and webhook security are unchanged."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

import budget
import config
import db
import notify
from assistant import checkins, ledger, store
from assistant import settings as aset
from assistant_fixtures import assistant_env, fake_model  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")

OWNER, TOKEN, BASE = "+15551234567", "test-auth-token", "https://agent.example.com"


@pytest.fixture
def env(settings, clock):
    config.set_settings(settings.replace(twilio_auth_token=TOKEN, owner_phone=OWNER, public_base_url=BASE))
    aset.set_assistant_settings(aset.get_assistant_settings().replace(enabled=True))
    app = FastAPI()
    app.include_router(notify.router)
    yield TestClient(app)
    aset.set_assistant_settings(None)


_n = iter(range(1, 10_000))


def sms(client, body, *, sender=OWNER, signed=True):
    data = {"From": sender, "To": "+15550001111", "Body": body, "MessageSid": f"SM{next(_n):06d}", "AccountSid": "AC1"}
    headers = {"X-Twilio-Signature": RequestValidator(TOKEN).compute_signature(BASE + "/twilio/sms", data)} \
        if signed else {}
    return client.post("/twilio/sms", data=data, headers=headers)


def test_friday_hours_flow(env, clock, fake_model):
    ledger.set_job("Warehouse", hourly_rate=25, est_tax_rate=0.2)
    clock.set(datetime(2026, 11, 13, 23, 45, tzinfo=UTC))           # Friday 18:45 New York
    ids = checkins.plan_day()
    assert checkins.plan_day() == []                                 # idempotent
    hours_q = [c for c in store.list_checkins() if c.kind == "weekly_hours"]
    assert [c.id for c in hours_q] and hours_q[0].id in ids
    out = checkins.send_due()
    assert out["sent"] >= 1
    chk = store.get_checkin(hours_q[0].id)
    assert chk.sent_at and "hours" in chk.question and "sir" in chk.question
    sent_before = db.list_notifications()
    assert any(n.status == "dry_run" and "hours" in n.body for n in sent_before)

    r = sms(env, "about 38 and a half")
    assert r.status_code == 200 and "<Message>" not in r.text        # empty TwiML; answered in background
    assert store.get_checkin(chk.id).status == "answered"
    wl = store.get_work_log(store.get_job_by_name("Warehouse").id, "2026-11-09")
    assert wl.hours == 38.5
    inc = store.list_income(source="job")
    assert len(inc) == 1 and inc[0].amount_usd == pytest.approx(962.50)
    reply = store.list_messages(1, direction="out")[0]
    assert reply.kind == "reply" and reply.body.startswith("Logged, sir: 38.5 hours, roughly $963 before tax")
    assert len(reply.body) <= 320 and reply.body.count("sir") == 1
    s = ledger.summary()
    assert s["this_week"]["hours"] == 38.5 and s["this_week"]["job_gross"] == 962.5
    assert s["this_week"]["job_net_estimate"] == 770.0

    # a correction replaces, never adds
    ledger.log_hours(40, week="2026-11-13")
    assert store.income_total(source="job") == 1000.0

    # free-form question -> conversational agent, reply recorded
    fake_model.queue("You worked 40 hours this week, sir.")
    r = sms(env, "how am I doing this week?")
    assert r.status_code == 200
    last = store.list_messages(1, direction="out")[0]
    assert last.body == "You worked 40 hours this week, sir." and last.agent == "main"


def test_pipeline_commands_and_security_unchanged(env, clock):
    r = sms(env, "PAUSE")
    assert "Paused" in r.text and budget.is_paused()
    r = sms(env, "RESUME")
    assert "Resumed" in r.text
    did = db.add_decision("other", "Try something", {"action": "x"})
    r = sms(env, "YES")                                                # id still required while pending
    assert f"YES {did}" in r.text
    r = sms(env, f"NO {did}")
    assert "rejected" in r.text and db.get_decision(did).status == "rejected"
    n_in = len(store.list_messages(100, direction="in"))
    assert sms(env, "hello", signed=False).status_code == 403
    assert sms(env, "hello", sender="+15559999999").text.count("<Message>") == 0
    assert len(store.list_messages(100, direction="in")) == n_in    # neither reached the assistant


def test_controls_quiet_hours_and_caps(env, clock):
    clock.set(datetime(2026, 11, 14, 4, 0, tzinfo=UTC))             # 23:00 local: quiet hours
    cid = store.add_checkin("weekly_hours", "How many hours, sir?", db.now_iso())
    assert checkins.send_due()["sent"] == 0 and store.get_checkin(cid).sent_at is None
    sms(env, "STOP CHECK-INS")
    assert store.get_checkin(cid).status == "skipped" and checkins.plan_day() == []
    sms(env, "START CHECK-INS")
    assert db.kv_get("checkins:stopped") is None
    # the assistant's own SMS cap: nudges lose to questions
    aset.set_assistant_settings(aset.get_assistant_settings().replace(max_sms_per_day=1))
    clock.set(datetime(2026, 11, 14, 17, 0, tzinfo=UTC))            # 12:00 local Saturday
    q = store.add_checkin("onboarding:pay_rate", "What is your hourly pay, sir?", db.now_iso())
    nudge = store.add_checkin("wellbeing", "How are you, sir?", db.now_iso())
    checkins.send_due()
    assert store.get_checkin(q).sent_at and store.get_checkin(nudge).status == "skipped"
    sms(env, "$27.50 an hour")
    assert store.list_jobs()[0].hourly_rate == 27.5


def test_assistant_spend_separate_from_pipeline(env, clock, fake_model):
    before = budget.spent_today()
    fake_model.queue("Noted, sir.")
    sms(env, "remind me about the dentist")
    assert store.assistant_spent_today() > 0 and budget.spent_today() == before
