"""home_live.py + the one design system: the Cecil home's live panels, the staff page, the top bar's
dropdown menus on every page, the staff as Invincible characters and the Sterling -> Cecil rename."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import config
import config_store
import db
import home
import home_live
import server
from assistant import registry, store
from assistant import settings as asettings
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")

EVIL = '<script>alert(1)</script><img src=x onerror=alert(2)>'

PAGES = ["/dashboard/", "/dashboard/studio", "/dashboard/clips", "/dashboard/insights", "/dashboard/strategy",
         "/dashboard/sources", "/dashboard/costs", "/dashboard/decisions", "/dashboard/events", "/dashboard/memory",
         "/dashboard/staff", "/dashboard/agents/studio", "/dashboard/modes", "/dashboard/history",
         "/dashboard/twilio-check", "/dashboard/skills", "/setup/", "/setup/about", "/setup/brain", "/setup/money", "/setup/phone",
         "/setup/youtube", "/setup/memory", "/setup/power", "/setup/done"]


@pytest.fixture
def client() -> TestClient:
    return TestClient(server.create_app(), base_url="http://testserver")


@pytest.fixture(autouse=True)
def _no_git(monkeypatch):
    from assistant import skill_vetting

    def offline(url, accept=None):   # the Skills page reads its catalog from GitHub
        raise skill_vetting.VettingError("offline in tests")

    monkeypatch.setattr(skill_vetting, "http_get", offline)
    home_live.set_git_log(lambda n: [])
    yield
    home_live.set_git_log(None)


# -- one theme, one top bar ---------------------------------------------------------------------

@pytest.mark.parametrize("path", PAGES)
def test_every_page_uses_the_hud_theme_and_dropdown_nav(client, path):
    r = client.get(path)
    assert r.status_code == 200, path
    html = r.text
    assert "/dashboard/static/theme.css" in html and 'class="topbar"' in html
    # theme.css loads last, so its glass and chrome win over the component stylesheets
    assert html.index("theme.css") > html.index("dashboard.css") and html.index("theme.css") > html.index("home.css")
    for menu in ("menu-studio", "menu-staff", "menu-memory", "menu-settings"):
        assert f'aria-controls="{menu}"' in html and f'id="{menu}"' in html, (path, menu)
    assert 'aria-expanded="false"' in html and "data-nav-toggle" in html and "nav.js" in html
    assert html.count('aria-current="page"') >= 1, path
    assert "theme.js" not in html and "data-theme-toggle" not in html          # one theme only


def test_menus_mark_the_current_section_and_page(client):
    html = client.get("/setup/power").text
    nav = html[html.index('id="main-nav"'):html.index("</nav>", html.index('id="main-nav"'))]
    assert re.search(r'class="mainnav-link is-current"[^>]*aria-controls="menu-settings"', nav)
    assert '<a href="/setup/power" aria-current="page">' in nav
    staff = client.get("/dashboard/staff").text
    assert re.search(r'class="mainnav-link is-current"[^>]*aria-controls="menu-staff"', staff)


def test_static_assets_are_served(client):
    for name in ("theme.css", "nav.js", "cecil.js"):
        assert client.get(f"/dashboard/static/{name}").status_code == 200
    css = client.get("/dashboard/static/theme.css").text
    assert "backdrop-filter" in css and "prefers-reduced-transparency" in css and "prefers-reduced-motion" in css
    assert "@supports not" in css                                                # no-blur fallback


# -- the staff as Invincible characters -------------------------------------------------------

EXPECTED = {"main": "Cecil", "studio": "Invincible", "ledger": "Art", "quartermaster": "Debbie",
            "archivist": "The Immortal", "researcher": "Allen", "engineer": "Robot", "briefer": "Donald"}


def test_staff_display_names_keep_ids_stable():
    agents = registry.load_agents(skills=registry.load_skills())
    assert set(EXPECTED) <= set(agents)                                          # ids unchanged
    rows = {r["name"]: r for r in home.staff_nav()}
    assert {k: rows[k]["display"] for k in EXPECTED} == EXPECTED
    assert all(rows[k]["character"] for k in EXPECTED)
    assert home.agent_display("ledger") == "Art" and home.agent_display(None) == "Cecil"
    assert home.agent_display("someone-new") == "Someone new"
    # every character is an original drawn chibi portrait (inline SVG), eyes on the blink layer
    assert set(home.CHARACTERS) == set(EXPECTED)
    for name in home.CHARACTERS:
        svg = home.sprite_svg(name)
        assert 'viewBox="0 0 64 64"' in svg and 'class="px-eyes"' in svg and f"sprite-{name}" in svg, name
        assert 'class="px-eyes"' in home.sprite_svg(name, pose="alert", size=24)
    assert home.CHARACTER_NAMES["studio"] == "Invincible" and home.CHARACTER_NAMES["ledger"] == "Art Rosenbaum"
    assert 'viewBox="0 0 64 64"' in home.sprite_svg("someone-new")          # a generic figure for new staff


def test_feed_and_runs_show_display_names_not_ids():
    store.record_agent_run("ledger", "Totalled the week", model="m", duration_ms=5, cost_usd=0.01, status="ok",
                           summary="Week total: 32 h")
    who = {i["who"] for i in home.feed_data()}
    assert "Art" in who and "Ledger" not in who
    work = home_live.recent_work()
    assert work[0]["who"] == "Art" and work[0]["agent"] == "ledger"


def test_staff_page_lists_characters(client):
    store.record_agent_run("archivist", "Filing the journal", model="m", duration_ms=0, cost_usd=0, status="running")
    html = client.get("/dashboard/staff").text
    for disp in EXPECTED.values():
        assert disp in html
    assert "Filing the journal" in html and "is-working" in html


# -- the Cecil home's live panels ----------------------------------------------------------------

def test_home_has_strip_panels_charts_and_new_stuff(client):
    home_live.set_git_log(lambda n: [("abc1234", "2026-11-13T14:00:00+00:00", "feat(home): add the cecil home."),
                                     ("def5678", "2026-11-12T14:00:00+00:00", "Sterling: fix the clock")])
    store.add_message("in", "sms", "How many hours this week?")
    store.add_message("out", "sms", "Thirty-two, sir.", kind="reply", agent="main")
    html = client.get("/dashboard/").text
    for hook in ("data-cc-strip", "data-cc-stage", "data-cc-elapsed", "data-cc-orbits", "data-cc-working",
                 'data-cc-live="chats"', 'data-cc-live="staff"', 'data-cc-live="work"', 'data-cc-live="new"',
                 'data-cc-live="charts"', "cecil.js"):
        assert hook in html, hook
    # jarvis.js's hooks are all still there
    for hook in ("data-jarvis", "data-jv-core", "data-jv-ptt", "data-jv-status", "data-jv-steps", "data-jv-tasks",
                 "data-jv-panels", "data-jv-you", "data-jv-them", "data-jv-history", "data-chat-log", "data-jv-diag"):
        assert hook in html, hook
    for label in ("Claude plan", "Assistant spend", "Voice latency", "Money this week", "Shorts views"):
        assert label in html
    assert "Add the cecil home" in html and "Fix the clock" in html                # friendly titles
    assert "Thirty-two, sir." in html and "How many hours this week?" in html


def test_api_cecil_returns_escaped_fragments(client):
    store.add_message("in", "dashboard", EVIL)
    store.add_message("out", "dashboard", "Reply " + EVIL, kind="reply", agent="main")
    store.record_agent_run("studio", EVIL, model="m", duration_ms=1, cost_usd=0, status="ok", summary=EVIL)
    home_live.set_git_log(lambda n: [("abc1234", "2026-11-13T14:00:00+00:00", EVIL)])
    j = client.get("/dashboard/api/cecil").json()
    assert set(j["html"]) == set(home_live.PARTS) and "brain" in j and "working" in j
    blob = "".join(j["html"].values())
    assert "<script>" not in blob and "<img src=x" not in blob and "&lt;script&gt;" in blob
    page = client.get("/dashboard/").text
    assert "<script>alert(1)" not in page and "<img src=x onerror" not in page


def test_new_endpoints_need_auth(client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "a-test-token-value")
    config.reset_settings()
    assert client.get("/dashboard/api/cecil").status_code == 401
    assert client.get("/dashboard/staff").status_code == 401
    ok = client.get("/dashboard/api/cecil", headers={"Authorization": "Bearer a-test-token-value"})
    assert ok.status_code == 200


def test_recent_chats_pair_exchanges_across_channels():
    store.add_message("in", "sms", "one")
    store.add_message("out", "sms", "reply one", kind="reply", agent="main")
    store.add_message("in", "dashboard", "two")
    store.add_message("out", "voice", "announcement", agent="briefer")
    chats = home_live.recent_chats()
    assert chats[0]["them"] == "announcement" and chats[0]["who"] == "Donald" and chats[0]["you"] is None
    pairs = {(c["you"], c["them"]) for c in chats}
    assert ("one", "reply one") in pairs and ("two", None) in pairs


def test_friendly_titles_and_cache():
    assert home_live.friendly_title("Cecil: make the ring spin.") == "Make the ring spin"
    assert home_live.friendly_title("fix(ui)!: tidy menus;") == "Tidy menus"
    assert home_live.friendly_title("") == "Small fix" and len(home_live.friendly_title("x" * 300)) <= 110
    calls = []
    home_live.set_git_log(lambda n: calls.append(n) or [("a1", "2026-11-13T10:00:00Z", "one")])
    assert home_live.new_stuff()[0]["title"] == "One" and home_live.new_stuff()[0]["title"] == "One"
    assert len(calls) == 1                                                       # cached


def test_charts_and_geometry():
    assert home_live.spark([1]) is None and home_live.spark([1, 3, 2])["line"].startswith("M0")
    b = home_live.bars([0, 2, 4])
    assert b[2]["h"] > b[1]["h"] > b[0]["h"] >= 1
    c = home_live.charts()
    assert set(c) == {"claude", "spend", "latency", "money", "views"}
    assert [m["key"] for m in c["claude"]["meters"]] == ["five_hour", "seven_day"]


def test_plan_tile_shows_five_hour_and_weekly_only(client, clock):
    """The owner asked: no "19 out of 60 runs", just the 5-hour and weekly usage, live, and no fake 100%."""
    from assistant import usage

    in2h = int((db.utcnow() + timedelta(hours=2)).timestamp())
    usage.record_window({"status": "allowed_warning", "rateLimitType": "five_hour", "utilization": 0.68, "resetsAt": in2h})
    usage.record_window({"status": "allowed", "rateLimitType": "seven_day", "utilization": 0.3, "resetsAt": in2h + 86400 * 3})
    m = {x["key"]: x for x in home_live.plan_meters()["meters"]}
    assert m["five_hour"]["left_pct"] == 32 and m["five_hour"]["used_pct"] == 68 and m["five_hour"]["note"].startswith("resets")
    assert m["seven_day"]["left_pct"] == 70 and m["seven_day"]["level"] == "ok"
    parts = client.get("/dashboard/api/cecil").json()["html"]["charts"]
    assert "Claude plan" in parts and "32% left" in parts and "70% left" in parts and "Weekly" in parts
    assert "runs" not in parts and "Claude today" not in parts
    assert "runs today" not in client.get("/dashboard/").text
    # the 5-hour window resets with no new reading: unknown, never "100% left"
    clock.advance(hours=3)
    m = {x["key"]: x for x in home_live.plan_meters()["meters"]}
    assert m["five_hour"]["left_pct"] is None and m["five_hour"]["level"] == "unknown"
    assert "new figure after the next run" in m["five_hour"]["note"]
    parts = client.get("/dashboard/api/cecil").json()["html"]["charts"]
    assert "100% left" not in parts and "70% left" in parts
    u = usage.usage_status()["windows"]["five_hour"]
    assert u["stale"] and u["left_pct"] is None and u["reset_was_text"]


def test_five_hour_refreshes_when_only_weekly_is_reported(client, clock):
    """The CLI reports only the binding window. Once weekly is tighter, the 5-hour window stops being
    reported; a stale or rejected 5-hour reading must give way to the bound weekly implies."""
    from assistant import usage

    now = int(db.utcnow().timestamp())
    usage.record_window({"status": "rejected", "rateLimitType": "five_hour", "resetsAt": now + 3600})
    clock.advance(hours=2)                       # 5-hour window resets; no 5-hour event ever comes again
    usage.record_window({"status": "allowed_warning", "rateLimitType": "seven_day", "utilization": 0.75,
                         "resetsAt": now + 86400 * 5})
    w = usage.usage_status()["windows"]["five_hour"]
    assert w["status"] == "allowed" and w["left_pct"] is None and not w["stale"]
    assert w["left_at_least_pct"] == 25 and w["inferred_from"] == "seven_day" and w["as_of_text"]
    m = {x["key"]: x for x in home_live.plan_meters()["meters"]}
    assert m["five_hour"]["left_at_least_pct"] == 25 and "weekly limit is the tighter" in m["five_hour"]["note"]
    assert m["seven_day"]["left_pct"] == 25
    parts = client.get("/dashboard/api/cecil").json()["html"]["charts"]
    assert "&ge;25% left" in parts and "100% left" not in parts
    assert not usage.constrained(0.99)[0]       # a bound is not a rejection
    # each further run refreshes the bound
    usage.record_window({"status": "allowed_warning", "rateLimitType": "seven_day", "utilization": 0.8,
                         "resetsAt": now + 86400 * 5})
    assert usage.usage_status()["windows"]["five_hour"]["left_at_least_pct"] == 20
    # a real 5-hour reading wins, and is not overwritten by later weekly events
    usage.record_window({"status": "allowed", "rateLimitType": "five_hour", "utilization": 0.1, "resetsAt": now + 4 * 3600})
    usage.record_window({"status": "allowed_warning", "rateLimitType": "seven_day", "utilization": 0.81,
                         "resetsAt": now + 86400 * 5})
    w = usage.usage_status()["windows"]["five_hour"]
    assert w["left_pct"] == 90 and w["left_at_least_pct"] is None and w["inferred_from"] is None


def test_no_inference_from_rejection_or_model_windows():
    from assistant import usage

    in1d = int((db.utcnow() + timedelta(days=1)).timestamp())
    usage.record_window({"status": "rejected", "rateLimitType": "seven_day", "utilization": 1.0, "resetsAt": in1d})
    usage.record_window({"status": "allowed", "rateLimitType": "seven_day_opus", "utilization": 0.2, "resetsAt": in1d})
    assert "five_hour" not in usage.usage_status()["windows"]


def test_plan_tile_before_any_reading():
    m = home_live.plan_meters()["meters"]
    assert [x["note"] for x in m] == ["no reading yet", "no reading yet"] and all(x["left_pct"] is None for x in m)


# -- Sterling -> Cecil ---------------------------------------------------------------------------

def test_default_name_is_cecil():
    assert asettings.AssistantSettings().name == "Cecil" == asettings.DEFAULT_NAME


def _write_config(text: str):
    p = config.get_settings().config_file
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    config_store.reload()
    return p


def test_name_migration_only_from_the_old_default_and_only_once(monkeypatch):
    monkeypatch.delenv("ASSISTANT_NAME", raising=False)
    p = _write_config("# mine\nASSISTANT_NAME=Sterling\nOWNER_NAME=Blake\n")
    assert asettings.migrate_legacy_name() is True
    text = p.read_text()
    assert "ASSISTANT_NAME=Cecil" in text and "# mine" in text and "OWNER_NAME=Blake" in text
    assert asettings.get_assistant_settings().name == "Cecil"
    # he chooses Sterling again afterwards: it sticks (one-time)
    _write_config("ASSISTANT_NAME=Sterling\n")
    assert asettings.migrate_legacy_name() is False and "Sterling" in p.read_text()


def test_name_migration_leaves_a_chosen_name_alone(monkeypatch):
    monkeypatch.delenv("ASSISTANT_NAME", raising=False)
    p = _write_config("ASSISTANT_NAME=Alfred\n")
    assert asettings.migrate_legacy_name() is False and "ASSISTANT_NAME=Alfred" in p.read_text()
    db.kv_delete(asettings.NAME_MIGRATION_KV)
    p = _write_config("ASSISTANT_NAME=Sterling Archer\n")
    assert asettings.migrate_legacy_name() is False and "Sterling Archer" in p.read_text()
