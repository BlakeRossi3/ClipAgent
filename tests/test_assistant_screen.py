"""Cecil can see what is on the Jarvis page and take it down (assistant/screen.py)."""

from __future__ import annotations

import pytest

import db
from assistant import events, hud, screen, status, tiles, tools
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")


def _clip_panels(n=4):
    out = []
    for i in range(n, 0, -1):
        p = hud.make_panel("video", f"Clip {i}", {"clip_id": i, "caption": f"caption {i}"}, hold="sticky", turn="t0")
        out.append(hud.publish_panel(p))
    return out


def test_panels_he_sees_are_listed_and_in_every_turn(clock):
    _clip_panels()
    tiles.show("Weather", "72F", key="weather")
    names = [p["title"] for p in screen.panels()]
    assert names == ["CLIP 4", "CLIP 3", "CLIP 2", "CLIP 1"] or names == ["Clip 4", "Clip 3", "Clip 2", "Clip 1"]
    line = screen.summary_line()
    assert "4 panels you put up" in line and "video" in line and '"Weather"' in line
    assert line in status.turn_context()


def test_clear_all_or_one_by_title_and_the_page_is_told(clock):
    _clip_panels()
    title = screen.panels()[0]["title"]
    out = screen.clear(title)
    assert out["removed_panels"] == 1 and title not in out["left"]
    assert any(e["type"] == "panel_remove" for e in events.BUS._events)
    assert screen.clear("nothing like this")["removed_panels"] == 0
    tiles.show("Score", "NYG 21")
    out = screen.clear("all")
    assert out["removed_panels"] == 3 and out["removed_tiles"] == 1
    assert screen.panels() == [] and tiles.live() == [] and screen.summary_line() == ""


def test_he_can_close_a_panel_himself(clock):
    p = _clip_panels(1)[0]
    assert screen.dismiss(p["id"]) and screen.panels() == []
    assert not screen.dismiss(p["id"])


def test_page_report_is_kept_sanitised_and_read_back(clock):
    big = "x" * 5000
    screen.record({"page": "/dashboard/", "sections": [{"key": "reels", "title": "Reels", "area": "right rail",
                                                       "shown": True, "in_view": True, "text": big}] * 100,
                   "panels": [{"id": "p1", "kind": "video", "title": "CLIP 5", "text": "Webb captured light"}],
                   "overlays": {"history": True, "evil": True}})
    rep = screen.latest()
    assert len(rep["sections"]) == screen.MAX_SECTIONS and len(rep["sections"][0]["text"]) == screen.TEXT_CHARS
    assert rep["overlays"] == {"expanded_panel": False, "history": True, "diagnostics": False}
    got = screen.read()
    assert got["page_open"] and got["page_report"]["panels"][0]["title"] == "CLIP 5"
    clock.advance(minutes=5)
    got = screen.read()
    assert not got["page_open"] and "out of date" in got["page_report_note"]


def test_tools_registered_for_cecil(clock):
    tools.load_extensions()
    _clip_panels(2)
    ctx = tools.ToolContext(channel="voice", agent="main", source="voice")
    res = tools.dispatch("screen_read", {}, ctx, allowed={"screen_read", "screen_clear"})
    assert res.ok and "CLIP" in res.content.upper()
    res = tools.dispatch("screen_clear", {"target": "panels"}, ctx, allowed={"screen_read", "screen_clear"})
    assert res.ok and '"removed_panels": 2' in res.content
    assert db.list_events(limit=5)[0].message == "screen cleared"


def test_a_text_turns_tool_process_reaches_the_page(clock):
    """Tools of a text/dashboard turn run in assistant.mcp_server (another process): their panels and
    removals travel through the kv outbox and the server publishes them."""
    p = hud.make_panel("video", "Clip 9", {"clip_id": 9}, hold="sticky", turn="t0")
    events.set_relay(True)
    try:
        hud.publish_panel(p)
        assert events.BUS.snapshot()["panels"] == []          # not in this "process" bus yet
        screen.record({"panels": [{"id": p["id"], "kind": "video", "title": "CLIP 9"}]})
        assert [x["title"] for x in screen.panels()] == ["CLIP 9"]   # seen through the page's report
    finally:
        events.set_relay(False)
    assert events.drain_outbox(force=True) == 1
    assert [x["title"] for x in screen.panels()] == [p["title"]]
    events.set_relay(True)
    try:
        screen.clear("panels")
    finally:
        events.set_relay(False)
    assert events.drain_outbox(force=True) == 1 and screen.panels() == []
    assert events.drain_outbox(force=True) == 0
