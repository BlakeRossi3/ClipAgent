"""assistant/nfl.py: parse ESPN's scoreboard, build the scoreboard tile (his team first), tool, Monday jobs."""

from __future__ import annotations

import pytest
from assistant_fixtures import assistant_env  # noqa: F401
from fastapi import FastAPI
from fastapi.testclient import TestClient

import dashboard
import db
from assistant import nfl, store, tiles, tools

pytestmark = pytest.mark.usefixtures("assistant_env")


def _team(tid, abbr, name, score, home, winner=None, record=""):
    return {"homeAway": "home" if home else "away", "score": str(score), "winner": winner,
            "team": {"id": tid, "abbreviation": abbr, "shortDisplayName": name},
            "records": [{"type": "total", "summary": record}] if record else []}


def _leader(name, player, line, value, tid):
    return {"name": name, "leaders": [{"displayValue": line, "value": value,
                                       "athlete": {"displayName": player}, "team": {"id": tid}}]}


def _event(date, state, detail, away, home, *, leaders=(), headline="", neutral=False):
    return {"date": date, "competitions": [{
        "date": date, "neutralSite": neutral, "status": {"type": {"state": state, "shortDetail": detail}},
        "competitors": [home, away], "leaders": list(leaders),
        "headlines": [{"shortLinkText": headline}] if headline else []}]}


def payload():
    return {"week": {"number": 10, "teamsOnBye": [{"abbreviation": "CHI"}]}, "events": [
        _event("2026-11-17T01:15Z", "pre", "11/16 - 8:15 PM EST",
               _team("24", "LAC", "Chargers", 0, False), _team("33", "BAL", "Ravens", 0, True)),
        _event("2026-11-15T18:00Z", "post", "Final",
               _team("25", "SF", "49ers", 46, False, True), _team("6", "DAL", "Cowboys", 40, True, False),
               leaders=[_leader("passingYards", "Brock Purdy", "25/31, 340 YDS, 3 TD", 340, "25")]),
        _event("2026-11-15T21:00Z", "post", "Final",
               _team("14", "LAR", "Rams", 34, False, True), _team("22", "ARI", "Cardinals", 6, True, False)),
        _event("2026-11-13T01:15Z", "post", "Final/OT",
               _team("28", "WSH", "Commanders", 24, False, True, "3-5-1"),
               _team("19", "NYG", "Giants", 21, True, False, "5-3-1"),
               leaders=[_leader("rushingYards", "Cam Skattebo", "18 CAR, 78 YDS, 2 TD", 78, "19"),
                        _leader("passingYards", "Jayden Daniels", "20/30, 250 YDS", 250, "28")],
               headline="Commanders rally to stun the Giants"),
        _event("2026-11-15T14:30Z", "post", "Final",
               _team("17", "NE", "Patriots", 10, False, False), _team("8", "DET", "Lions", 35, True, True),
               neutral=True),
    ]}


@pytest.fixture
def env(clock):
    store.init_store()
    calls = []
    nfl.set_http(lambda url: calls.append(url) or payload())
    yield calls
    nfl.set_http(None)


def test_parse_reads_scores_records_leaders_and_byes(env):
    s = nfl.parse(payload())
    assert s.week == 10 and s.byes == ["CHI"] and len(s.games) == 5
    assert s.games[0].home.abbr == "NYG"  # sorted by kickoff
    g = s.games[0]
    assert g.overtime and g.home.record == "5-3-1" and g.away.winner and g.home.score == 21
    assert [ld.team for ld in g.leaders] == ["NYG", "WSH"] and g.headline.startswith("Commanders")
    assert s.games[-1].state == "pre" and s.games[-1].home.score is None
    assert nfl.game_row(s.games[1]) == "NE 10 vs DET 35"


def test_tile_puts_the_giants_first_with_the_week_stats(env):
    spec = nfl.build_tile(nfl.parse(payload()), "NYG")
    assert spec["value"] == "NYG 21-24 L" and spec["title"] == "NFL week 10 scores · 4/5 final"
    assert spec["body"].splitlines()[0] == "Giants lost to Commanders 21-24 in overtime, now 5-3-1."
    assert "NYG star: Cam Skattebo" in spec["body"] and "Shootout: SF 46 @ DAL 40 (86 pts)" in spec["body"]
    assert "Rout: LAR by 28" in spec["body"]
    rows = spec["rows"]
    assert rows[0] == {"text": "WSH 24 @ NYG 21", "note": "F/OT", "mark": True}
    assert rows[-1]["text"] == "LAC @ BAL" and rows[-1]["note"] == "Mon 8:15 PM"  # New York time
    assert not any(r["mark"] for r in rows[1:])
    assert nfl.insights(nfl.parse(payload()), "NYG")[-1].startswith("Top passer: Brock Purdy (SF)")


def test_bye_week_and_upcoming_game(env):
    s = nfl.parse(payload())
    assert nfl.team_result(s, "CHI") == ("CHI bye", "CHI on a bye this week.")
    assert nfl.team_result(s, "BAL")[0] == "BAL Mon 8:15 PM"


def test_show_puts_one_wide_tile_up_and_refresh_replaces_it(env):
    r = nfl.show()
    assert r["team"] == "NYG" and r["headline"] == "NYG 21-24 L" and "1 game still to finish" in r["summary"]
    assert env == [nfl.SCOREBOARD_URL]
    nfl.show()
    live = tiles.live()
    assert len(live) == 1 and live[0].tile_key == nfl.TILE_KEY and len(live[0].rows) == 5
    assert (db.parse_iso(live[0].expires_at) - db.utcnow()).total_seconds() == nfl.DEFAULT_MINUTES * 60
    app = FastAPI()
    app.include_router(dashboard.router)
    html = TestClient(app, base_url="http://testserver").get("/dashboard/api/home").json()["html"]["dyn_tiles"]
    assert "dtile-wide" in html and 'class="is-mark"' in html and "WSH 24 @ NYG 21" in html


def test_tool_and_team_preference(env):
    tools.load_extensions()
    from assistant import registry

    main = registry.load_agents(skills=registry.load_skills(), known_tools=tools.known_tool_names())["main"]
    assert "nfl_scores" in main.tools
    ctx = tools.ToolContext(channel="voice", agent="main", source="owner")
    r = tools.dispatch("nfl_scores", {"team": "sf", "remember_team": True}, ctx, allowed=set(main.tools))
    assert r.ok and "SF 46-40 W" in r.content and nfl.get_team() == "SF"


def test_feed_failure_is_a_tool_error_and_the_job_just_logs(env):
    def boom(url):
        raise OSError("offline")

    nfl.set_http(boom)
    tools.load_extensions()
    ctx = tools.ToolContext(channel="voice", agent="main", source="owner")
    r = tools.dispatch("nfl_scores", {}, ctx, allowed={"nfl_scores"})
    assert not r.ok and "offline" in r.content
    assert nfl.scheduled() == "skipped" and tiles.live() == []
    nfl.set_http(lambda url: {"nope": 1})
    with pytest.raises(nfl.NFLError):
        nfl.fetch()


def test_monday_jobs_are_scheduled(env, monkeypatch):
    import main
    from assistant import settings as aset

    monkeypatch.setenv("ASSISTANT_ENABLED", "true")
    aset.reset_assistant_settings()
    specs = {s.id: s for s in main.assistant_job_specs()}
    assert specs["assistant_nfl_monday"].args == {"day_of_week": "mon", "hour": 8, "minute": 5}
    assert specs["assistant_nfl_mnf"].args["day_of_week"] == "mon"
