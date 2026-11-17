"""Ideas of the day are kept: the tool, the morning surprise's IDEA line, idea tiles, earlier tiles."""

from __future__ import annotations

import pytest

import db
from assistant import ideas, schedule, store, tiles, tools, vault
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")


def test_save_keeps_it_in_the_vault_and_the_list(clock):
    out = ideas.save("Shorts from old NASA reels", "Cut the 1969 footage into 30 s explainers.", why="cheap, safe")
    assert out["saved"] and out["day"] == "2026-11-13"
    note = vault.read_note(ideas.NOTE)
    assert "## 2026-11-13: Shorts from old NASA reels" in note.body and "1969 footage" in note.body
    assert ideas.list_ideas()[0]["title"] == "Shorts from old NASA reels"
    assert ideas.save("Shorts from old NASA reels", "again")["saved"] is False      # once per day
    assert len(ideas.list_ideas()) == 1


def test_idea_tiles_are_kept_after_they_expire(clock):
    tiles.show("Idea of the day: A walking playlist", "Songs at 120 bpm for your evening walk.", key="idea")
    tiles.show("Weather", "72°F", key="weather")
    kept = ideas.list_ideas()
    assert [i["title"] for i in kept] == ["A walking playlist"] and kept[0]["source"] == "tile"
    clock.advance(hours=2)
    assert tiles.live() == [] and ideas.list_ideas()[0]["title"] == "A walking playlist"


def test_morning_surprise_idea_line_is_kept(clock):
    full = "TITLE: Pocket galaxy\nIDEA: Stream your edits - Go live while cutting a Short.\nI made a toy."
    assert ideas.from_text(full, day="2026-11-13")
    i = ideas.list_ideas()[0]
    assert i["title"] == "Stream your edits" and i["idea"] == "Go live while cutting a Short."
    assert ideas.from_text("TITLE: nothing else") is False


def test_earlier_idea_tiles_are_recovered_once(clock):
    store.add_tile("Idea of the day: Desk plants", expires_at=db.now_iso(), body="A pothos for the studio corner.",
                   tile_key="idea-of-the-day")
    store.add_tile("Score", expires_at=db.now_iso(), body="NYG 21")
    assert [t.title for t in store.list_all_tiles()] == ["Idea of the day: Desk plants", "Score"]
    assert [i["title"] for i in ideas.list_ideas()] == ["Desk plants"]
    assert ideas.backfill() == 0                                 # once only


def test_tool_and_scheduled_page(clock):
    tools.load_extensions()
    ctx = tools.ToolContext(channel="voice-task", agent="main", source="voice")
    res = tools.dispatch("idea_save", {"title": "Batch-record voiceovers", "idea": "Sundays, 20 minutes."}, ctx,
                         allowed={"idea_save"})
    assert res.ok and '"saved": true' in res.content
    assert schedule.snapshot()["ideas"][0]["title"] == "Batch-record voiceovers"
