"""Web setup wizard + Settings page (setup_web.py): redirect, steps, secrets, security, archive import,
YouTube OAuth, maintenance actions."""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import config
import config_store
import db
import llm
import main
import server
import setup_web
from assistant import store, vault
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")

BASE = "http://127.0.0.1:8000"
SECRET_KEY = "sk-ant-api03-SECRETVALUE1234567890"
TWILIO_TOKEN = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def wiz(tmp_path, monkeypatch) -> Path:
    """Fresh install: no config file, wizard not finished."""
    for key in ("SETUP_COMPLETE", "TIMEZONE", "DRY_RUN"):   # let the written file decide
        monkeypatch.delenv(key, raising=False)
    cfg = tmp_path / "cfgdir" / "config.env"
    monkeypatch.setenv("CLIPAGENT_CONFIG_FILE", str(cfg))
    config_store.reload()
    monkeypatch.setattr(main, "_control", {"server": None, "restart": False})
    return cfg


@pytest.fixture
def c(wiz) -> TestClient:
    return TestClient(server.create_app(), base_url=BASE)


def _csrf(client: TestClient) -> str:
    html = client.get("/setup/about").text
    m = re.search(r'name="csrf" value="([0-9a-f]+)"', html)
    assert m, html[:500]
    return m.group(1)


def _about(c, csrf, **kw):
    data = {"csrf": csrf, "owner_name": "Blake", "honorific": "sir", "assistant_name": "Sterling",
            "timezone": "America/Chicago", **kw}
    return c.post("/setup/about", data=data, follow_redirects=False)


# --------------------------------------------------------------------------- redirect + flow

def test_redirects_to_setup_until_finished(c, wiz):
    r = c.get("/", follow_redirects=False)
    assert r.headers["location"] == "/setup/"
    r = c.get("/dashboard/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup/"
    page = c.get("/setup/").text
    assert "Let's get you set up" in page and "Begin" in page
    csrf = _csrf(c)
    assert _about(c, csrf).headers["location"] == "/setup/brain"
    assert config.get_settings().setup_complete is False          # file exists but says false
    r = c.post("/setup/finish", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/dashboard/")
    assert config_store.read_values()["SETUP_COMPLETE"] == "true"
    assert c.get("/dashboard/", follow_redirects=False).status_code == 200
    assert c.get("/", follow_redirects=False).headers["location"] == "/dashboard/"
    hub = c.get("/setup/").text                                   # now the Settings page
    assert "Check for updates" in hub and "Begin" not in hub


def test_every_step_page_renders(c):
    for step in setup_web.STEP_KEYS:
        if step == "done":
            continue
        r = c.get(f"/setup/{step}")
        assert r.status_code == 200, step
        assert "Step " in r.text
    assert c.get("/setup/nope").status_code == 404


def test_about_validates_and_saves(c, wiz):
    csrf = _csrf(c)
    r = _about(c, csrf, timezone="Mars/Olympus", owner_name="")
    assert r.status_code == 400 and "time zone like" in r.text and "tell me your name" in r.text
    assert not wiz.exists()
    assert _about(c, csrf, owner_name="Blake  R").status_code == 303
    s = config.get_settings()
    assert (s.owner_name, s.timezone) == ("Blake R", "America/Chicago")
    from assistant.settings import get_assistant_settings
    assert get_assistant_settings().name == "Sterling" and get_assistant_settings().owner_honorific == "sir"


def test_brain_key_is_saved_tested_with_the_fake_and_never_shown(c, wiz):
    csrf = _csrf(c)
    r = c.post("/setup/brain", data={"csrf": csrf, "api_key": SECRET_KEY, "pipeline_cap": "3",
                                     "assistant_cap": "2", "assistant_on": "1", "action": "test"})
    assert r.status_code == 200 and "no real call" in r.text
    assert SECRET_KEY not in r.text
    vals = config_store.read_values()
    assert vals["ANTHROPIC_API_KEY"] == SECRET_KEY and vals["FAKE_LLM"] == "false"
    assert vals["MAX_DAILY_LLM_BUDGET_USD"] == "3.00" and vals["ASSISTANT_ENABLED"] == "true"
    page = c.get("/setup/brain").text
    assert "•••• saved" in page and SECRET_KEY not in page
    # a bad key is refused and not echoed back
    r = c.post("/setup/brain", data={"csrf": csrf, "api_key": "not-a-key-SECRET", "pipeline_cap": "3",
                                     "assistant_cap": "2"})
    assert r.status_code == 400 and "not-a-key-SECRET" not in r.text
    # clearing the key goes back to practice mode
    c.post("/setup/brain", data={"csrf": csrf, "clear_api_key": "1", "pipeline_cap": "1", "assistant_cap": "1"})
    vals = config_store.read_values()
    assert vals["ANTHROPIC_API_KEY"] == "" and vals["FAKE_LLM"] == "true"


def test_api_key_test_reports_problems(wiz, monkeypatch):
    assert setup_web.test_api_key()["title"] == "No key saved"
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET_KEY)
    config.reset_settings()

    class Unauthorized(Exception):
        status_code = 401

    def handler(req):
        raise llm.LLMError("Anthropic API error 401") from Unauthorized("invalid x-api-key")
    llm.set_fake_handler(handler)
    res = setup_web.test_api_key()
    assert not res["ok"] and "did not accept that key" in res["text"]


def test_money_step_sets_the_job(c):
    csrf = _csrf(c)
    r = c.post("/setup/money", data={"csrf": csrf, "job_name": "Target", "rate": "abc", "schedule": "weekly"})
    assert r.status_code == 400 and "hourly pay" in r.text
    r = c.post("/setup/money", data={"csrf": csrf, "job_name": "Target", "rate": "$18.50", "schedule": "weekly",
                                     "tax": "20"}, follow_redirects=False)
    assert r.status_code == 303
    job = store.list_jobs()[0]
    assert (job.name, job.hourly_rate, job.pay_schedule, round(job.est_tax_rate, 2)) == ("Target", 18.5, "weekly", 0.2)
    assert 'value="18.5"' in c.get("/setup/money").text


def test_phone_step_normalizes_and_test_text_respects_dry_run(c, clock):
    csrf = _csrf(c)
    r = c.post("/setup/phone", data={"csrf": csrf, "owner_phone": "555 123", "twilio_sid": "XX1"})
    assert r.status_code == 400 and "mobile number" in r.text and "starts with AC" in r.text
    r = c.post("/setup/phone", data={"csrf": csrf, "owner_phone": "(555) 123-4567", "twilio_sid": "AC" + "a" * 32,
                                     "twilio_token": TWILIO_TOKEN, "twilio_from": "+1 555 000 1111",
                                     "public_url": "https://agent.example.com", "action": "test"})
    assert r.status_code == 200 and "Recorded, not sent" in r.text and TWILIO_TOKEN not in r.text
    s = config.get_settings()
    assert (s.owner_phone, s.twilio_from_number, s.twilio_auth_token) == ("+15551234567", "+15550001111", TWILIO_TOKEN)
    assert db.list_notifications(limit=1)[0].status == "dry_run"
    assert TWILIO_TOKEN not in c.get("/setup/phone").text
    r = c.post("/setup/phone", data={"csrf": csrf, "owner_phone": "+15551234567",
                                     "public_url": "https://x.example.com/path?q=1"})
    assert r.status_code == 400


def test_power_step_and_phone_password_shown_once(c, wiz):
    csrf = _csrf(c)
    r = c.post("/setup/power", data={"csrf": csrf, "whisper_device": "cuda", "dry_run": "false"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup/done"
    assert (config.get_settings().whisper_device, config.get_settings().dry_run) == ("cuda", False)
    assert c.post("/setup/power", data={"csrf": csrf, "whisper_device": "tpu"}).status_code == 400
    r = c.post("/setup/power", data={"csrf": csrf, "action": "new_token"})
    token = re.search(r'class="s-token">([^<]+)<', r.text).group(1)
    assert config.get_settings().dashboard_token == token and len(token) >= 20
    assert "clipagent_dash" in r.headers.get("set-cookie", "") and "Path=/" in r.headers["set-cookie"]
    page = c.get("/setup/power")                       # still signed in through the cookie
    assert page.status_code == 200 and token not in page.text and "One is saved" in page.text
    fresh = TestClient(server.create_app(), base_url=BASE)
    assert fresh.get("/setup/power").status_code == 401   # a token now protects the page


def test_secrets_never_rendered_on_any_page(c, wiz):
    config_store.update({"ANTHROPIC_API_KEY": SECRET_KEY, "TWILIO_AUTH_TOKEN": TWILIO_TOKEN,
                         "SETUP_COMPLETE": "true"})
    for path in ["/setup/", *[f"/setup/{s}" for s in setup_web.STEP_KEYS if s != "done"]]:
        text = c.get(path).text
        assert SECRET_KEY not in text and TWILIO_TOKEN not in text, path
    events = json.dumps([e.to_dict() for e in db.list_events(limit=50)], default=str)
    assert SECRET_KEY not in events and TWILIO_TOKEN not in events


# --------------------------------------------------------------------------- security on every POST

POSTS = ["/setup/about", "/setup/brain", "/setup/money", "/setup/phone", "/setup/power", "/setup/finish",
         "/setup/action/restart", "/setup/action/stop", "/setup/action/update", "/setup/youtube/connect",
         "/setup/youtube/secret", "/setup/memory/import"]


@pytest.mark.parametrize("path", POSTS)
def test_every_post_needs_csrf_same_origin_and_a_local_or_authenticated_client(c, wiz, monkeypatch, path):
    ran = []
    monkeypatch.setattr(setup_web, "update_app", lambda: ran.append("update") or {"ok": True, "updated": False,
                                                                                  "log": []})
    monkeypatch.setattr(main, "request_shutdown", lambda **kw: ran.append(kw) or True)
    csrf = _csrf(c)
    data = {"owner_name": "X", "honorific": "sir", "assistant_name": "S", "timezone": "UTC"}
    assert c.post(path, data=data).status_code == 403                                   # no CSRF field
    assert c.post(path, data={**data, "csrf": "0" * 64}).status_code == 403              # wrong CSRF
    assert c.post(path, data={**data, "csrf": csrf},
                  headers={"Origin": "https://evil.example"}).status_code == 403         # cross-site
    proxied = {"X-Forwarded-For": "203.0.113.7"}
    assert c.post(path, data={**data, "csrf": csrf}, headers=proxied).status_code == 403  # not on this machine
    remote = TestClient(server.create_app(), base_url="https://agent.example.com")
    assert remote.post(path, data={**data, "csrf": csrf}).status_code == 403
    monkeypatch.setenv("DASHBOARD_ALLOW_UNAUTHENTICATED", "true")                       # never for /setup
    config.reset_settings()
    assert remote.post(path, data={**data, "csrf": csrf}, headers=proxied).status_code == 403
    assert remote.get("/setup/about", headers=proxied).status_code == 403
    assert ran == [] and not wiz.exists()


def test_bearer_token_opens_setup_remotely_and_trust_loopback(c, wiz, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "t" * 32)
    config.reset_settings()
    remote = TestClient(server.create_app(), base_url="https://agent.example.com")
    hdr = {"X-Forwarded-For": "203.0.113.7"}
    assert remote.get("/setup/about", headers=hdr).status_code == 401
    assert remote.get("/setup/about", headers={**hdr, "Authorization": "Bearer " + "t" * 32}).status_code == 200
    assert c.get("/setup/about").status_code == 401                 # token set: even the laptop needs it ...
    monkeypatch.setenv("DASHBOARD_TRUST_LOOPBACK", "true")         # ... unless the Windows launcher trusts it
    config.reset_settings()
    assert c.get("/setup/about").status_code == 200
    assert c.get("/dashboard/studio").status_code == 200
    assert remote.get("/setup/about", headers=hdr).status_code == 401   # proxied requests are not "loopback"


def test_upload_size_limit_and_missing_length(c, monkeypatch):
    csrf = _csrf(c)
    monkeypatch.setattr(setup_web, "MAX_ARCHIVE_BYTES", 1000)
    big = io.BytesIO(b"x" * 50_000)
    r = c.post("/setup/memory/import", data={"csrf": csrf}, files={"file": ("v.zip", big, "application/zip")})
    assert r.status_code == 413


# --------------------------------------------------------------------------- vault import

def _zip(files: dict[str, bytes], symlink: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
        if symlink:
            zi = zipfile.ZipInfo(symlink)
            zi.external_attr = (0o120777 << 16)
            zf.writestr(zi, "/etc/passwd")
    return buf.getvalue()


def _tar(files: dict[str, bytes], link: str | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        if link:
            ti = tarfile.TarInfo(link)
            ti.type = tarfile.SYMTYPE
            ti.linkname = "../../../etc/passwd"
            tf.addfile(ti)
    return buf.getvalue()


def test_import_merges_without_overwriting(assistant_env, clock):
    vault.ensure_vault()
    mine = vault.vault_root() / "People" / "Mom.md"
    mine.parent.mkdir(parents=True, exist_ok=True)
    mine.write_text("my own note\n", encoding="utf-8")
    data = _tar({"vault/Profile/About.md": b"# About\nBlake likes espresso.\n",
                 "vault/People/Mom.md": b"imported version\n",
                 "vault/People/Dad.md": b"# Dad\n",
                 "vault/.obsidian/app.json": b"{}", "vault/img.png": b"\x89PNG",
                 "vault/Journal/.gitkeep": b""})
    res = setup_web.import_vault_archive(data)
    root = vault.vault_root()
    assert (root / "Profile" / "About.md").read_text(encoding="utf-8").endswith("espresso.\n")  # template filled
    assert mine.read_text(encoding="utf-8") == "my own note\n"                                  # never overwritten
    assert (root / "People" / "Mom (imported).md").read_text(encoding="utf-8") == "imported version\n"
    assert (root / "People" / "Dad.md").is_file() and not (root / "vault").exists()            # wrapper stripped
    assert (res["added"], res["replaced_template"], res["kept_both"], res["refused_count"]) == (1, 1, 1, 2)
    assert any(h.path == "Profile/About.md" for h in vault.search("espresso"))                 # reindexed
    again = setup_web.import_vault_archive(data)                                                # idempotent
    assert again["added"] == 0 and again["kept_both"] == 0


@pytest.mark.parametrize("bad", [
    _zip({"notes/ok.md": b"x", "../evil.md": b"x"}),
    _zip({"/etc/evil.md": b"x"}),
    _zip({"C:/Windows/evil.md": b"x"}),
    _zip({"a/ok.md": b"x"}, symlink="a/link.md"),
    _tar({"a/ok.md": b"x"}, link="a/link.md"),
    _tar({"a/../../evil.md": b"x"}),
    b"not an archive at all",
])
def test_import_refuses_unsafe_archives_and_writes_nothing(assistant_env, tmp_path, bad):
    vault.ensure_vault()
    before = sorted(vault.list_notes())
    with pytest.raises(setup_web.ArchiveError):
        setup_web.import_vault_archive(bad)
    assert sorted(vault.list_notes()) == before
    assert not (tmp_path / "evil.md").exists() and not (vault.vault_root().parent / "evil.md").exists()


def test_import_refuses_oversized_notes_and_archives(assistant_env, monkeypatch):
    with pytest.raises(setup_web.ArchiveError, match="larger than a note"):
        setup_web.import_vault_archive(_zip({"big.md": b"x" * (vault.MAX_NOTE_BYTES + 1)}))
    monkeypatch.setattr(setup_web, "MAX_ARCHIVE_TOTAL", 10)
    with pytest.raises(setup_web.ArchiveError, match="too large in total"):
        setup_web.import_vault_archive(_zip({"a.md": b"x" * 8, "b.md": b"x" * 8}))
    with pytest.raises(setup_web.ArchiveError, match="No notes"):
        setup_web.import_vault_archive(_zip({"a.txt": b"x"}))


def test_import_route(c, clock):
    csrf = _csrf(c)
    r = c.post("/setup/memory/import", data={"csrf": csrf},
               files={"file": ("vault.zip", io.BytesIO(_zip({"People/Ann.md": b"# Ann\n"})), "application/zip")})
    assert r.status_code == 200 and "Memory imported" in r.text and "1 note added" in r.text
    r = c.post("/setup/memory/import", data={"csrf": csrf},
               files={"file": ("vault.zip", io.BytesIO(_zip({"../x.md": b"x"})), "application/zip")})
    assert r.status_code == 400 and "Unsafe path" in r.text


# --------------------------------------------------------------------------- YouTube

def _client_secret(kind: str = "installed") -> bytes:
    return json.dumps({kind: {"client_id": "123-abc.apps.googleusercontent.com", "client_secret": "GOCSPX-x",
                              "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                              "token_uri": "https://oauth2.googleapis.com/token",
                              "redirect_uris": ["http://localhost"]}}).encode()


class FakeFlow:
    made: list["FakeFlow"] = []

    def __init__(self, cfg, *, redirect_uri, state=None, code_verifier=None):
        self.redirect_uri, self.state = redirect_uri, state
        self.code_verifier = code_verifier or "verifier-" + "v" * 50
        self.code = None
        FakeFlow.made.append(self)

    def authorization_url(self, **kw):
        assert kw == {"access_type": "offline", "prompt": "consent"}
        return f"https://accounts.google.com/o/oauth2/auth?state={self.state}", self.state

    def fetch_token(self, code):
        self.code = code

    @property
    def credentials(self):
        import youtube_auth
        scopes = list(youtube_auth.SCOPES)
        return SimpleNamespace(refresh_token="refresh", scopes=scopes,
                               to_json=lambda: json.dumps({"refresh_token": "refresh", "scopes": scopes}))


def test_client_secret_upload_validates(c, wiz):
    csrf = _csrf(c)
    r = c.post("/setup/youtube/secret", data={"csrf": csrf},
               files={"file": ("client_secret.json", io.BytesIO(_client_secret("web")), "application/json")})
    assert r.status_code == 400 and "Desktop app" in r.text
    r = c.post("/setup/youtube/secret", data={"csrf": csrf},
               files={"file": ("x.json", io.BytesIO(b"{nope"), "application/json")})
    assert r.status_code == 400
    r = c.post("/setup/youtube/secret", data={"csrf": csrf},
               files={"file": ("client_secret.json", io.BytesIO(_client_secret()), "application/json")},
               follow_redirects=False)
    assert r.status_code == 303
    s = config.get_settings()
    assert s.youtube_client_secrets == s.data_dir / "client_secret.json" and s.youtube_client_secrets.is_file()
    assert "GOCSPX-x" not in c.get("/setup/youtube").text


def test_youtube_oauth_state_is_checked_and_single_use(c, wiz, monkeypatch, clock):
    import youtube_auth

    FakeFlow.made = []
    monkeypatch.setattr(setup_web, "FLOW_FACTORY", lambda cfg, **kw: FakeFlow(cfg, **kw))
    csrf = _csrf(c)
    assert c.post("/setup/youtube/connect", data={"csrf": csrf}).status_code == 400   # no client file yet
    c.post("/setup/youtube/secret", data={"csrf": csrf},
           files={"file": ("client_secret.json", io.BytesIO(_client_secret()), "application/json")})
    r = c.post("/setup/youtube/connect", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://accounts.google.com/")
    start = FakeFlow.made[-1]
    assert start.redirect_uri == "http://127.0.0.1:8000/setup/youtube/callback"
    state = start.state

    r = c.get("/setup/youtube/callback", params={"state": "x" * 43, "code": "c"})        # wrong state
    assert r.status_code == 400 and "Sign-in expired" in r.text
    assert not youtube_auth.token_path().exists()

    r = c.get("/setup/youtube/callback", params={"state": state, "code": "the-code"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup/youtube?msg=youtube_ok"
    done = FakeFlow.made[-1]
    assert (done.code, done.state, done.code_verifier) == ("the-code", state, start.code_verifier)   # PKCE
    assert json.loads(youtube_auth.token_path().read_text(encoding="utf-8"))["refresh_token"] == "refresh"

    r = c.get("/setup/youtube/callback", params={"state": state, "code": "the-code"})    # replay
    assert r.status_code == 400


def test_youtube_oauth_expired_and_denied(c, wiz, monkeypatch, clock):
    monkeypatch.setattr(setup_web, "FLOW_FACTORY", lambda cfg, **kw: FakeFlow(cfg, **kw))
    csrf = _csrf(c)
    c.post("/setup/youtube/secret", data={"csrf": csrf},
           files={"file": ("client_secret.json", io.BytesIO(_client_secret()), "application/json")})
    c.post("/setup/youtube/connect", data={"csrf": csrf}, follow_redirects=False)
    state = FakeFlow.made[-1].state
    clock.advance(minutes=16)
    assert c.get("/setup/youtube/callback", params={"state": state, "code": "c"}).status_code == 400
    c.post("/setup/youtube/connect", data={"csrf": csrf}, follow_redirects=False)
    r = c.get("/setup/youtube/callback", params={"state": FakeFlow.made[-1].state, "error": "access_denied"})
    assert r.status_code == 400 and "did not grant access" in r.text


# --------------------------------------------------------------------------- maintenance

class _Proc:
    def __init__(self, out="", rc=0):
        self.stdout, self.stderr, self.returncode = out, "", rc


def test_update_runs_exact_shell_free_commands(monkeypatch):
    calls = []
    heads = iter(["aaa", "bbb"])
    digests = iter(["old", "new"])

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        assert "shell" not in kw and isinstance(argv, list)
        if argv[1:] == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return _Proc("claude/clever-allen-zi43bi\n")
        if argv[1:] == ["rev-parse", "HEAD"]:
            return _Proc(next(heads))
        return _Proc("ok")

    monkeypatch.setattr(setup_web.subprocess, "run", fake_run)
    monkeypatch.setattr(setup_web.shutil, "which", lambda name: "/usr/bin/git")
    monkeypatch.setattr(setup_web, "_req_digest", lambda: next(digests))
    res = setup_web.update_app()
    assert res["ok"] and res["updated"]
    assert [a for a, _ in calls] == [
        ["/usr/bin/git", "rev-parse", "--abbrev-ref", "HEAD"],
        ["/usr/bin/git", "rev-parse", "HEAD"],
        ["/usr/bin/git", "pull", "--ff-only", "origin", "claude/clever-allen-zi43bi"],
        ["/usr/bin/git", "rev-parse", "HEAD"],
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-r",
         str(setup_web.REPO_ROOT / "requirements.txt")],
    ]
    assert all(kw["cwd"] == str(setup_web.REPO_ROOT) for _, kw in calls)


@pytest.mark.parametrize("branch", ["HEAD", "--upload-pack=touch /tmp/x", "a..b", ""])
def test_update_refuses_odd_branches(monkeypatch, branch):
    calls = []
    monkeypatch.setattr(setup_web.subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or _Proc(branch + "\n"))
    res = setup_web.update_app()
    assert not res["ok"] and len(calls) == 1


def test_update_pull_failure_changes_nothing(monkeypatch):
    def fake_run(argv, **kw):
        if "pull" in argv:
            return _Proc("fatal: Not possible to fast-forward", 1)
        return _Proc("main\n" if "--abbrev-ref" in argv else "aaa")
    monkeypatch.setattr(setup_web.subprocess, "run", fake_run)
    res = setup_web.update_app()
    assert not res["ok"] and "Nothing was changed" in res["log"][-1]


def test_restart_stop_and_update_buttons(c, wiz, monkeypatch):
    config_store.update({"SETUP_COMPLETE": "true"})
    csrf = _csrf(c)
    r = c.post("/setup/action/restart", data={"csrf": csrf})
    assert "Not available" in r.text                                    # not running under `schedule`
    srv = SimpleNamespace(should_exit=False)
    main._control["server"] = srv
    assert 'disabled title' not in c.get("/setup/").text
    r = c.post("/setup/action/restart", data={"csrf": csrf})
    assert "Restarting" in r.text and 'http-equiv="refresh"' in r.text
    assert srv.should_exit and main._control["restart"] is True
    srv.should_exit = False
    r = c.post("/setup/action/stop", data={"csrf": csrf})
    assert "stopped" in r.text and srv.should_exit and main._control["restart"] is False
    monkeypatch.setattr(setup_web, "update_app", lambda: {"ok": True, "updated": True, "log": ["Fast-forward"]})
    srv.should_exit = False
    r = c.post("/setup/action/update", data={"csrf": csrf})
    assert "Updated - restarting" in r.text and srv.should_exit and main._control["restart"] is True
    assert c.post("/setup/action/open-data", data={"csrf": csrf}).status_code == 400   # Windows only
    assert c.post("/setup/action/rm-rf", data={"csrf": csrf}).status_code == 404


def test_done_page_shows_checklist_with_fix_links(c, monkeypatch):
    checks = [main.Check("Twilio", "warn", "missing", "see docs/TWILIO_SETUP.md"),
              main.Check("python", "pass", "3.13"), main.Check("ffmpeg", "fail", "not found", "install it")]
    monkeypatch.setattr(main, "doctor_checks", lambda **kw: checks)
    html = c.get("/setup/done").text
    assert html.index("ffmpeg") < html.index("Twilio") < html.index("1 things working")   # red first
    assert 'href="/setup/phone">Fix' in html and "install it" in html
    assert "docs/TWILIO_SETUP.md" not in html                                       # CLI hint replaced by Fix
    assert "Go to my home screen" in html


def test_help_pages(c):
    assert "Twilio" in c.get("/setup/help/twilio").text
    assert c.get("/setup/help/../../config.env").status_code == 404
    assert c.get("/setup/help/secrets").status_code == 404


def test_start_with_windows_shortcut(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(setup_web.Path, "home", classmethod(lambda cls: tmp_path))
    lnk = tmp_path / "ClipAgent (background).lnk"
    monkeypatch.setattr(setup_web, "BACKGROUND_LNK", lnk)
    assert "run ClipAgent-Setup.bat again" in setup_web.set_start_with_windows(True)
    lnk.write_bytes(b"LNK")
    assert setup_web.set_start_with_windows(True) is None and setup_web.start_with_windows()
    assert (setup_web.startup_dir() / "ClipAgent.lnk").read_bytes() == b"LNK"
    assert setup_web.set_start_with_windows(False) is None and not setup_web.start_with_windows()


def test_startup_option_only_on_windows(c, monkeypatch):
    assert "Start ClipAgent when Windows starts" not in c.get("/setup/power").text
    monkeypatch.setattr(sys, "platform", "win32")
    assert "Start ClipAgent when Windows starts" in c.get("/setup/power").text


def test_phone_normalization():
    assert setup_web.normalize_phone("555-123-4567") == "+15551234567"
    assert setup_web.normalize_phone("1 (555) 123 4567") == "+15551234567"
    assert setup_web.normalize_phone("+44 20 7946 0958") == "+442079460958"
    assert setup_web.normalize_phone("12345") is None


def test_subprocess_not_shell_anywhere_in_setup_web():
    src = Path(setup_web.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in src and "os.system" not in src
    assert subprocess.run is not None
