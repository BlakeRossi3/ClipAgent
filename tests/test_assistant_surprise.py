"""assistant/surprise.py: the daily "make him something cool" job (offline, fake background runner)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from assistant_fixtures import assistant_env  # noqa: F401
from fastapi.testclient import TestClient

import db
import main
import server
from assistant import schedule, store, surprise, tasks, usage, vault
from assistant import settings as asettings

pytestmark = pytest.mark.usefixtures("assistant_env")


@pytest.fixture
def on(monkeypatch, clock):
    monkeypatch.setenv("ASSISTANT_ENABLED", "true")
    asettings.reset_assistant_settings()
    store.init_store()
    runs = []

    def runner(brief, context):
        runs.append((brief, context))
        day = db.local_today().isoformat()
        p = surprise.html_path(day)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("<!doctype html><title>orrery</title><script>document.title='x'</script>", encoding="utf-8")
        return "TITLE: A Pocket Orrery\nI made a tiny solar system you can spin. Drag to speed it up.", "ok"

    tasks.set_runner(runner)
    yield runs
    tasks.set_runner(None)


def _wait():
    for t in tasks.list_tasks():
        tasks.wait(t["id"])


def test_job_follows_the_setting(on, monkeypatch):
    spec = next(j for j in main.job_specs() if j.id == "assistant_morning_surprise")
    assert spec.args == {"hour": 7, "minute": 40}
    monkeypatch.setenv("ASSISTANT_MORNING_SURPRISE", "06:05")
    asettings.reset_assistant_settings()
    assert next(j for j in main.job_specs() if j.id == "assistant_morning_surprise").args == {"hour": 6, "minute": 5}
    monkeypatch.setenv("ASSISTANT_MORNING_SURPRISE", "")
    asettings.reset_assistant_settings()
    assert "assistant_morning_surprise" not in [j.id for j in main.job_specs()]
    assert surprise.run_morning() == "off"


def test_pick_kind_avoids_recent_ones():
    day = date(2026, 11, 13)
    past = [{"kind": k} for k in surprise.KIND_KEYS[:5]]
    for i in range(20):
        assert surprise.pick_kind(day + timedelta(days=i), past) not in surprise.KIND_KEYS[:5]
    assert surprise.pick_kind(day, past) == surprise.pick_kind(day, past)          # stable for the day


def test_no_stories_or_news_formats():
    # he said no stories (the robot-vacuum one), no news, no 'on this day'
    for gone in ("story", "history", "deep_dive", "playlist"):
        assert gone not in surprise.KIND_KEYS
    for k, what in surprise.KINDS:
        assert "story" not in what.lower()


def test_theme_comes_from_his_interests_and_rotates():
    assert {"sports", "food", "gym", "coding", "technology", "ai"} <= set(surprise.THEME_KEYS)
    day = date(2026, 11, 13)
    past = [{"theme": t} for t in surprise.THEME_KEYS[:3]]
    for i in range(20):
        assert surprise.pick_theme(day + timedelta(days=i), past) not in surprise.THEME_KEYS[:3]
    assert surprise.pick_theme(day, past) == surprise.pick_theme(day, past)


def test_brief_is_about_him_not_a_template(on, clock):
    text = surprise.brief(db.local_today(), "game", "gym")
    assert "Profile/Preferences.md" in text and "Profile/About.md" in text        # look at what he likes first
    assert "no stories" in text.lower()
    assert dict(surprise.THEMES)["gym"] in text


def test_morning_run_makes_files_and_announces_once(on, clock):
    assert surprise.run_morning() == "started"
    assert surprise.run_morning() == "skipped"                                     # once a day
    _wait()
    brief, ctx = on[0]
    assert "what he's into" in brief and ctx.get("effort") == "medium" and ctx.get("model")
    item = surprise.latest()
    assert item["theme"] in surprise.THEME_KEYS and item["kind"] in surprise.KIND_KEYS
    assert item["title"] == "A Pocket Orrery" and item["html_url"] == "/dashboard/surprises/2026-11-13.html"
    note = vault.read_note("Agent/Surprises/2026-11-13.md")
    assert "tiny solar system" in note.body
    d = store.list_deferred_finished(5)[0]
    assert d.kind == "surprise" and d.status == "done"
    assert not any(m.kind == "task_done" for m in store.list_messages(20, channel="voice"))   # no 7:40 announcement
    line = surprise.announcement()
    assert "A Pocket Orrery" in line and surprise.announcement() == ""
    from voice import web

    assert "Pocket Orrery" not in (web.activity().get("briefing") or "")             # already said once
    c = TestClient(server.create_app())
    r = c.get(item["html_url"])
    assert r.status_code == 200 and "sandbox allow-scripts" in r.headers["content-security-policy"]
    assert "allow-same-origin" not in r.headers["content-security-policy"]
    assert c.get("/dashboard/surprises/..%2Fconfig.html").status_code == 404
    page = c.get("/dashboard/scheduled").text
    assert "A Pocket Orrery" in page and "Open it" in page


def test_todays_surprise_plays_right_on_the_main_page(on, clock):
    """He asked (2026-11-18): open the Jarvis page and see it there, not on the Scheduled page; and the
    7:45 panel showed the raw 'TITLE: ... IDEA: ...' lines instead of a readable summary."""
    tasks.set_runner(lambda brief, ctx: (_make_toy() or
                                         "TITLE: Focaccia Quiz\nIDEA: Protein ranking - log your breakfasts.\n"
                                         "Ten questions on café food. Answers reveal as you go.", "ok"))
    surprise.run_morning()
    _wait()
    card = surprise.home_card()
    assert card["title"] == "Focaccia Quiz" and card["html_url"].endswith(f"{db.local_today()}.html")
    assert "TITLE:" not in card["summary"] and "IDEA:" not in card["summary"]
    assert "Ten questions" in card["summary"]
    c = TestClient(server.create_app())
    r = c.get("/dashboard/")
    assert "Focaccia Quiz" in r.text and f'src="{card["html_url"]}"' in r.text and 'sandbox="allow-scripts"' in r.text
    assert "'self'" in r.headers["content-security-policy"].split("frame-src")[1].split(";")[0]
    toy = c.get(card["html_url"])
    assert "frame-ancestors 'self'" in toy.headers["content-security-policy"]
    assert toy.headers.get("x-frame-options", "SAMEORIGIN") == "SAMEORIGIN"
    line = surprise.announcement()
    assert "Focaccia Quiz" in line and "Scheduled page" not in line
    clock.advance(days=1)                                                          # yesterday's leaves the page
    assert surprise.home_card() is None and "surprise-frame" not in c.get("/dashboard/").text


def _make_toy():
    p = surprise.html_path(db.local_today().isoformat())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("<!doctype html><title>quiz</title><p>hi</p>", encoding="utf-8")
    return ""


def test_tight_usage_waits_for_the_reset(on, clock):
    resets = int((db.utcnow() + timedelta(hours=1)).timestamp())
    usage.record_window({"status": "allowed_warning", "utilization": 0.85, "resetsAt": resets,
                         "rateLimitType": "five_hour"})
    assert surprise.run_morning() == "deferred"
    q = [d for d in store.list_deferred("queued") if d.kind == "surprise"]
    assert len(q) == 1 and db.parse_iso(q[0].run_after) > db.utcnow() + timedelta(minutes=59)
    assert not on
    assert any(x["kind"] == "surprise" for x in schedule.snapshot()["upcoming"])
