"""Dashboard: pages, auth, CSRF, approvals, CSV export, JSON summary, escaping, demo seeding."""

from __future__ import annotations

import csv
import io
import os
import re
import shutil
import sys
import types
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
import dashboard
import db
import home
import seed_demo
from assistant import runtime, store
from assistant import settings as asettings
from assistant_fixtures import assistant_env, fake_model  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")

PAGES = ["/dashboard/", "/dashboard", "/dashboard/clips", "/dashboard/insights", "/dashboard/strategy",
         "/dashboard/sources", "/dashboard/costs", "/dashboard/decisions", "/dashboard/events",
         "/dashboard/clips?sort=cost&dir=asc&status=published", "/dashboard/insights?metric=pct&period=28",
         "/dashboard/costs?period=7", "/dashboard/sources?status=rejected", "/dashboard/events?level=error",
         "/dashboard/studio?period=all", "/dashboard/studio?period=90", "/dashboard/decisions?nstatus=sent",
         "/dashboard/studio", "/dashboard/memory", "/dashboard/memory?q=coffee", "/dashboard/agents/main",
         "/dashboard/agents/researcher"]
APIS = ["/dashboard/api/home", "/dashboard/api/summary", "/dashboard/api/series", "/dashboard/api/clips", "/dashboard/api/insights",
        "/dashboard/api/costs", "/dashboard/licences.csv"]


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(dashboard.router)
    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_app(), base_url="http://testserver")


def _with_token(monkeypatch, token: str = "s3cret-token") -> str:
    monkeypatch.setenv("DASHBOARD_TOKEN", token)
    config.reset_settings()
    return token


def _csrf(html: str) -> str:
    m = re.search(r'name="csrf" value="([0-9a-f]+)"', html)
    assert m, "no csrf field on page"
    return m.group(1)


@pytest.fixture
def notifier(monkeypatch):
    """A fake ``notify`` module that records handle_decision calls and resolves like the real one."""
    calls = []

    def handle_decision(decision_id: int, approved: bool, *, via: str) -> bool:
        calls.append((decision_id, approved, via))
        return db.resolve_decision(decision_id, "approved" if approved else "rejected", via=via)

    mod = types.SimpleNamespace(handle_decision=handle_decision)
    monkeypatch.setitem(sys.modules, "notify", mod)
    return calls


_SEED_CACHE: dict[str, Any] = {}


@pytest.fixture
def seeded(tmp_path_factory, clock, monkeypatch):
    """Small but complete demo dataset (built once per session, copied into each test's data dir)."""
    data_dir = config.get_settings().data_dir
    if "dir" not in _SEED_CACHE:
        counts = seed_demo.seed(data_dir, days=30, seed_value=3, now=clock.now)
        assert counts["uploads"] > 10 and counts["stats"] > 50
        conn = db.connect()
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        cache = tmp_path_factory.mktemp("demo-template")
        for name in ("clipagent.db", "strategy.json", seed_demo.DEMO_MARKER_FILE):
            shutil.copy(data_dir / name, cache / name)
        shutil.copytree(data_dir / "vault", cache / "vault")
        _SEED_CACHE.update(dir=cache, counts=counts)
    else:
        for name in ("clipagent.db", "strategy.json", seed_demo.DEMO_MARKER_FILE):
            shutil.copy(_SEED_CACHE["dir"] / name, data_dir / name)
        shutil.copytree(_SEED_CACHE["dir"] / "vault", data_dir / "vault", dirs_exist_ok=True)
    monkeypatch.setenv("CLIPAGENT_STRATEGY_PATH", str(data_dir / "strategy.json"))
    monkeypatch.setenv("VAULT_DIR", str(data_dir / "vault"))
    for k in ("ASSISTANT_ENABLED", "FAKE_ASSISTANT", "OWNER_NAME"):
        monkeypatch.setenv(k, os.environ.get(k, ""))  # seed_demo sets these; restore after the test
    config.reset_settings()
    asettings.reset_assistant_settings()
    return _SEED_CACHE["counts"]


# --------------------------------------------------------------------------- pages


@pytest.mark.parametrize("path", PAGES + APIS)
def test_every_page_200_on_empty_db(client, clock, path):
    r = client.get(path)
    assert r.status_code == 200, r.text[:2000]


def test_empty_overview_shows_onboarding(client, clock):
    html = client.get("/dashboard/studio").text
    assert "Getting started" in html
    home_html = client.get("/dashboard/").text
    assert "Getting settled" in home_html and "tell me your pay rate" in home_html
    assert "DEMO DATA" not in html
    assert "<script src=\"http" not in html and "https://cdn" not in html


@pytest.mark.parametrize("path", PAGES + APIS)
def test_every_page_200_on_seeded_db(client, seeded, path):
    r = client.get(path)
    assert r.status_code == 200, r.text[:2000]


def test_seeded_detail_pages_and_banner(client, seeded):
    html = client.get("/dashboard/").text
    assert "DEMO DATA" in html
    clip = db.list_clips("uploaded", limit=1)[0]
    r = client.get(f"/dashboard/clips/{clip.id}")
    assert r.status_code == 200 and "Licence &amp; source" in r.text
    assert client.get(f"/dashboard/sources/{clip.source_id}").status_code == 200
    assert client.get(f"/dashboard/api/clips/{clip.id}").json()["clip"]["id"] == clip.id
    assert client.get("/dashboard/clips/999999").status_code == 404
    assert client.get("/dashboard/sources/999999").status_code == 404
    assert client.get("/dashboard/api/clips/999999").status_code == 404


def test_static_files_served_and_traversal_blocked(client):
    assert client.get("/dashboard/static/dashboard.css").status_code == 200
    assert client.get("/dashboard/static/charts.js").status_code == 200
    assert client.get("/dashboard/static/../dashboard.py").status_code == 404
    assert client.get("/dashboard/static/%2e%2e/dashboard.py").status_code == 404


def test_security_headers(client, clock):
    r = client.get("/dashboard/")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "same-origin"   # no-referrer made browsers send Origin: null


# --------------------------------------------------------------------------- JSON


def test_summary_shape(client, seeded):
    s = client.get("/dashboard/api/summary?period=7").json()
    assert set(s) >= {"generated_at", "timezone", "demo", "dry_run", "period", "kpis", "today",
                      "pending_decisions", "pipeline", "experiment", "counts", "recent_events"}
    assert s["demo"] is True
    assert s["period"]["key"] == "7" and s["period"]["previous_start"]
    for k in ("views", "watch_minutes", "subs_net", "published", "avg_view_pct", "spend", "cost_per_1k"):
        assert set(s["kpis"][k]) == {"value", "previous", "delta_pct", "delta_pts"}
    assert s["kpis"]["views"]["value"] > 0
    assert set(s["today"]) >= {"uploads", "upload_cap", "spend_usd", "spend_cap_usd", "paused"}
    assert [p["module"] for p in s["pipeline"]][:3] == ["scout", "fetch", "transcribe"]
    assert s["pending_decisions"] and s["pending_decisions"][0]["kind"] == "new_source_type"


def test_summary_empty_db(client, clock):
    s = client.get("/dashboard/api/summary").json()
    assert s["kpis"]["views"]["value"] == 0
    assert s["pending_decisions"] == [] and s["demo"] is False
    assert s["today"]["uploads"] == 0


def test_series_and_clips_api(client, seeded):
    s = client.get("/dashboard/api/series").json()
    assert len(s["series"]["views"]["x"]) == 28
    clips = client.get("/dashboard/api/clips?sort=views&limit=5").json()
    views = [c["views"] for c in clips["clips"]]
    assert views == sorted(views, reverse=True) and clips["total"] >= len(views)


# --------------------------------------------------------------------------- auth


def test_auth_off_allows_everything(client, clock):
    assert client.get("/dashboard/api/summary").status_code == 200


def test_auth_on_requires_token(client, clock, monkeypatch):
    token = _with_token(monkeypatch)
    r = client.get("/dashboard/")
    assert r.status_code == 401 and "Sign in" in r.text
    assert client.get("/dashboard/api/summary").status_code == 401
    assert client.get("/dashboard/licences.csv").status_code == 401
    assert client.get("/dashboard/api/summary?token=wrong").status_code == 401
    assert client.get("/dashboard/api/summary", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/dashboard/api/summary", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert client.get(f"/dashboard/api/summary?token={token}").status_code == 200
    # static assets stay public so the sign-in page is styled
    assert client.get("/dashboard/static/dashboard.css").status_code == 200


def test_token_query_sets_cookie_and_strips_token(client, clock, monkeypatch):
    token = _with_token(monkeypatch)
    r = client.get(f"/dashboard/clips?token={token}&sort=views", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard/clips?sort=views"
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and token not in cookie
    # the cookie now authenticates the browser
    assert client.get("/dashboard/clips").status_code == 200


def test_login_form(client, clock, monkeypatch):
    token = _with_token(monkeypatch)
    bad = client.post("/dashboard/login", data={"token": "wrong", "next": "/dashboard/costs"})
    assert bad.status_code == 401
    r = client.post("/dashboard/login", data={"token": token, "next": "https://evil.example/x"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/dashboard/"  # no open redirect
    assert client.get("/dashboard/costs").status_code == 200
    page = client.get("/dashboard/").text
    out = client.post("/dashboard/logout", data={"csrf": _csrf(page)}, follow_redirects=False)
    assert out.status_code == 303
    client.cookies.clear()
    assert client.get("/dashboard/").status_code == 401


# --------------------------------------------------------------------------- decisions


def test_approve_calls_notifier(client, clock, notifier):
    did = db.add_decision("budget_increase", "Raise the daily AI budget from 1.00 to 1.50 dollars")
    html = client.get("/dashboard/decisions").text
    assert "Raise the daily AI budget" in html
    r = client.post(f"/dashboard/decisions/{did}/approve", data={"csrf": _csrf(html), "next": "/dashboard/"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/dashboard/?msg=approved"
    assert notifier == [(did, True, "dashboard")]
    assert db.get_decision(did).status == "approved"
    # a second answer is reported as already resolved and does not call the notifier again
    r2 = client.post(f"/dashboard/decisions/{did}/reject", data={"csrf": _csrf(html)}, follow_redirects=False)
    assert "msg=already" in r2.headers["location"]
    assert len(notifier) == 1


def test_reject_calls_notifier(client, clock, notifier):
    did = db.add_decision("new_niche", "Add history topics")
    html = client.get("/dashboard/").text
    r = client.post(f"/dashboard/decisions/{did}/reject", data={"csrf": _csrf(html)}, follow_redirects=False)
    assert r.status_code == 303 and "msg=rejected" in r.headers["location"]
    assert notifier == [(did, False, "dashboard")]


def test_decision_post_requires_csrf(client, clock, notifier):
    did = db.add_decision("new_niche", "Add history topics")
    assert client.post(f"/dashboard/decisions/{did}/approve", data={}).status_code == 403
    assert client.post(f"/dashboard/decisions/{did}/approve", data={"csrf": "0" * 64}).status_code == 403
    html = client.get("/dashboard/decisions").text
    cross = client.post(f"/dashboard/decisions/{did}/approve", data={"csrf": _csrf(html)},
                        headers={"Origin": "https://evil.example"})
    assert cross.status_code == 403
    assert notifier == [] and db.get_decision(did).status == "pending"
    assert client.get(f"/dashboard/decisions/{did}/approve").status_code == 405  # never on GET


def test_decision_post_auth_and_bearer(client, clock, notifier, monkeypatch):
    token = _with_token(monkeypatch)
    did = db.add_decision("new_niche", "Add history topics")
    assert client.post(f"/dashboard/decisions/{did}/approve", data={"csrf": "x"}).status_code == 401
    r = client.post(f"/dashboard/decisions/{did}/approve", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert notifier == [(did, True, "dashboard")]


def test_notifier_failure_is_reported(client, clock, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("twilio down")

    monkeypatch.setitem(sys.modules, "notify", types.SimpleNamespace(handle_decision=boom))
    did = db.add_decision("new_niche", "Add history topics")
    html = client.get("/dashboard/decisions").text
    r = client.post(f"/dashboard/decisions/{did}/approve", data={"csrf": _csrf(html)}, follow_redirects=False)
    assert "msg=failed" in r.headers["location"]
    assert any("failed" in e.message for e in db.list_events(level="error"))


# --------------------------------------------------------------------------- CSV


def test_licence_csv(client, clock, make_clip, make_upload, make_source):
    sid = make_source(status="picked", channel="NASA", licence="us_gov_public_domain",
                      licence_evidence={"matched_rule": "allowlisted channel 'NASA'", "license": None})
    cid = make_clip(sid, status="rendered", title="=HYPERLINK(\"http://evil\")", credit_text="Video: NASA (public domain)")
    make_upload(cid, video_id="abcDEF12345")
    dry = make_clip(sid, status="rendered", title="dry one")
    make_upload(dry, video_id="dryrun-2", dry_run=True)
    r = client.get("/dashboard/licences.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 1  # dry runs excluded by default
    row = rows[0]
    assert row["youtube_video_id"] == "abcDEF12345"
    assert row["youtube_url"] == "https://www.youtube.com/shorts/abcDEF12345"
    assert row["licence"] == "us_gov_public_domain"
    assert row["publisher"] == "NASA"
    assert row["credit_text"] == "Video: NASA (public domain)"
    assert row["licence_rule"] == "allowlisted channel 'NASA'"
    assert row["source_url"].startswith("https://www.youtube.com/watch?v=")
    assert row["clip_title"].startswith("'=")  # spreadsheet formula injection neutralised
    rows2 = list(csv.DictReader(io.StringIO(client.get("/dashboard/licences.csv?include_dry_run=1").text)))
    assert len(rows2) == 2


# --------------------------------------------------------------------------- escaping


EVIL = '<script>alert("x")</script><img src=x onerror=alert(1)>'


def test_malicious_title_is_escaped_everywhere(client, clock, make_source, make_clip, make_upload):
    sid = make_source(status="picked", title=EVIL, channel=EVIL, description=EVIL,
                      licence_evidence={"matched_rule": EVIL})
    cid = make_clip(sid, status="rendered", title=EVIL, hook_text=EVIL, sonnet_rationale=EVIL, description=EVIL)
    vid = "vidEVIL0001"
    make_upload(cid, video_id=vid)
    db.upsert_stat(vid, "2026-11-11", views=50, avg_view_pct=60.0)
    db.add_decision("other", EVIL, {"x": EVIL})
    db.log_event("pick", EVIL, level="error", data={"t": EVIL})
    db.add_notification("sms", EVIL, "sms", status="sent")
    for path in ["/dashboard/", "/dashboard/studio", "/dashboard/clips", f"/dashboard/clips/{cid}", f"/dashboard/sources/{sid}",
                 "/dashboard/sources", "/dashboard/decisions", "/dashboard/events", "/dashboard/insights",
                 "/dashboard/clips?q=%3Cscript%3E"]:
        html = client.get(path).text
        assert "<script>alert" not in html, path
        assert "<img src=x" not in html, path
    assert "&lt;script&gt;" in client.get(f"/dashboard/clips/{cid}").text


def test_json_island_cannot_break_out(client, clock, make_source, make_clip, make_upload):
    sid = make_source(status="picked")
    cid = make_clip(sid, status="rendered", title="</script><script>alert(1)</script>")
    make_upload(cid, video_id="vidJSON0001", publish_at="2026-11-01T15:00:00Z")
    for d in range(10):
        db.upsert_stat("vidJSON0001", (datetime(2026, 11, 1) + timedelta(days=d)).date(), views=100 + d,
                       avg_view_pct=70.0)
    db.update_clip(cid, haiku_score=9)
    html = client.get("/dashboard/insights").text
    assert "</script><script>alert(1)" not in html


def test_revenue_tiles_only_with_revenue_data(client, clock, make_upload):
    make_upload(video_id="vidREV00001")
    db.upsert_stat("vidREV00001", "2026-11-10", views=1000, avg_view_pct=70.0)
    assert "RPM" not in client.get("/dashboard/studio").text  # no revenue reported -> no revenue tiles
    conn = db.connect()
    try:
        col = dashboard.revenue_column(conn)
        if col is None:  # older schema: emulate the optional revenue column
            conn.execute("ALTER TABLE stats ADD COLUMN estimated_revenue REAL")
            col = "estimated_revenue"
        assert "RPM" not in client.get("/dashboard/studio").text
        conn.execute(f"UPDATE stats SET {col} = 2.5")
        conn.commit()
    finally:
        conn.close()
    html = client.get("/dashboard/studio").text
    assert "RPM" in html and "Revenue − LLM cost" in html
    s = client.get("/dashboard/api/summary").json()
    assert s["kpis"]["revenue"]["value"] == pytest.approx(2.5)
    assert s["kpis"]["rpm"]["value"] == pytest.approx(2.5)


def test_live_counts_fill_the_analytics_lag(client, clock, make_upload):
    uid = make_upload(video_id="vidLIVE0001")
    if not hasattr(db, "set_upload_lifetime"):
        pytest.skip("schema without lifetime counters")
    db.set_upload_lifetime("vidLIVE0001", views=4321, likes=99, comments=3)
    clips = client.get("/dashboard/api/clips").json()["clips"]
    assert clips[0]["views"] == 4321 and clips[0]["likes"] == 99
    clip_id = db.get_upload(uid).clip_id
    assert "4.3K" in client.get(f"/dashboard/clips/{clip_id}").text


# --------------------------------------------------------------------------- helpers


def test_formatters():
    assert dashboard.fmt_compact(1284) == "1.3K"
    assert dashboard.fmt_compact(312_000) == "312K"
    assert dashboard.fmt_compact(4_200_000) == "4.2M"
    assert dashboard.fmt_compact(None) == "—"
    assert dashboard.fmt_usd(0.42) == "$0.42"
    assert dashboard.fmt_usd(0.0108) == "$0.011"
    assert dashboard.fmt_usd(12.5) == "$12.50"
    assert dashboard.fmt_pct(54.2) == "54%"
    assert dashboard.fmt_duration(45) == "0:45"


def test_hook_form_and_spearman():
    assert dashboard.hook_form("Would you fly on this?") == "Question"
    assert dashboard.hook_form("17,000 mph and falling") == "Number / stat"
    assert dashboard.hook_form("Nobody expected this.") == "Statement / claim"
    res = dashboard.spearman([1, 2, 3, 4, 5, 6], [2, 4, 6, 8, 10, 12])
    assert res["r"] == pytest.approx(1, abs=1e-5)
    assert dashboard.spearman([1, 2], [1, 2])["r"] is None
    assert "Not enough data" in dashboard.describe_correlation({"n": 2, "r": None, "p": None})


def test_strategy_diff():
    old = {"version": 1, "uploads_per_day": 4, "topics": [{"query": "a"}], "experiment": {"id": "e1"},
           "clip_length_sec": {"min": 20, "target": 45, "max": 59}}
    new = {"version": 2, "uploads_per_day": 5, "topics": [{"query": "b"}], "experiment": {"id": "e2"},
           "clip_length_sec": {"min": 20, "target": 38, "max": 59}}
    out = dashboard.strategy_diff(old, new)
    assert "uploads per day: 4 → 5" in out
    assert any(x.startswith("topics: +b") for x in out)
    assert "experiment: e1 → e2" in out
    assert any("target 45 → 38" in x for x in out)


# --------------------------------------------------------------------------- seed_demo


def test_seed_demo_refuses_real_db(tmp_path, clock, make_source):
    make_source()  # real data in the default (test) DB
    data_dir = config.get_settings().data_dir
    with pytest.raises(seed_demo.RefuseError):
        seed_demo.seed(data_dir, days=3, now=clock.now)
    assert len(db.list_sources()) == 1
    assert db.kv_get(dashboard.DEMO_KV_KEY) is None


def test_seed_demo_cli_refuses_and_replaces(tmp_path, clock, capsys):
    target = tmp_path / "demo"
    assert seed_demo.main(["--data-dir", str(target), "--days", "4", "--seed", "1"]) == 0
    assert seed_demo.main(["--data-dir", str(target), "--days", "4"]) == 2  # existing demo needs --replace
    assert "refusing" in capsys.readouterr().err
    assert seed_demo.main(["--data-dir", str(target), "--days", "4", "--replace"]) == 0
    assert db.kv_get(dashboard.DEMO_KV_KEY)["generator"] == "seed_demo.py"


def test_seed_demo_refuses_foreign_strategy_file(tmp_path, clock):
    target = tmp_path / "other"
    target.mkdir()
    (target / "strategy.json").write_text("{}")
    with pytest.raises(seed_demo.RefuseError):
        seed_demo.seed(target, days=2, now=clock.now)


def test_seeded_data_is_consistent(seeded):
    # every uploaded clip has a licence and an upload; stats only for real (non-dry-run) videos
    for c in db.list_clips("uploaded"):
        assert c.licence in ("us_gov_public_domain", "cc_by") and c.source_url
        assert db.list_uploads(clip_id=c.id)
    live = set(db.live_video_ids())
    conn = db.connect()
    try:
        vids = {r[0] for r in conn.execute("SELECT DISTINCT youtube_video_id FROM stats")}
    finally:
        conn.close()
    assert vids <= live and all(v.startswith("demo") for v in vids)
    assert db.list_decisions("pending")
    assert any(e.level == "error" for e in db.list_events(limit=2000))
    assert any(s.status == "rejected" for s in db.list_sources())
    assert db.utcnow is not None and db.utcnow() == datetime(2026, 11, 13, 15, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- review regressions


def test_no_token_dashboard_refuses_requests_relayed_by_a_proxy(clock):
    """Behind Caddy every request arrives from 127.0.0.1; without a token it must not be 'open'."""
    did = db.add_decision("budget_increase", "Raise budget", {"action": "apply_strategy"})
    local = TestClient(_app(), base_url="http://testserver")
    html = local.get("/dashboard/decisions").text                    # direct local: allowed
    proxied = TestClient(_app(), base_url="https://agent.example.com")
    hdr = {"X-Forwarded-For": "203.0.113.9"}
    assert proxied.get("/dashboard/decisions", headers=hdr).status_code == 403
    assert proxied.get("/dashboard/api/summary").status_code == 403    # public Host name alone
    assert local.get("/dashboard/api/summary", headers=hdr).status_code == 403
    r = proxied.post(f"/dashboard/decisions/{did}/approve", data={"csrf": _csrf(html)}, headers=hdr)
    assert r.status_code == 403 and db.get_decision(did).status == "pending"


def test_allow_unauthenticated_opt_in(clock, monkeypatch):
    monkeypatch.setenv("DASHBOARD_ALLOW_UNAUTHENTICATED", "true")
    config.reset_settings()
    c = TestClient(_app(), base_url="https://agent.example.com")
    assert c.get("/dashboard/api/summary", headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 200


def test_wrong_tokens_are_throttled(client, clock, monkeypatch):
    token = _with_token(monkeypatch)
    monkeypatch.setattr(dashboard, "_auth_failures", {})
    for _ in range(dashboard.AUTH_FAIL_LIMIT):
        assert client.post("/dashboard/login", data={"token": "guess"}).status_code == 401
    assert client.post("/dashboard/login", data={"token": token}).status_code == 429   # even the right one
    assert client.get("/dashboard/api/summary", headers={"Authorization": f"Bearer {token}"}).status_code == 429
    clock_now = dashboard._auth_failures
    for ip in clock_now:
        clock_now[ip] = [t - dashboard.AUTH_FAIL_WINDOW_SEC - 1 for t in clock_now[ip]]
    assert client.get("/dashboard/api/summary", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_source_links_never_use_script_urls(client, clock, make_source, make_clip):
    sid = make_source(status="picked", url="javascript:alert(document.cookie)")
    cid = make_clip(sid)
    for path in (f"/dashboard/sources/{sid}", f"/dashboard/clips/{cid}"):
        html = client.get(path).text
        assert 'href="javascript:' not in html and 'href="#"' in html
    assert dashboard.ext_url("https://nasa.gov/x") == "https://nasa.gov/x"


def test_clip_page_shows_haiku_reason(client, clock, make_clip):
    cid = make_clip(haiku_reason="Opens with a <b>question</b>")
    html = client.get(f"/dashboard/clips/{cid}").text
    assert "Opens with a &lt;b&gt;question&lt;/b&gt;" in html


def test_approval_that_fails_to_apply_is_reported(client, clock, monkeypatch):
    import notify

    def decide(decision_id, approved, *, via):
        db.resolve_decision(decision_id, "approved", via=via)
        return notify.DecisionOutcome(True, "approved", False)

    monkeypatch.setitem(sys.modules, "notify", types.SimpleNamespace(decide=decide, handle_decision=None))
    did = db.add_decision("new_niche", "Add history topics")
    html = client.get("/dashboard/decisions").text
    r = client.post(f"/dashboard/decisions/{did}/approve", data={"csrf": _csrf(html)}, follow_redirects=False)
    assert "msg=unapplied" in r.headers["location"]
    assert "could not be applied" in client.get("/dashboard/decisions?msg=unapplied").text


def test_seed_demo_ignores_db_path_from_config_env(tmp_path, monkeypatch):
    """config.env relocating the real DB must not make the demo write into it."""
    real_db = tmp_path / "real" / "clipagent.db"
    cfg = tmp_path / "config.env"
    cfg.write_text(f"CLIPAGENT_DB_PATH={real_db}\nCLIPAGENT_MEDIA_DIR={tmp_path / 'realmedia'}\n"
                   "DRY_RUN=false\nTWILIO_AUTH_TOKEN=live-token\n", encoding="utf-8")
    monkeypatch.setenv("CLIPAGENT_CONFIG_FILE", str(cfg))
    demo = tmp_path / "demo"
    seed_demo.configure(demo)
    s = config.get_settings()
    assert s.db_path == demo / "clipagent.db" and str(s.downloads_dir).startswith(str(demo))
    assert s.dry_run and not s.twilio_auth_token
    assert not real_db.exists()


# --------------------------------------------------------------------------- command-centre home


def _enable(monkeypatch, **extra):
    monkeypatch.setenv("ASSISTANT_ENABLED", "true")
    monkeypatch.setenv("FAKE_ASSISTANT", "true")
    for k, v in extra.items():
        monkeypatch.setenv(k, v)
    config.reset_settings()
    asettings.reset_assistant_settings()


def _json_post(client, path, data, **headers):
    return client.post(path, data=data, headers={"Accept": "application/json", **headers})


def test_old_urls_redirect_to_studio(client, clock):
    r = client.get("/dashboard/?period=28", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/dashboard/studio?period=28"
    r = client.get("/dashboard/overview?period=7", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/dashboard/studio?period=7"
    assert 'href="/dashboard/studio"' in client.get("/dashboard/clips").text  # studio sub-nav


def test_seeded_home_shows_everything(client, seeded):
    html = client.get("/dashboard/").text
    assert "DEMO DATA" in html and "Needs you" in html and "Spoke &amp; Chain" in html
    for name in ("Studio", "Ledger", "Quartermaster", "Archivist", "Briefer", "Researcher", "Engineer"):
        assert name in html
    assert "Black bean tacos" in html and "Emergency fund" in html
    j = client.get("/dashboard/api/home").json()
    assert j["mood"] == "alert" and set(j["html"]) == {"needs", "tiles", "roster", "feed"}
    statuses = {a["name"]: a["status"] for a in j["agents"]}
    assert statuses["researcher"] == "error" and statuses["archivist"] == "working"
    note = client.get("/dashboard/memory?note=Profile/About.md")
    assert note.status_code == 200 and 'class="wikilink"' in note.text


def test_new_posts_require_csrf_and_auth(client, clock, monkeypatch):
    _enable(monkeypatch)
    meal = store.add_meal("dinner", "Tacos")
    chk = store.add_checkin("evening", "How was today?", db.now_iso())
    posts = [("/dashboard/chat", {"message": "hi"}), (f"/dashboard/meals/{meal}/accept", {}),
             (f"/dashboard/checkins/{chk}/answer", {"answer": "fine"}), ("/dashboard/jobs", {"name": "X", "rate": "20"})]
    html = client.get("/dashboard/").text
    for path, data in posts:
        assert client.post(path, data=data).status_code == 403, path
        assert client.post(path, data={**data, "csrf": "0" * 64}).status_code == 403, path
        cross = client.post(path, data={**data, "csrf": _csrf(html)}, headers={"Origin": "https://evil.example"})
        assert cross.status_code == 403, path
        assert client.get(path).status_code == 405, path
    assert store.get_meal(meal).status == "suggested" and store.get_checkin(chk).status == "pending"
    assert not store.list_jobs() and not store.list_messages()
    token = _with_token(monkeypatch)
    for path, data in posts:
        assert client.post(path, data={**data, "csrf": _csrf(html)}).status_code == 401, path
    assert client.get("/dashboard/api/home").status_code == 401
    assert client.get("/dashboard/memory").status_code == 401
    assert client.get("/dashboard/api/home", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_meal_checkin_and_job_posts(client, clock, monkeypatch):
    html = client.get("/dashboard/").text
    c = _csrf(html)
    r = client.post("/dashboard/jobs", data={"csrf": c, "name": "Bike shop", "rate": "$21.50"}, follow_redirects=False)
    assert r.status_code == 303 and "msg=job_saved" in r.headers["location"]
    assert store.list_jobs()[0].hourly_rate == 21.5
    meal = store.add_meal("lunch", "Noodles")
    assert _json_post(client, f"/dashboard/meals/{meal}/decline", {"csrf": c}).json()["status"] == "declined"
    chk = store.add_checkin("weekly_hours", "Hours this week?", db.now_iso())
    assert _json_post(client, f"/dashboard/checkins/{chk}/answer", {"csrf": c, "answer": "lots"}).status_code == 400
    r = _json_post(client, f"/dashboard/checkins/{chk}/answer", {"csrf": c, "answer": "about 32.5 hours"})
    assert r.json()["logged"]["hours"] == 32.5 and store.get_checkin(chk).status == "answered"
    assert store.get_work_log(store.list_jobs()[0].id).hours == 32.5
    assert _json_post(client, f"/dashboard/checkins/{chk}/answer", {"csrf": c, "answer": "1"}).status_code == 409


def test_chat_runs_turn_escapes_and_is_rate_limited(client, clock, monkeypatch, fake_model):
    _enable(monkeypatch)
    monkeypatch.setattr(home, "_hits", {})
    fake_model.queue(EVIL)
    c = _csrf(client.get("/dashboard/").text)
    r = _json_post(client, "/dashboard/chat", {"csrf": c, "message": "hello"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert [m.channel for m in store.list_messages()] == ["dashboard", "dashboard"]
    html = client.get("/dashboard/").text
    assert "<script>alert" not in html and "<img src=x" not in html and "&lt;script&gt;" in html
    assert _json_post(client, "/dashboard/chat", {"csrf": c, "message": "x" * (home.CHAT_MAX_CHARS + 1)}).status_code == 413
    assert _json_post(client, "/dashboard/chat", {"csrf": c, "message": "  "}).status_code == 400
    codes = [_json_post(client, "/dashboard/chat", {"csrf": c, "message": "again"}).status_code for _ in range(8)]
    assert 429 in codes
    blocked = _json_post(client, "/dashboard/chat", {"csrf": c, "message": "again"})
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) > 0


def test_chat_disabled_states(client, clock, monkeypatch):
    c = _csrf(client.get("/dashboard/").text)
    html = client.get("/dashboard/").text
    assert "switched off" in html and 'name="message"' in html and "disabled" in html
    r = _json_post(client, "/dashboard/chat", {"csrf": c, "message": "hi"})
    assert r.status_code == 503 and "ASSISTANT_ENABLED" in r.json()["reason"]
    _enable(monkeypatch, ASSISTANT_DAILY_BUDGET_USD="0")
    r = _json_post(client, "/dashboard/chat", {"csrf": c, "message": "hi"})
    assert r.status_code == 503 and "allowance" in r.json()["reason"]
    assert not store.list_messages()


def test_untrusted_text_is_escaped_on_home(client, clock):
    store.add_meal("dinner", EVIL)
    store.record_agent_run("studio", EVIL, model="m", duration_ms=1, cost_usd=0, status="error", summary=EVIL)
    store.add_message("in", "sms", EVIL)
    store.add_lesson("x", EVIL)
    store.add_goal(EVIL, target=10, current=1)
    store.add_checkin("evening", EVIL, db.now_iso())
    for path in ("/dashboard/", "/dashboard/agents/studio", "/dashboard/memory"):
        html = client.get(path).text
        assert "<script>alert" not in html and "<img src=x" not in html, path
    assert "<script>alert" not in str(client.get("/dashboard/api/home").json()["html"])


def test_memory_browser_safe_markdown_and_traversal(client, clock, tmp_path):
    from assistant import vault

    vault.write_note("People/Sam.md", "# Sam\n\n" + EVIL + "\n\n- **bold** [[About]] [[Nowhere|x]]\n"
                     "[click](javascript:alert(1))\n```\n<b>code</b>\n```", source="test")
    vault.write_note("Profile/About.md", "# About\nSee [[Sam]]", source="test")
    r = client.get("/dashboard/memory?note=People/Sam.md")
    assert r.status_code == 200
    body = r.text
    assert "<script>alert" not in body and "<img src=x" not in body and "<b>code</b>" not in body
    assert 'href="javascript:' not in body and "<strong>bold</strong>" in body
    assert 'href="/dashboard/memory?note=Profile%2FAbout.md"' in body and "is-missing" in body
    assert "Profile/About.md" in body  # backlink
    outside = tmp_path / "secret.md"
    outside.write_text("TOP SECRET")
    for bad in ("../secret.md", "../../etc/passwd", "/etc/passwd", "%2e%2e/secret.md", ".obsidian/x.md", "x.py"):
        resp = client.get("/dashboard/memory", params={"note": bad})
        assert resp.status_code == 404 and "TOP SECRET" not in resp.text, bad


def test_render_markdown_never_emits_raw_html():
    out = home.render_markdown('<div onclick="x">hi</div>\n# <i>t</i>\n- [ ] <a href="y">z</a>\n> <svg/onload=1>',
                               resolve=lambda t: None)
    assert "<div" not in out and "<i>" not in out and "<a href" not in out and "<svg" not in out
    tags = set(re.findall(r"</?([a-z0-9]+)", out))
    assert tags <= {"p", "br", "h2", "h3", "h4", "ul", "ol", "li", "strong", "em", "code", "pre", "blockquote",
                    "hr", "span", "a"}


def test_greeting_and_streak():
    from datetime import date

    today = date(2026, 11, 13)
    assert home.streak_of({today, today - timedelta(days=1), today - timedelta(days=3)}, today) == 2
    assert home.streak_of({today - timedelta(days=1), today - timedelta(days=2)}, today) == 2
    a = {"honorific": "sir", "enabled": True}
    needs = {"decisions": [], "checkins": [], "failures_24h": 0, "run_errors": []}
    g = home.greeting(datetime(2026, 11, 13, 19, 0), a, needs, {"missing": []}, {}, {}, False)
    assert g["salute"] == "Good evening, sir." and g["mood"] == "idle"
    assert "sprite" in home.sprite_svg("main", pose="alert") and "crispEdges" in home.glyph("coin")


def test_seed_demo_vault_stays_inside_demo_dir(seeded):
    root = asettings.get_assistant_settings().vault_dir.resolve()
    assert root.is_relative_to(config.get_settings().data_dir.resolve())
    assert (root / seed_demo.DEMO_MARKER_FILE).exists()
    assert store.list_jobs()[0].name == "Spoke & Chain"
    assert any(r.status == "error" for r in store.list_agent_runs(200))
    assert store.list_checkins("pending")


def test_chat_images_and_background_turns(client, clock, monkeypatch, fake_model):
    import time as _time

    _enable(monkeypatch)
    monkeypatch.setattr(home, "_hits", {})
    c = _csrf(client.get("/dashboard/").text)
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
    r = client.post("/dashboard/chat", data={"csrf": c, "message": "look", "async": "1"},
                    files=[("image", ("shot.png", png, "image/png"))], headers={"Accept": "application/json"})
    assert r.status_code == 200 and r.json()["pending"] is True
    name = r.json()["images"][0]
    assert home.UPLOAD_NAME.match(name)
    for _ in range(100):
        j = client.get(f"/dashboard/chat/job/{r.json()['job']}").json()
        if j["done"]:
            break
        _time.sleep(0.05)
    assert j["done"] and j["ok"]
    msg = store.list_messages(channel="dashboard", direction="in")[0]
    assert msg.meta == {"attachments": [name]}
    img = client.get(f"/dashboard/uploads/{name}")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"
    assert client.get("/dashboard/uploads/..%2Fconfig.env").status_code == 404
    assert f"/dashboard/uploads/{name}" in client.get("/dashboard/").text
    bad = client.post("/dashboard/chat", data={"csrf": c, "message": "x"},
                      files=[("image", ("evil.png", b"<script>", "image/png"))], headers={"Accept": "application/json"})
    assert bad.status_code == 400 and "not accepted" in bad.json()["reason"]
    stop = _json_post(client, "/dashboard/sterling/stop", {"csrf": c})
    assert stop.status_code == 200
    from assistant import runtime as _rt

    assert _rt.is_stopped() and "switched off" in _json_post(client, "/dashboard/chat", {"csrf": c, "message": "hi"}).json()["reason"]
    _json_post(client, "/dashboard/sterling/start", {"csrf": c})
    assert not _rt.is_stopped()
