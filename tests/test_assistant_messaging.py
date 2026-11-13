"""assistant.messaging + notify WhatsApp routes: channel choice, 24-hour window holds, 30034 auto
switch, inbound WhatsApp security (signature, owner-only with the whatsapp: prefix, MessageSid de-dup,
id-required approvals), media download, and the doctor's WhatsApp error -> fix mapping. Offline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

import config
import db
import notify
from assistant import messaging
from assistant import twilio_doctor as td

OWNER = "+15551234567"
FROM = "+15550001111"
WA_FROM = "+14155238886"
TOKEN = "test-auth-token"
BASE = "https://agent.example.com"


class TwilioErr(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"HTTP 400 error: Unable to create record: Twilio error {code}")
        self.code = code


@dataclass
class _Res:
    sid: str


@dataclass
class _Endpoint:
    created: list[dict[str, Any]] = field(default_factory=list)
    errors: list[Exception] = field(default_factory=list)

    def create(self, **kw: Any) -> _Res:
        if self.errors:
            raise self.errors.pop(0)
        self.created.append(kw)
        return _Res(f"SM{len(self.created):04d}")


class FakeTwilio:
    def __init__(self) -> None:
        self.calls = _Endpoint()
        self.messages = _Endpoint()


@pytest.fixture
def live(settings, clock):
    fake = FakeTwilio()
    notify.set_client(fake)
    config.set_settings(settings.replace(
        dry_run=False, twilio_account_sid="AC" + "0" * 32, twilio_auth_token=TOKEN, twilio_from_number=FROM,
        owner_phone=OWNER, public_base_url=BASE, whatsapp_from=WA_FROM, messaging_channel="auto",
        quiet_hours_start="00:00", quiet_hours_end="00:00"))
    db.kv_set(messaging.K_A2P_CHECK, db.now_iso())    # no A2P network refresh in tests
    yield fake
    notify.set_client(None)
    messaging.set_media_fetcher(None)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(notify.router)
    return TestClient(app)


def post(c: TestClient, path: str, data: dict[str, str], *, sign: bool = True):
    headers = {"X-Twilio-Signature": RequestValidator(TOKEN).compute_signature(BASE + path, data)} if sign else {}
    return c.post(path, data=data, headers=headers)


def wa_in(body: str, sid: str = "SMin1", frm: str = f"whatsapp:{OWNER}", **kw: str) -> dict[str, str]:
    return {"From": frm, "To": f"whatsapp:{WA_FROM}", "Body": body, "MessageSid": sid, "NumMedia": "0", **kw}


def last_row() -> db.Notification:
    return db.list_notifications(None, limit=None)[-1]


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------

def test_route_auto_rules(live):
    config.set_settings(config.get_settings().replace(whatsapp_from=""))
    assert messaging.route_text() == messaging.Route("sms", reason="WhatsApp not set up")
    config.set_settings(config.get_settings().replace(whatsapp_from=WA_FROM))
    r = messaging.route_text()                          # SMS unknown, window closed -> hold
    assert r.transport == "whatsapp" and r.hold and "window is closed" in r.reason
    messaging.record_inbound_whatsapp("hi")
    assert messaging.route_text() == messaging.Route("whatsapp", reason="auto: SMS unknown")
    messaging.set_a2p_status("VERIFIED")                # campaign approved -> SMS
    assert messaging.route_text().transport == "sms"
    config.set_settings(config.get_settings().replace(messaging_channel="sms"))
    db.kv_delete(messaging.K_A2P)
    assert messaging.route_text().transport == "sms"


def test_whatsapp_send_in_window_uses_prefixed_addresses(live):
    messaging.record_inbound_whatsapp("hi")
    nid = notify.sms("Hello Blake", kind="test")
    kw = live.messages.created[0]
    assert kw["to"] == f"whatsapp:{OWNER}" and kw["from_"] == f"whatsapp:{WA_FROM}" and kw["body"] == "Hello Blake"
    assert kw["status_callback"] == f"{BASE}/twilio/message-status?notification_id={nid}"
    assert db.get_notification(nid).status == "sent" and messaging.transport_of(nid) == "whatsapp"


def test_outside_window_holds_then_sends_after_next_message(live, client, clock):
    nid = notify.notify("weekly_report", "Your report")
    row = db.get_notification(nid)
    assert row.status == "queued" and row.error.startswith("held:") and live.messages.created == []
    assert notify.send_queued() == 0 and live.messages.created == []          # still held
    r = post(client, "/twilio/whatsapp", wa_in("STATUS"))
    assert r.status_code == 200 and "<Message>" in r.text                     # command answered inline
    assert messaging.window_open()
    # the webhook queued send_queued as a background task (TestClient runs it)
    assert db.get_notification(nid).status == "sent"
    assert live.messages.created[-1]["to"] == f"whatsapp:{OWNER}"


def test_whatsapp_mode_falls_back_to_sms_outside_window_when_sms_works(live):
    config.set_settings(config.get_settings().replace(messaging_channel="whatsapp"))
    messaging.set_a2p_status("VERIFIED")
    notify.sms("Ping")
    assert live.messages.created[0]["to"] == OWNER and live.messages.created[0]["from_"] == FROM


def test_held_rows_expire_after_48h(live, clock):
    nid = notify.sms("Old news")
    assert db.get_notification(nid).status == "queued"
    clock.advance(hours=messaging.HOLD_MAX_HOURS + 1)
    notify.send_queued()
    assert db.get_notification(nid).status == "skipped" and live.messages.created == []


def test_create_63016_holds_and_closes_window(live):
    messaging.record_inbound_whatsapp("hi")
    live.messages.errors.append(TwilioErr(63016))
    nid = notify.sms("Hi")
    assert db.get_notification(nid).status == "queued" and messaging.is_held(nid)
    assert not messaging.window_open() and messaging.whatsapp_state()["last_error"]["code"] == 63016


def test_63015_marks_not_joined(live):
    messaging.record_inbound_whatsapp("hi")
    live.messages.errors.append(TwilioErr(63015))
    notify.sms("Hi")
    assert messaging.whatsapp_state()["state"] == "not_joined"
    messaging.record_inbound_whatsapp("join two-words")
    assert messaging.whatsapp_state()["state"] == "open" and messaging.whatsapp_state()["joined_at"]


def test_30034_status_callback_switches_auto_to_whatsapp(live, client):
    messaging.set_a2p_status("VERIFIED")
    messaging.record_inbound_whatsapp("hi")
    nid = notify.sms("Budget note")
    assert live.messages.created[0]["to"] == OWNER                           # SMS first
    path = f"/twilio/message-status?notification_id={nid}"
    r = post(client, path, {"MessageSid": "SM0001", "MessageStatus": "undelivered", "ErrorCode": "30034",
                            "To": OWNER, "From": FROM})
    assert r.status_code == 204
    assert messaging.sms_state() == "blocked" and messaging.route_text().transport == "whatsapp"
    assert notify.send_queued() == 1
    assert live.messages.created[-1]["to"] == f"whatsapp:{OWNER}" and live.messages.created[-1]["body"] == "Budget note"
    # requeued only once
    post(client, path, {"MessageSid": "SM0001", "MessageStatus": "undelivered", "ErrorCode": "30034", "To": OWNER})
    assert notify.send_queued() == 0
    # a later delivered SMS clears the block
    post(client, "/twilio/message-status", {"MessageSid": "SM9", "MessageStatus": "delivered", "To": OWNER})
    assert messaging.sms_state() == "ok"


def test_message_status_requires_signature(live, client):
    r = post(client, "/twilio/message-status", {"MessageSid": "SM1", "MessageStatus": "delivered"}, sign=False)
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# inbound WhatsApp security
# ---------------------------------------------------------------------------

def test_inbound_whatsapp_signature_owner_and_dedup(live, client):
    assert post(client, "/twilio/whatsapp", wa_in("STATUS"), sign=False).status_code == 403
    r = post(client, "/twilio/whatsapp", wa_in("STATUS", sid="SMx", frm="whatsapp:+15559999999"))
    assert r.status_code == 200 and "<Message>" not in r.text and not messaging.window_open()
    r = post(client, "/twilio/whatsapp", wa_in("STATUS", sid="SMa"))
    assert "<Message>" in r.text
    r = post(client, "/twilio/whatsapp", wa_in("STATUS", sid="SMa"))     # replay
    assert "<Message>" not in r.text
    # WhatsApp posted to the SMS URL works the same
    assert "<Message>" in post(client, "/twilio/sms", wa_in("STATUS", sid="SMb")).text


def test_inbound_whatsapp_approval_needs_id(live, client, applied_noop):
    did = db.add_decision("new_niche", "Try space content")
    r = post(client, "/twilio/whatsapp", wa_in("YES", sid="SMy"))
    assert "include the request number" in r.text and db.get_decision(did).status == "pending"
    r = post(client, "/twilio/whatsapp", wa_in(f"NO {did}", sid="SMz"))
    assert "rejected" in r.text and db.get_decision(did).status == "rejected"


@pytest.fixture
def applied_noop(monkeypatch):
    import sys
    import types

    mod = types.ModuleType("strategist")
    mod.apply_decision = lambda decision_id: True  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "strategist", mod)


# ---------------------------------------------------------------------------
# media
# ---------------------------------------------------------------------------

def test_inbound_media_downloaded_with_auth_and_handed_to_assistant(live, client, monkeypatch):
    got: list[tuple[str, tuple[str, str]]] = []

    def fetch(url, auth):
        got.append((url, auth))
        return (b"\x89PNG fake" if "img" in url else b"OggS fake"), ""

    messaging.set_media_fetcher(fetch)
    turns: list[tuple[str, Any]] = []
    from assistant import runtime

    monkeypatch.setattr(notify, "_assistant_enabled", lambda: True)
    monkeypatch.setattr(runtime, "run_turn", lambda text, **kw: turns.append((text, kw)))
    data = wa_in("what is this?", sid="SMm", NumMedia="3",
                 MediaUrl0="https://api.twilio.com/2010-04-01/Accounts/AC/Messages/MM/Media/img1",
                 MediaContentType0="image/jpeg",
                 MediaUrl1="https://api.twilio.com/2010-04-01/Accounts/AC/Messages/MM/Media/voice1",
                 MediaContentType1="audio/ogg",
                 MediaUrl2="https://evil.example.com/x.png", MediaContentType2="image/png")
    assert post(client, "/twilio/whatsapp", data).status_code == 200
    assert [u for u, _ in got] == [data["MediaUrl0"], data["MediaUrl1"]]          # evil host refused
    assert got[0][1] == ("AC" + "0" * 32, TOKEN)
    text, kw = turns[0]
    assert kw["channel"] == "sms" and kw["deliver"] is True
    (img,) = kw["attachments"]
    assert img.endswith(".jpg") and "assistant" in img and "uploads" in img
    assert text.startswith("what is this?") and "audio/ogg" in text and ".ogg" in text


# ---------------------------------------------------------------------------
# dry run + doctor
# ---------------------------------------------------------------------------

def test_dry_run_records_route_without_holding(settings, clock):
    config.set_settings(settings.replace(whatsapp_from=WA_FROM, owner_phone=OWNER, twilio_account_sid="AC" + "0" * 32,
                                         twilio_auth_token=TOKEN, twilio_from_number=FROM))   # DRY_RUN stays on
    nid = notify.sms("hi")
    assert db.get_notification(nid).status == "dry_run"
    ev = db.list_events(module="notify", limit=1)[0]
    assert ev.data["transport"] == "whatsapp" and ev.data["would_hold"] is True


def test_doctor_explains_whatsapp_codes():
    for code in (63007, 63015, 63016, 63018, 30034, 21608, 21610, 21211, 20003, 11200):
        e = td.explain(code)
        assert e and not e["cause"].startswith("Twilio error"), code
    assert "join" in td.explain(63015)["fix"] and "24 hours" in td.explain(63016)["cause"]


def test_doctor_channel_fix_and_texting_checks(tmp_path, monkeypatch, clock):
    cfg = tmp_path / "config.env"
    cfg.write_text("MESSAGING_CHANNEL=sms\nWHATSAPP_FROM=+14155238886\nOWNER_PHONE=+15551234567\n"
                   "TWILIO_ACCOUNT_SID=AC" + "0" * 32 + "\nTWILIO_AUTH_TOKEN=t\n")
    monkeypatch.setenv("CLIPAGENT_CONFIG_FILE", str(cfg))
    config.reset_settings()
    messaging.set_a2p_status("PENDING")
    d = td.diagnose(remote=False)
    ch = next(c for c in d.checks if c.id == "channel")
    assert ch.status == "fail" and ch.action == "channel_auto"
    assert next(c for c in d.checks if c.id == "whatsapp").status == "warn"     # not joined yet
    assert "auto" in td.apply_fix("channel_auto")
    assert "MESSAGING_CHANNEL=auto" in cfg.read_text() and config.get_settings().messaging_channel == "auto"


def test_doctor_detects_whatsapp_number_and_sms_block():
    from types import SimpleNamespace as NS

    d = td.Diagnosis(ok=True, checks=[], number={"phone_number": FROM},
                     a2p={"campaigns": [{"campaign_status": "IN_PROGRESS"}], "service": {"sid": "MG1"}})
    d.messages = [{"direction": "inbound", "to": "whatsapp:+14155550000", "from": f"whatsapp:{OWNER}"},
                  {"direction": "outbound-api", "from": FROM, "to": OWNER, "status": "undelivered", "error_code": 30034},
                  {"direction": "outbound-api", "to": f"whatsapp:{OWNER}", "error_code": 63016}]
    s = NS(whatsapp_from=WA_FROM, twilio_from_number=FROM)
    td._whatsapp(None, s, d)
    ids = {c.id: c for c in d.checks}
    assert ids["wa_from"].action == "use_detected_whatsapp_number" and "wa_err_63016" in ids
    assert messaging.sms_state() == "blocked" and messaging.a2p_status() == "IN_PROGRESS"
