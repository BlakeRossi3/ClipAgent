"""assistant.tunnel: restart a dead Cloudflare quick tunnel, save the new address, re-point Twilio."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

import config
import config_store
import db
from assistant import tunnel
from assistant import twilio_doctor as td
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env", "clean")

OLD = "https://old-dead-words.trycloudflare.com"
NEW = "https://fresh-new-words.trycloudflare.com"
NUM = "+15551230000"


@pytest.fixture
def clean():
    yield
    tunnel.reset_seams()
    td.set_client(None)


class Fake:
    """Programmable seams: which hosts are alive, what cloudflared prints."""

    def __init__(self, alive=(), prints: str | None = NEW, exe: str | None = "C:/cf/cloudflared.exe"):
        self.alive = set(alive)
        self.prints = prints
        self.spawned: list[list[str]] = []
        self.killed: list[int] = []
        tunnel.set_seams(alive=lambda url: url in self.alive, spawn=self.spawn, kill=self.killed.append,
                         sleep=lambda s: None, which=lambda: exe)

    def spawn(self, argv, logfile):
        self.spawned.append(argv)
        if self.prints:
            with open(logfile, "a", encoding="utf-8") as fh:
                fh.write('{"level":"info","message":"|  ' + self.prints + '   |"}\n')
        return 4242


def set_base(url):
    config_store.update({"PUBLIC_BASE_URL": url})


def test_not_a_quick_tunnel_is_left_alone():
    set_base("https://me.example.net")
    f = Fake()
    assert tunnel.ensure()["status"] == "skipped" and not f.spawned


def test_alive_quick_tunnel_is_left_alone():
    set_base(OLD)
    f = Fake(alive={OLD})
    assert tunnel.ensure()["status"] == "ok" and not f.spawned
    assert (db.kv_get(tunnel.KV) or {}).get("misses", 0) == 0


def test_one_miss_waits_second_miss_restarts_and_saves_new_address():
    set_base(OLD)
    f = Fake()
    assert tunnel.ensure()["status"] == "waiting" and not f.spawned  # one blip is not a dead tunnel
    r = tunnel.ensure()
    assert r["status"] == "restarted" and r["url"] == NEW and r["old"] == OLD
    assert f.spawned and "--url" in f.spawned[0] and f.spawned[0][-1].startswith("http://localhost:")
    assert config.get_settings().public_base_url == NEW
    st = db.kv_get(tunnel.KV)
    assert st["pid"] == 4242 and st["url"] == NEW and st["whatsapp_url_stale"] is True
    # a fresh tunnel gets time to become reachable before it is judged
    f.alive.clear()
    assert tunnel.ensure()["status"] == "starting" and len(f.spawned) == 1


def test_restart_kills_the_tunnel_it_started_before(clock):
    set_base(OLD)
    db.kv_set(tunnel.KV, {"pid": 99, "misses": 1, "started_at": "2020-01-01T00:00:00Z"})
    f = Fake()
    assert tunnel.ensure()["status"] == "restarted" and f.killed == [99]


def test_no_cloudflared_is_reported_not_crashed():
    set_base(OLD)
    db.kv_set(tunnel.KV, {"misses": 1})
    f = Fake(exe=None)
    r = tunnel.ensure()
    assert r["status"] == "failed" and "cloudflared" in r["why"] and not f.spawned


def test_cloudflared_prints_no_address():
    set_base(OLD)
    db.kv_set(tunnel.KV, {"misses": 1})
    Fake(prints=None)
    r = tunnel.ensure()
    assert r["status"] == "failed" and config.get_settings().public_base_url == OLD


def _client(sms_url):
    updates = []
    number = NS(sid="PN1", phone_number=NUM, sms_url=sms_url, sms_method="POST", sms_application_sid=None)

    class Numbers:
        def list(self, **kw):
            return [number]

        def __call__(self, sid):
            return NS(update=lambda **kw: updates.append(kw))
    return NS(incoming_phone_numbers=Numbers()), updates


def test_restart_repoints_the_numbers_sms_webhook_that_pointed_at_the_old_tunnel(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC" + "0" * 32)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", NUM)
    set_base(OLD)
    client, updates = _client(OLD + "/twilio/sms")
    td.set_client(client)
    db.kv_set(tunnel.KV, {"misses": 1})
    Fake()
    r = tunnel.ensure()
    assert r["status"] == "restarted" and updates == [{"sms_url": NEW + "/twilio/sms", "sms_method": "POST"}]
    assert "now go to" in r["sms_webhook"]


def test_restart_leaves_a_webhook_pointing_somewhere_else(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC" + "0" * 32)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", NUM)
    assert td.follow_base_change(OLD, client=_client("https://his-own-app.example/sms")[0]) is None


def test_doctor_explains_11210_and_flags_stale_whatsapp_url(monkeypatch):
    e = td.explain(11210)
    assert "address" in e["cause"].lower() and "/twilio/whatsapp" in e["fix"]
    set_base(NEW)
    db.kv_set(tunnel.KV, {"url": NEW, "whatsapp_url_stale": True})
    monkeypatch.setenv("WHATSAPP_FROM", "+14155238886")
    config.reset_settings()
    d = td.diagnose(remote=False)
    c = next(c for c in d.checks if c.id == "wa_webhook")
    assert NEW + "/twilio/whatsapp" in c.fix
    tunnel.whatsapp_url_updated()
    assert not any(c.id == "wa_webhook" for c in td.diagnose(remote=False).checks)
