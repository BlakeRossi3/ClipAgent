"""assistant/tiles.py: short-lived tiles on the Jarvis page - lifetime, replace by key, cap, tools, page."""

from __future__ import annotations

import re

import pytest
from assistant_fixtures import assistant_env  # noqa: F401
from fastapi import FastAPI
from fastapi.testclient import TestClient

import dashboard
import db
from assistant import store, tiles, tools

pytestmark = pytest.mark.usefixtures("assistant_env")


@pytest.fixture
def env(clock):
    store.init_store()
    return clock


def test_tile_goes_up_for_an_hour_then_leaves_by_itself(env):
    t = tiles.show("Weather in Boston", "Light rain after 3", value="58°F")
    assert t.kind == "info" and t.value == "58°F"
    assert db.parse_iso(t.expires_at) - db.parse_iso(t.created_at) == __import__("datetime").timedelta(hours=1)
    assert [x.id for x in tiles.live()] == [t.id]
    env.advance(minutes=59)
    assert [x.id for x in tiles.live()] == [t.id]
    assert tiles.view()[0]["left"] == "1 min"
    env.advance(minutes=1)
    assert tiles.live() == [] and tiles.view() == []


def test_minutes_are_clamped_and_text_is_cleaned(env):
    short = tiles.show("x", minutes=0)
    long = tiles.show("y", minutes=99999)
    assert (db.parse_iso(short.expires_at) - db.utcnow()).total_seconds() == 60
    assert (db.parse_iso(long.expires_at) - db.utcnow()).total_seconds() == tiles.MAX_MINUTES * 60
    t = tiles.show("  a\x00b   " + "z" * 200, "line one\n\n line\ttwo \x07", value="1" * 50)
    assert t.title.startswith("a b z") and len(t.title) == tiles.TITLE_MAX
    assert t.body == "line one\nline two" and len(t.value) == tiles.VALUE_MAX
    with pytest.raises(tiles.TileError):
        tiles.show("   ")
    with pytest.raises(tiles.TileError):
        tiles.show("ok", kind="banner")
    with pytest.raises(tiles.TileError):
        tiles.show("ok", minutes="soon")


def test_same_key_replaces_and_cap_drops_oldest(env):
    a = tiles.show("Weather", value="58°F", key="Weather")
    b = tiles.show("Weather", value="61°F", key="weather")
    live = tiles.live()
    assert [t.id for t in live] == [b.id] and store.get_tile(a.id).removed_by == "replaced"
    for i in range(tiles.MAX_LIVE):
        env.advance(seconds=1)
        tiles.show(f"n{i}")
    live = tiles.live()
    assert len(live) == tiles.MAX_LIVE and b.id not in [t.id for t in live]
    assert store.get_tile(b.id).removed_by == "overflow"


def test_remove_by_id_key_or_all_and_prune(env):
    a = tiles.show("A", key="k")
    tiles.show("B")
    assert tiles.remove(key="k") == 1 and store.get_tile(a.id).removed_at
    assert tiles.remove(a.id) == 0
    assert tiles.remove() == 1 and tiles.live() == []
    env.advance(days=tiles.KEEP_DAYS + 1)
    tiles.show("C")  # showing prunes old rows
    assert store.get_tile(a.id) is None


def test_tools_registered_for_main_agent(env):
    tools.load_extensions()
    from assistant import registry

    main = registry.load_agents(skills=registry.load_skills(), known_tools=tools.known_tool_names())["main"]
    allowed = set(main.tools)
    assert {"tile_show", "tile_remove", "tile_list"} <= allowed
    ctx = tools.ToolContext(channel="dashboard", agent="main", source="owner")
    r = tools.dispatch("tile_show", {"title": "Score", "value": "3-1", "kind": "metric", "minutes": 30}, ctx,
                       allowed=allowed)
    assert r.ok and '"3-1"' in r.content
    listed = tools.dispatch("tile_list", {}, ctx, allowed=allowed)
    assert listed.ok and "Score" in listed.content
    bad = tools.dispatch("tile_remove", {}, ctx, allowed=allowed)
    assert not bad.ok
    assert tools.dispatch("tile_remove", {"all": True}, ctx, allowed=allowed).ok and tiles.live() == []
    assert not tools.dispatch("tile_show", {"title": "x", "kind": "nope"}, ctx, allowed=allowed).ok


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(dashboard.router)
    return TestClient(app, base_url="http://testserver")


def test_jarvis_page_shows_tiles_escaped_and_owner_can_dismiss(env):
    t = tiles.show("<b>Weather</b>", "Rain later", value="58°F", kind="reminder")
    c = _client()
    html = c.get("/dashboard/").text
    assert 'data-live="dyn_tiles"' in html and "dtile-reminder" in html and "58°F" in html
    assert "&lt;b&gt;Weather&lt;/b&gt;" in html and "<b>Weather</b>" not in html
    assert f'data-expires="{tiles.view()[0]["expires_ms"]}"' in html
    j = c.get("/dashboard/api/home").json()
    assert "Rain later" in j["html"]["dyn_tiles"]
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1)
    assert c.post(f"/dashboard/tiles/{t.id}/dismiss", data={}).status_code == 403
    r = c.post(f"/dashboard/tiles/{t.id}/dismiss", data={"csrf": csrf}, headers={"Accept": "application/json"})
    assert r.status_code == 200 and r.json()["ok"]
    assert store.get_tile(t.id).removed_by == "owner" and tiles.live() == []
    again = c.post(f"/dashboard/tiles/{t.id}/dismiss", data={"csrf": csrf}, headers={"Accept": "application/json"})
    assert again.status_code == 404
    assert "dtile" not in c.get("/dashboard/api/home").json()["html"]["dyn_tiles"]


def test_tile_from_claude_code_reaches_the_open_page_and_leaves_on_time(env):
    """The path Cecil really uses: the clipagent MCP server (Claude Code) -> tile_show -> the page's
    live refresh (/dashboard/api/home) picks it up without a reload, and drops it when the hour is up."""
    from assistant import mcp_server

    tools.load_extensions()
    srv = mcp_server.Server({"CLIPAGENT_MCP_AGENT": "main", "CLIPAGENT_MCP_ALLOWED": "tile_show,tile_list",
                             "CLIPAGENT_MCP_CHANNEL": "voice", "CLIPAGENT_MCP_SOURCE": "owner"})
    assert {"tile_show", "tile_list"} <= {t["name"] for t in srv.tool_list()}
    c = _client()
    assert "dtile" not in c.get("/dashboard/api/home").json()["html"]["dyn_tiles"]  # page already open
    res = srv.call("tile_show", {"title": "Next train", "value": "7:42", "key": "train"})
    assert not res["isError"]
    live = c.get("/dashboard/api/home").json()["html"]["dyn_tiles"]
    assert "Next train" in live and "7:42" in live and "1 h left" in live and "/dismiss" in live
    env.advance(minutes=60)
    assert "Next train" not in c.get("/dashboard/api/home").json()["html"]["dyn_tiles"]


def test_cli_show_list_clear(env, capsys):
    assert tiles.main(["show", "Hello", "world", "--minutes", "5", "--key", "hi"]) == 0
    assert tiles.main(["list"]) == 0
    assert "Hello" in capsys.readouterr().out
    assert tiles.main(["clear"]) == 0 and tiles.live() == []
