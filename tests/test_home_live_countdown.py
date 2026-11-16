"""The GTA 6 release countdown tile on the Jarvis page (home_live.release_countdown, GTA6_RELEASE_DATE)."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
import home_live
import server
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")


@pytest.fixture(autouse=True)
def _no_git():
    home_live.set_git_log(lambda n: [])
    yield
    home_live.set_git_log(None)


def _use(monkeypatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("GTA6_RELEASE_DATE", raising=False)
    else:
        monkeypatch.setenv("GTA6_RELEASE_DATE", value)
    config.reset_settings()


@pytest.mark.parametrize("raw, want", [
    ("2026-11-19", "2026-11-19"),
    (" 2027-03-01 ", "2027-03-01"),
    ("2026-11-19T18:30", "2026-11-19T18:30"),
    ("2026-11-19 09:05", "2026-11-19T09:05"),
    ("", ""),
    ("next tuesday", ""),
    ("2026-13-40", ""),
])
def test_release_date_is_one_validated_config_value(raw, want):
    assert config._release_date(raw) == want
    assert config.load_settings({"GTA6_RELEASE_DATE": raw}).gta6_release_date == want


def test_default_is_the_announced_date():
    assert config.load_settings({}).gta6_release_date == "2026-11-19"
    assert "GTA6_RELEASE_DATE=2026-11-19" in (Path(__file__).resolve().parent.parent / "config.env.example").read_text("utf-8")


def test_countdown_counts_to_local_midnight(clock, monkeypatch):
    _use(monkeypatch, None)                                    # frozen 2026-11-13T15:00Z = 10:00 New York
    cd = home_live.release_countdown()
    assert cd["target_iso"] == "2026-11-19T05:00:00Z"           # midnight EST
    assert (cd["days"], cd["hours"], cd["minutes"], cd["released"]) == (5, 14, 0, False)
    assert cd["target_text"] == "Thu 19 Nov 2026" and cd["label"] == "GTA VI"


def test_countdown_with_a_time_and_after_release(clock, monkeypatch):
    _use(monkeypatch, "2026-11-13T12:47")
    cd = home_live.release_countdown()
    assert (cd["days"], cd["hours"], cd["minutes"]) == (0, 2, 47)
    after = home_live.release_countdown(now=datetime(2026, 12, 1, tzinfo=timezone.utc))
    assert after["released"] and (after["days"], after["hours"], after["minutes"]) == (0, 0, 0)


def test_no_date_no_tile(monkeypatch):
    _use(monkeypatch, "")
    assert home_live.release_countdown() is None
    assert home_live.cecil_data()["countdown"] is None
    html = TestClient(server.create_app(), base_url="http://testserver").get("/dashboard/").text
    assert "data-countdown" not in html and 'data-widget="countdown"' not in html


def test_jarvis_page_shows_a_ticking_tile(clock, monkeypatch):
    _use(monkeypatch, None)
    client = TestClient(server.create_app(), base_url="http://testserver")
    html = client.get("/dashboard/").text
    assert 'data-countdown="2026-11-19T05:00:00Z"' in html and "GTA VI release" in html
    assert 'data-cd="d">5<' in html and 'data-cd="h">14<' in html and 'data-cd="m">00<' in html
    # its own collapsible panel on the Jarvis page (static/home.js folds every [data-widget])
    assert re.search(r'<section[^>]*data-widget="countdown"[^>]*>\s*<h2 class="rail-h">GTA VI release</h2>', html)
    # the live refresh carries it too, and the page script ticks it client-side
    assert "data-countdown" in client.get("/dashboard/api/cecil").json()["html"]["countdown"]
    js = client.get("/dashboard/static/cecil.js").text
    assert "tickCountdowns" in js and "[data-countdown]" in js and "setInterval(tickCountdowns, 1000)" in js
