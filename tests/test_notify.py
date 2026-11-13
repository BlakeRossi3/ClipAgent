"""Tests for notify.py: Twilio voice/SMS, approval flow, webhooks, quiet hours, summary."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

import budget
import config
import db
import notify

OWNER = "+15551234567"
FROM = "+15550001111"
TOKEN = "test-auth-token"
BASE = "https://agent.example.com"


# ---------------------------------------------------------------------------
# fakes / fixtures
# ---------------------------------------------------------------------------

@dataclass
class _Res:
    sid: str


@dataclass
class _Endpoint:
    prefix: str
    created: list[dict[str, Any]] = field(default_factory=list)
    error: Exception | None = None

    def create(self, **kw: Any) -> _Res:
        if self.error is not None:
            raise self.error
        self.created.append(kw)
        return _Res(f"{self.prefix}{len(self.created):04d}")


class FakeTwilio:
    def __init__(self) -> None:
        self.calls = _Endpoint("CA")
        self.messages = _Endpoint("SM")


@pytest.fixture
def fake_client() -> FakeTwilio:
    fake = FakeTwilio()
    notify.set_client(fake)
    yield fake
    notify.set_client(None)


@pytest.fixture
def live(fake_client, settings) -> FakeTwilio:
    """Twilio 'configured' and DRY_RUN off, with the fake client injected."""
    config.set_settings(settings.replace(
        dry_run=False, twilio_account_sid="AC" + "0" * 32, twilio_auth_token=TOKEN,
        twilio_from_number=FROM, owner_phone=OWNER, public_base_url=BASE))
    return fake_client


@pytest.fixture
def applied(monkeypatch) -> list[int]:
    """Stand-in strategist module recording apply_decision calls."""
    calls: list[int] = []
    mod = types.ModuleType("strategist")
    mod.apply_decision = lambda decision_id: calls.append(decision_id) or True  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "strategist", mod)
    return calls


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(notify.router)
    return TestClient(app)


def post(client: TestClient, path: str, data: dict[str, str], *, token: str | None = TOKEN,
         signature: str | None = None):
    headers = {}
    if signature is not None:
        headers["X-Twilio-Signature"] = signature
    elif token:
        headers["X-Twilio-Signature"] = RequestValidator(token).compute_signature(BASE + path, data)
    return client.post(path, data=data, headers=headers)


def call_params(**kw: str) -> dict[str, str]:
    base = {"CallSid": "CA0001", "To": OWNER, "From": FROM, "Direction": "outbound-api", "AccountSid": "AC1"}
    base.update(kw)
    return base


def rows(status: str | None = None) -> list[db.Notification]:
    return db.list_notifications(status, limit=None)


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------

def test_speakable_strips_urls_symbols_and_money():
    out = notify.speakable("Spent $1.50 (75%) on #Shorts & more: see https://example.com/x?y=1 or www.foo.org now")
    assert "http" not in out and "www" not in out and "example.com" not in out
    assert "a link" in out
    assert "1 dollar 50" in out and "75 percent" in out
    assert "#" not in out and "&" not in out and " and " in out
    assert notify.speakable("Cost $0.42 today") == "Cost 42 cents today"
    assert "number 12" in notify.speakable("Approve #12?")


def test_speakable_truncates_at_sentence():
    text = " ".join(f"Sentence number {i} is here." for i in range(200))
    out = notify.speakable(text, max_chars=300)
    assert len(out) < 360
    assert out.endswith("More details are on the dashboard.")


def test_normalize_phone():
    assert notify.normalize_phone("(555) 123-4567") == OWNER
    assert notify.normalize_phone("1-555-123-4567") == OWNER
    assert notify.normalize_phone("+1 555 123 4567") == OWNER
    assert notify.normalize_phone("") == ""


def test_spoken_number():
    assert notify.spoken_number(950) == "950"
    assert notify.spoken_number(12345) == "12.3 thousand"
    assert notify.spoken_number(2_000_000) == "2 million"


# ---------------------------------------------------------------------------
# sending
# ---------------------------------------------------------------------------

def test_dry_run_records_and_sends_nothing(fake_client, clock):
    nid = notify.notify("weekly_report", "Report ready")
    cid = notify.call("Hello there")
    assert {r.id: r.status for r in rows()} == {nid: "dry_run", cid: "dry_run"}
    assert fake_client.calls.created == [] and fake_client.messages.created == []
    assert any("[dry run]" in e.message for e in db.list_events(module="notify"))


def test_unconfigured_twilio_is_dry_run(fake_client, settings, clock):
    config.set_settings(settings.replace(dry_run=False))
    nid = notify.sms("hi")
    assert rows()[0].id == nid and rows()[0].status == "dry_run"
    assert fake_client.messages.created == []


def test_sms_live(live, clock):
    nid = notify.sms("Hello Blake", kind="test")
    msg = live.messages.created[0]
    assert msg["to"] == OWNER and msg["from_"] == FROM and msg["body"] == "Hello Blake"
    row = rows()[0]
    assert row.id == nid and row.status == "sent" and row.sid == "SM0001" and row.channel == "sms"


def test_call_live_uses_say_webhook_and_status_callback(live, clock):
    nid = notify.call("Something happened", kind="info")
    kw = live.calls.created[0]
    assert kw["url"] == f"{BASE}/twilio/say/{nid}"
    assert kw["status_callback"] == f"{BASE}/twilio/status?notification_id={nid}"
    assert kw["machine_detection"] == "DetectMessageEnd"
    assert kw["to"] == OWNER and kw["from_"] == FROM
    assert rows()[0].status == "sent" and rows()[0].sid == "CA0001"


def test_urgent_call_failure_falls_back_to_sms(live, clock):
    live.calls.error = RuntimeError("twilio down")
    notify.notify("upload_failed", "Upload of clip 4 failed", urgent=True)
    by_channel = {r.channel: r for r in rows()}
    assert by_channel["call"].status == "failed" and "twilio down" in by_channel["call"].error
    assert by_channel["sms"].status == "sent"
    assert "Upload of clip 4 failed" in live.messages.created[0]["body"]


def test_non_urgent_notify_is_sms(live, clock):
    notify.notify("weekly_report", "Weekly report is ready")
    assert live.calls.created == [] and len(live.messages.created) == 1


# ---------------------------------------------------------------------------
# say / voice TwiML
# ---------------------------------------------------------------------------

def test_say_webhook_reads_notification_without_urls(live, client, clock):
    nid = notify.call("Budget hit. Details at https://dash.example.com/x", kind="info")
    r = post(client, f"/twilio/say/{nid}", call_params(AnsweredBy="human"))
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    assert "Budget hit" in r.text and "https://dash" not in r.text and "a link" in r.text
    assert 'voice="Polly.Joanna-Neural"' in r.text and "<Hangup" in r.text


def test_request_approval_places_call_and_voice_twiml(live, client, clock):
    did = notify.request_approval("new_niche", "Switch the niche to space weather?", {"action": "x"})
    d = db.get_decision(did)
    assert d.status == "pending" and d.call_sid == "CA0001"
    assert db.parse_iso(d.expires_at) == clock.now + timedelta(hours=48)
    kw = live.calls.created[0]
    assert kw["url"] == f"{BASE}/twilio/voice/{did}" and kw["machine_detection"] == "Enable"

    r = post(client, f"/twilio/voice/{did}", call_params(AnsweredBy="human"))
    assert r.status_code == 200
    assert '<Gather action="https://agent.example.com/twilio/gather/%d?attempt=1"' % did in r.text
    assert 'numDigits="1"' in r.text and "Switch the niche to space weather" in r.text
    assert "Press 1 to approve, 2 to reject, or 9 to hear this again." in r.text
    assert "<Redirect" in r.text


# ---------------------------------------------------------------------------
# keypad approvals
# ---------------------------------------------------------------------------

@pytest.fixture
def approval(live, clock) -> int:
    return notify.request_approval("new_source_type", "Add DVIDS as a source?", {"action": "apply_strategy"})


def test_keypad_approve(approval, client, applied):
    r = post(client, f"/twilio/gather/{approval}?attempt=1", call_params(Digits="1"))
    assert "Approved" in r.text
    d = db.get_decision(approval)
    assert d.status == "approved" and d.resolved_via == "call"
    assert applied == [approval]
    # a second answer is refused politely and never re-applies
    r2 = post(client, f"/twilio/gather/{approval}?attempt=1", call_params(Digits="2"))
    assert "already approved" in r2.text
    assert db.get_decision(approval).status == "approved" and applied == [approval]


def test_keypad_reject(approval, client, applied):
    r = post(client, f"/twilio/gather/{approval}?attempt=1", call_params(Digits="2"))
    assert "Rejected" in r.text
    assert db.get_decision(approval).status == "rejected" and applied == []


def test_keypad_repeat_and_invalid_then_timeout_falls_back_to_sms(approval, client, live, applied):
    r = post(client, f"/twilio/gather/{approval}?attempt=1", call_params(Digits="9"))
    assert "Add DVIDS as a source" in r.text and "attempt=2" in r.text
    r = post(client, f"/twilio/gather/{approval}?attempt=2", call_params(Digits="7"))
    assert "not an option" in r.text and "attempt=3" in r.text
    r = post(client, f"/twilio/gather/{approval}?attempt=3", call_params())  # timeout (Redirect, no Digits)
    assert "text" in r.text and "<Hangup" in r.text
    assert db.get_decision(approval).status == "pending"
    assert len(live.messages.created) == 1
    assert f"Reply YES {approval} or NO {approval}" in live.messages.created[0]["body"]
    # the status callback afterwards must not send a second fallback
    post(client, "/twilio/status?notification_id=1", call_params(CallStatus="completed", AnsweredBy="human"))
    assert len(live.messages.created) == 1


def test_keypad_timeout_retries_before_fallback(approval, client, live):
    r = post(client, f"/twilio/gather/{approval}?attempt=1", call_params())
    assert "didn't hear" in r.text and "attempt=2" in r.text
    assert live.messages.created == []


def test_keypad_refused_when_call_not_to_owner(approval, client, applied):
    r = post(client, f"/twilio/gather/{approval}?attempt=1", call_params(Digits="1", To="+15559998888"))
    assert "can't accept" in r.text
    assert db.get_decision(approval).status == "pending" and applied == []


def test_keypad_refused_when_call_sid_mismatch(approval, client, applied):
    r = post(client, f"/twilio/gather/{approval}?attempt=1", call_params(Digits="1", CallSid="CA9999"))
    assert "can't accept" in r.text and db.get_decision(approval).status == "pending"


def test_machine_answered_approval_never_approves(approval, client, live, applied):
    r = post(client, f"/twilio/voice/{approval}", call_params(AnsweredBy="machine_start"))
    assert "<Gather" not in r.text and "<Hangup" in r.text
    assert len(live.messages.created) == 1 and "Reply YES" in live.messages.created[0]["body"]
    r = post(client, f"/twilio/gather/{approval}?attempt=1", call_params(Digits="1"))
    assert "can't accept" in r.text
    assert db.get_decision(approval).status == "pending" and applied == []


def test_expired_decision_refused(approval, client, clock, applied):
    clock.advance(hours=49)
    r = post(client, f"/twilio/gather/{approval}?attempt=1", call_params(Digits="1"))
    assert "already expired" in r.text
    assert db.get_decision(approval).status == "expired" and applied == []


# ---------------------------------------------------------------------------
# status callback
# ---------------------------------------------------------------------------

def test_status_no_answer_approval_falls_back_to_sms(approval, client, live):
    r = post(client, "/twilio/status?notification_id=1", call_params(CallStatus="no-answer"))
    assert r.status_code == 200
    call_row = next(n for n in rows() if n.channel == "call")
    assert call_row.status == "failed" and call_row.error == "call no-answer" and call_row.sid == "CA0001"
    assert len(live.messages.created) == 1 and f"YES {approval}" in live.messages.created[0]["body"]


def test_status_busy_info_call_texts_message(live, client, clock):
    nid = notify.call("Copyright claim on video X", kind="copyright_claim")
    post(client, f"/twilio/status?notification_id={nid}", call_params(CallStatus="busy"))
    post(client, f"/twilio/status?notification_id={nid}", call_params(CallStatus="busy"))  # duplicate callback
    assert len(live.messages.created) == 1
    assert live.messages.created[0]["body"].startswith("Missed call from your Shorts agent: Copyright claim")


def test_status_completed_by_human_info_call_no_sms(live, client, clock):
    nid = notify.call("FYI", kind="info")
    post(client, f"/twilio/status?notification_id={nid}", call_params(CallStatus="completed", AnsweredBy="human"))
    assert live.messages.created == []


def test_status_voicemail_info_call_texts(live, client, clock):
    nid = notify.call("FYI", kind="info")
    post(client, f"/twilio/status?notification_id={nid}",
         call_params(CallStatus="completed", AnsweredBy="machine_end_beep"))
    assert len(live.messages.created) == 1


# ---------------------------------------------------------------------------
# inbound SMS
# ---------------------------------------------------------------------------

def sms_params(body: str, sender: str = OWNER) -> dict[str, str]:
    return {"From": sender, "To": FROM, "Body": body, "MessageSid": "SM1", "AccountSid": "AC1"}


def test_sms_yes_with_id(approval, client, applied):
    r = post(client, "/twilio/sms", sms_params(f"yes {approval}", sender="(555) 123-4567"))
    assert "<Message>" in r.text and "approved" in r.text
    d = db.get_decision(approval)
    assert d.status == "approved" and d.resolved_via == "sms" and applied == [approval]
    r = post(client, "/twilio/sms", sms_params(f"NO {approval}"))
    assert "already approved" in r.text and applied == [approval]


def test_sms_bare_no_with_single_pending(approval, client, applied):
    r = post(client, "/twilio/sms", sms_params("No"))
    assert "rejected" in r.text and db.get_decision(approval).status == "rejected"


def test_sms_bare_yes_ambiguous(approval, client, applied):
    other = db.add_decision("other", "Second thing?", {})
    r = post(client, "/twilio/sms", sms_params("YES"))
    assert "Several approvals" in r.text and f"#{other}" in r.text
    assert db.get_decision(approval).status == "pending" and applied == []


def test_sms_bare_yes_nothing_pending(live, client, clock):
    assert "Nothing is waiting" in post(client, "/twilio/sms", sms_params("yes")).text


def test_sms_from_stranger_ignored(approval, client, applied):
    r = post(client, "/twilio/sms", sms_params(f"YES {approval}", sender="+15550009999"))
    assert r.status_code == 200 and "<Message" not in r.text
    assert db.get_decision(approval).status == "pending" and applied == []
    ev = db.list_events(module="notify", level="warning")
    assert any("non-owner" in e.message for e in ev)
    assert all("+15550009999" not in str(e.data) for e in ev)


def test_sms_expired_decision(approval, client, clock, applied):
    clock.advance(hours=48, minutes=1)
    r = post(client, "/twilio/sms", sms_params(f"YES {approval}"))
    assert "already expired" in r.text and applied == []


def test_sms_pause_resume_status_help(live, client, clock):
    r = post(client, "/twilio/sms", sms_params("pause"))
    assert "Paused" in r.text and budget.is_paused()
    r = post(client, "/twilio/sms", sms_params("STATUS"))
    assert "Uploads today 0/" in r.text and "PAUSED" in r.text and "Pending: none" in r.text
    r = post(client, "/twilio/sms", sms_params("Resume"))
    assert "Resumed" in r.text and not budget.is_paused()
    r = post(client, "/twilio/sms", sms_params("help"))
    assert "YES &lt;id&gt;" in r.text and "PAUSE" in r.text
    assert "Commands" in post(client, "/twilio/sms", sms_params("what?")).text


# ---------------------------------------------------------------------------
# signature validation
# ---------------------------------------------------------------------------

def test_signature_required_when_token_configured(live, client, clock):
    data = sms_params("STATUS")
    assert post(client, "/twilio/sms", data, token=None).status_code == 403
    assert post(client, "/twilio/sms", data, signature="bogus").status_code == 403
    assert post(client, "/twilio/sms", data, token="wrong-token").status_code == 403
    tampered = RequestValidator(TOKEN).compute_signature(BASE + "/twilio/sms", data)
    assert client.post("/twilio/sms", data={**data, "Body": "PAUSE"},
                       headers={"X-Twilio-Signature": tampered}).status_code == 403
    assert post(client, "/twilio/sms", data).status_code == 200
    assert not budget.is_paused()


def test_signature_uses_public_url_with_query(approval, client):
    path = f"/twilio/gather/{approval}?attempt=1"
    data = call_params(Digits="2")
    # signed against the internal URL (what the app sees) instead of the public one -> rejected
    bad = RequestValidator(TOKEN).compute_signature("http://testserver" + path, data)
    assert post(client, path, data, signature=bad).status_code == 403
    assert post(client, path, data).status_code == 200


def test_no_token_refused_when_live(settings, client, clock):
    config.set_settings(settings.replace(dry_run=False, owner_phone=OWNER))
    assert post(client, "/twilio/sms", sms_params("STATUS"), token=None).status_code == 403


def test_no_token_allowed_in_dry_run(settings, client, clock):
    config.set_settings(settings.replace(owner_phone=OWNER))
    r = post(client, "/twilio/sms", sms_params("STATUS"), token=None)
    assert r.status_code == 200 and "Uploads today" in r.text


# ---------------------------------------------------------------------------
# handle_decision (dashboard path)
# ---------------------------------------------------------------------------

def test_handle_decision_idempotent(applied, clock):
    did = db.add_decision("other", "Do it?", {})
    assert notify.handle_decision(did, True, via="dashboard") is True
    assert notify.handle_decision(did, True, via="dashboard") is False
    assert notify.handle_decision(did, False, via="sms") is False
    assert applied == [did] and db.get_decision(did).status == "approved"
    assert notify.handle_decision(9999, True, via="cli") is False


def test_handle_decision_expired(applied, clock):
    did = db.add_decision("other", "Do it?", {}, expires_at=db.to_iso(clock.now - timedelta(minutes=1)))
    assert notify.handle_decision(did, True, via="dashboard") is False
    assert db.get_decision(did).status == "expired" and applied == []


def test_handle_decision_without_strategist(monkeypatch, clock):
    monkeypatch.setitem(sys.modules, "strategist", None)  # import raises ImportError
    did = db.add_decision("other", "Do it?", {})
    assert notify.handle_decision(did, True, via="dashboard") is True
    assert db.get_decision(did).status == "approved"
    assert any("failed" in e.message for e in db.list_events(module="notify", level="error"))


# ---------------------------------------------------------------------------
# quiet hours, de-dup, send_queued
# ---------------------------------------------------------------------------

NIGHT = datetime(2026, 11, 14, 4, 0, tzinfo=UTC)      # 23:00 New York
MORNING = datetime(2026, 11, 14, 13, 30, tzinfo=UTC)  # 08:30 New York


def test_in_quiet_hours(clock):
    assert not notify.in_quiet_hours()
    clock.set(NIGHT)
    assert notify.in_quiet_hours()
    clock.set(datetime(2026, 11, 14, 12, 59, tzinfo=UTC))  # 07:59
    assert notify.in_quiet_hours()
    clock.set(datetime(2026, 11, 14, 13, 0, tzinfo=UTC))  # 08:00
    assert not notify.in_quiet_hours()


def test_quiet_hours_queue_then_deliver_once(live, clock):
    clock.set(NIGHT)
    nid = notify.notify("weekly_report", "Report ready")
    unid = notify.notify("info", "Not that urgent", urgent=True)  # urgent but not a problem kind
    assert {r.id: r.status for r in rows()} == {nid: "queued", unid: "queued"}
    assert notify.send_queued() == 0 and live.messages.created == [] and live.calls.created == []
    clock.set(MORNING)
    assert notify.send_queued() == 2
    assert len(live.messages.created) == 1 and len(live.calls.created) == 1
    assert notify.send_queued() == 0
    assert len(live.messages.created) == 1 and len(live.calls.created) == 1
    assert {r.status for r in rows()} == {"sent"}


def test_urgent_problem_calls_at_night_but_deduplicated(live, clock):
    clock.set(NIGHT)
    notify.notify("quota_exhausted", "YouTube quota is used up", urgent=True)
    assert len(live.calls.created) == 1
    notify.notify("quota_exhausted", "YouTube quota is used up", urgent=True)
    assert len(live.calls.created) == 1
    assert rows("queued")[0].channel == "sms"  # repeat becomes a text, held until morning
    clock.set(MORNING)  # 9.5 h later: dedup window (6 h) has passed
    notify.notify("quota_exhausted", "Still out of quota", urgent=True)
    assert len(live.calls.created) == 2


def test_urgent_dedup_daytime_texts(live, clock):
    notify.notify("copyright_claim", "Claim on video A", urgent=True)
    clock.advance(hours=2)
    notify.notify("copyright_claim", "Claim on video B", urgent=True)
    assert len(live.calls.created) == 1 and len(live.messages.created) == 1
    assert live.messages.created[0]["body"] == "Claim on video B"


def test_send_queued_delivers_budget_cap_from_budget_module(live, clock, settings):
    budget.record_usage(settings.sonnet_model, "test.burn", input_tokens=10_000_000, output_tokens=0, fake=True)
    queued = rows("queued")
    assert [r.kind for r in queued] == ["budget_cap"]
    assert notify.send_queued() == 1
    kw = live.calls.created[0]
    assert kw["url"] == f"{BASE}/twilio/say/{queued[0].id}"
    assert rows()[0].status == "sent"
    assert notify.send_queued() == 0 and len(live.calls.created) == 1


def test_send_queued_budget_cap_at_night_and_dry_run(fake_client, clock):
    clock.set(NIGHT)
    db.add_notification("budget_cap", "Budget used up", "call")
    db.add_notification("daily_summary", "Summary", "call")
    assert notify.send_queued() == 1  # urgent problem kind goes out even at night
    statuses = {r.kind: r.status for r in rows()}
    assert statuses == {"budget_cap": "dry_run", "daily_summary": "queued"}
    assert fake_client.calls.created == []


def test_send_queued_skips_resolved_decisions(live, clock, applied):
    clock.set(NIGHT)
    did = notify.request_approval("budget_increase", "Raise the budget to 2 dollars?", {})
    assert rows("queued")[0].decision_id == did and live.calls.created == []
    notify.handle_decision(did, False, via="dashboard")
    clock.set(MORNING)
    assert notify.send_queued() == 0
    assert rows()[0].status == "skipped" and live.calls.created == []


def test_send_queued_places_deferred_approval_call(live, clock):
    clock.set(NIGHT)
    did = notify.request_approval("budget_increase", "Raise the budget?", {})
    clock.set(MORNING)
    assert notify.send_queued() == 1
    assert live.calls.created[0]["url"] == f"{BASE}/twilio/voice/{did}"
    assert db.get_decision(did).call_sid == "CA0001"


# ---------------------------------------------------------------------------
# daily summary
# ---------------------------------------------------------------------------

def test_daily_summary_content(clock, make_clip, make_upload, settings, fake_client):
    today = db.local_today()
    yday = today - timedelta(days=1)
    c1 = make_clip(status="rendered", title="Artemis rollout in 60 seconds")
    c2 = make_clip(status="rendered", title="Senate hearing moment")
    make_upload(c1, video_id="vidA")
    make_upload(c2, video_id="vidB")
    db.upsert_stat("vidA", yday, views=12_400, subscribers_gained=5)
    db.upsert_stat("vidB", yday, views=800)
    db.upsert_channel_stat(yday, subscribers=100, total_views=50_000, video_count=10)
    db.upsert_channel_stat(today, subscribers=112, total_views=63_500, video_count=12)
    budget.record_usage(settings.haiku_model, "test.spend", input_tokens=100_000, output_tokens=10_000, fake=True)
    db.add_decision("new_niche", "New niche?", {})

    text = notify.daily_summary()
    assert "Hi Blake" in text
    assert "posted 2 shorts today" in text
    assert "Artemis rollout in 60 seconds" in text and "12.4 thousand views" in text
    assert "gained 13.5 thousand views since yesterday" in text
    assert "12 new subscribers" in text
    assert "AI spend today was" in text and "cents of your 1 dollar cap" in text
    assert "1 approval is waiting" in text
    assert "No problems today." in text
    words = len(text.split())
    assert 40 <= words <= 110  # ~20-30 s spoken
    row = rows()[0]
    assert row.kind == "daily_summary" and row.channel == "call" and row.status == "dry_run" and row.body == text


def test_daily_summary_quiet_day_and_problems(clock, make_clip, make_upload, fake_client):
    up = make_upload(make_clip(status="rendered"), succeeded=False)
    db.mark_upload_failed(up, "quota")
    budget.set_paused(True, "test")
    text = notify.daily_summary()
    assert "No shorts were posted today." in text
    assert "1 failed upload" in text and "AI spending is paused" in text
    assert "http" not in text
