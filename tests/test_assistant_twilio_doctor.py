"""assistant.twilio_doctor: local checks, fake Twilio client diagnosis, error table, fixes."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

import config
import db
from assistant import tools
from assistant import twilio_doctor as td
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env", "clean")
SID = "AC" + "0" * 32
NUM = "+15551230000"


@pytest.fixture
def clean():
    yield
    td.set_client(None)


def configure(monkeypatch, **kw):
    env = {"TWILIO_ACCOUNT_SID": SID, "TWILIO_AUTH_TOKEN": "tok", "TWILIO_FROM_NUMBER": NUM,
           "OWNER_PHONE": "+15559870000", "PUBLIC_BASE_URL": "https://me.example.net", **kw}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    config.reset_settings()


class Lister:
    def __init__(self, items, **ctx):
        self.items = items
        self.updated = []
        self.__dict__.update(ctx)

    def list(self, **kw):
        return list(self.items)


class FakeClient:
    def __init__(self, *, acct_type="Full", sms_url="https://old.example/sms", in_service=True, campaign="PENDING",
                 messages=(), caller_ids=(), auth_fail=False):
        self.updates: list = []
        self.auth_fail = auth_fail
        acc = NS(type=acct_type, status="active", friendly_name="x")
        self.api = NS(v2010=NS(accounts=lambda sid: NS(fetch=self._fetch(acc))))
        self.number = NS(sid="PN1", phone_number=NUM, capabilities={"sms": True, "voice": True, "mms": True},
                         sms_url=sms_url, sms_method="POST", sms_application_sid=None, voice_url="")
        client = self

        class Numbers(Lister):
            def __call__(self, sid):
                return NS(update=lambda **kw: client.updates.append(("number", sid, kw)))
        self.incoming_phone_numbers = Numbers([self.number])
        self.outgoing_caller_ids = Lister(list(caller_ids))
        self.messages = Lister(list(messages))
        self.calls = Lister([NS(status="completed", to="+15559870000", direction="outbound-api", start_time=None,
                                date_created=None)])
        svc = NS(sid="MG1", friendly_name="Sterling", us_app_to_person_registered=False,
                 use_inbound_webhook_on_number=False, inbound_request_url="https://old.example/svc")
        camps = [NS(sid="QE1", campaign_status=campaign, us_app_to_person_usecase="MIXED", errors=[])] if campaign else []

        class Services(Lister):
            def __call__(self, sid):
                return NS(phone_numbers=Lister([NS(phone_number=NUM)] if in_service else []),
                          us_app_to_person=Lister(camps),
                          update=lambda **kw: client.updates.append(("service", sid, kw)))
        self.messaging = NS(v1=NS(brand_registrations=Lister([NS(sid="BN1", status="APPROVED", failure_reason=None,
                                                                 brand_type="SOLE_PROPRIETOR")]),
                                  services=Services([svc])))

    def _fetch(self, acc):
        def fetch():
            if self.auth_fail:
                raise type("TwilioRestException", (Exception,), {"code": 20003, "status": 401})("Authenticate")
            return acc
        return fetch


def ids(d, status=None):
    return {c.id for c in d.checks if status is None or c.status == status}


def test_local_checks_without_credentials():
    d = td.diagnose()
    assert not d.ok and "config" in ids(d, "fail") and "public_url" in ids(d, "fail")  # localhost default
    assert "dry_run" in ids(d, "info")


def test_full_diagnosis_with_fake_client(monkeypatch):
    configure(monkeypatch)
    msgs = [NS(from_=NUM, to="+15559870000", status="undelivered", error_code=30034, error_message="Unregistered",
               direction="outbound-api", date_sent=None, date_created=None)] * 2
    td.set_client(FakeClient(messages=msgs))
    d = td.diagnose()
    assert d.account["type"] == "Full"
    assert "sms_webhook" in ids(d, "warn") and next(c for c in d.checks if c.id == "sms_webhook").action == "set_sms_webhook"
    assert "service_webhook" in ids(d, "warn")
    a2p = next(c for c in d.checks if c.id == "a2p")
    assert a2p.status == "warn" and "waiting" in a2p.title
    err = next(c for c in d.checks if c.id == "err_30034")
    assert "A2P 10DLC" in err.fix and err.docs.endswith("/30034")
    assert d.messages[0]["explain"]["code"] == "30034"
    assert "A2P" in d.summary()


def test_unregistered_number_and_trial(monkeypatch):
    configure(monkeypatch)
    td.set_client(FakeClient(acct_type="Trial", in_service=False, campaign=None, sms_url="https://me.example.net/twilio/sms"))
    d = td.diagnose()
    assert "a2p" in ids(d, "fail") and "trial_verified" in ids(d, "fail") and "sms_webhook" in ids(d, "pass")
    td.set_client(FakeClient(acct_type="Trial", caller_ids=[NS(phone_number="+15559870000")], campaign="VERIFIED"))
    d = td.diagnose()
    assert "trial" in ids(d, "warn") and "a2p" in ids(d, "pass")


def test_auth_failure_is_explained(monkeypatch):
    configure(monkeypatch)
    td.set_client(FakeClient(auth_fail=True))
    d = td.diagnose()
    auth = next(c for c in d.checks if c.id == "auth")
    assert auth.status == "fail" and "Auth Token" in auth.fix


def test_number_formats_and_normalise_fix(monkeypatch, tmp_path):
    cfg = tmp_path / "config.env"
    cfg.write_text("OWNER_PHONE=(555) 987-0000\nTWILIO_FROM_NUMBER=5551230000\n")
    monkeypatch.setenv("CLIPAGENT_CONFIG_FILE", str(cfg))
    monkeypatch.delenv("OWNER_PHONE", raising=False)
    config.reset_settings()
    d = td.diagnose(remote=False)
    fmt = next(c for c in d.checks if c.id == "formats")
    assert fmt.action == "normalise_numbers"
    assert "Rewrote" in td.apply_fix("normalise_numbers")
    text = cfg.read_text()
    assert "OWNER_PHONE=+15559870000" in text and "TWILIO_FROM_NUMBER=+15551230000" in text
    assert config.get_settings().owner_phone == "+15559870000"


def test_set_and_revert_webhook(monkeypatch):
    configure(monkeypatch)
    fc = FakeClient()
    td.set_client(fc)
    assert "now go to https://me.example.net/twilio/sms" in td.apply_fix("set_sms_webhook")
    assert fc.updates[-1] == ("number", "PN1", {"sms_url": "https://me.example.net/twilio/sms", "sms_method": "POST"})
    td.apply_fix("revert_sms_webhook")
    assert fc.updates[-1][2]["sms_url"] == "https://old.example/sms"
    td.apply_fix("service_use_number_webhook")
    assert fc.updates[-1] == ("service", "MG1", {"use_inbound_webhook_on_number": True})
    configure(monkeypatch, PUBLIC_BASE_URL="http://localhost:8000")
    with pytest.raises(td.FixError):
        td.apply_fix("set_sms_webhook")


def test_error_table():
    for code in (30034, 21608, 21614, 30003, 30005, 30006, 30007, 21211, 20003, 11200, 21610):
        e = td.explain(code)
        assert e and e["cause"] and e["fix"] and e["docs"].endswith(str(code))
    assert td.explain(99999)["docs"].endswith("99999") and td.explain(None) is None


def test_tools_and_approval_flow(monkeypatch):
    configure(monkeypatch)
    fc = FakeClient()
    td.set_client(fc)
    tools.load_extensions()
    ctx = tools.ToolContext()
    r = tools.dispatch("twilio_diagnose", {}, ctx, allowed={"twilio_diagnose"})
    assert r.ok and "problems" in r.content
    r = tools.dispatch("twilio_fix", {"action": "set_sms_webhook"}, ctx, allowed={"twilio_fix"})
    assert r.ok and not fc.updates
    did = db.list_decisions("pending")[0].id
    db.resolve_decision(did, "approved", via="sms")
    assert tools.apply_approved_decision(did)
    assert fc.updates and fc.updates[-1][0] == "number"
