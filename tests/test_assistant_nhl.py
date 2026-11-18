"""assistant/nhl.py: the Islanders season schedule, the live/final game tile, the every-minute job, tool, Scheduled page."""

from __future__ import annotations

from datetime import timedelta

import pytest
from assistant_fixtures import assistant_env  # noqa: F401

import db
from assistant import nhl, schedule, store, tiles, tools

pytestmark = pytest.mark.usefixtures("assistant_env")

GAME = 2026020336


def _team(abbr, name, score=None):
    t = {"abbrev": abbr, "commonName": {"default": name}}
    if score is not None:
        t["score"] = score
    return t


def sched_payload():
    return {"games": [
        {"id": 2026010008, "gameType": 1, "startTimeUTC": "2026-09-20T17:00:00Z", "gameState": "FINAL",
         "awayTeam": _team("NYI", "Islanders", 1), "homeTeam": _team("NJD", "Devils", 2)},   # preseason: dropped
        {"id": 2026020100, "gameType": 2, "startTimeUTC": "2026-11-08T00:00:00Z", "gameState": "OFF",
         "awayTeam": _team("NYI", "Islanders", 3), "homeTeam": _team("BOS", "Bruins", 2),
         "gameOutcome": {"lastPeriodType": "REG"}},
        {"id": 2026020110, "gameType": 2, "startTimeUTC": "2026-11-10T00:00:00Z", "gameState": "OFF",
         "awayTeam": _team("PIT", "Penguins", 4), "homeTeam": _team("NYI", "Islanders", 3),
         "gameOutcome": {"lastPeriodType": "OT"}},
        {"id": 2026020120, "gameType": 2, "startTimeUTC": "2026-11-11T00:00:00Z", "gameState": "OFF",
         "awayTeam": _team("NYR", "Rangers", 5), "homeTeam": _team("NYI", "Islanders", 1),
         "gameOutcome": {"lastPeriodType": "REG"}},
        {"id": GAME, "gameType": 2, "startTimeUTC": "2026-11-14T00:00:00Z", "gameState": "FUT",
         "gameScheduleState": "OK", "venue": {"default": "UBS Arena"},
         "tvBroadcasts": [{"network": "MSGSN", "countryCode": "US"}, {"network": "SNO", "countryCode": "CA"}],
         "awayTeam": _team("CBJ", "Blue Jackets"), "homeTeam": _team("NYI", "Islanders")},
        {"id": 2026020400, "gameType": 2, "startTimeUTC": "2026-11-16T00:30:00Z", "gameState": "FUT",
         "awayTeam": _team("NYI", "Islanders"), "homeTeam": _team("TOR", "Maple Leafs")},
    ]}


def _skater(name, pos, num, g=0, a=0, sog=0, toi="18:00", pm=0, hits=0, blocks=0, pim=0, ppg=0):
    return {"name": {"default": name}, "position": pos, "sweaterNumber": num, "goals": g, "assists": a,
            "points": g + a, "sog": sog, "toi": toi, "plusMinus": pm, "hits": hits, "blockedShots": blocks,
            "pim": pim, "powerPlayGoals": ppg}


def box(state="LIVE", period=2, clock="04:12", intermission=False, away=1, home=3, ptype="REG"):
    return {"id": GAME, "gameState": state, "startTimeUTC": "2026-11-14T00:00:00Z", "venue": {"default": "UBS Arena"},
            "periodDescriptor": {"number": period, "periodType": ptype},
            "clock": {"timeRemaining": clock, "inIntermission": intermission},
            "awayTeam": {**_team("CBJ", "Blue Jackets", away), "sog": 19},
            "homeTeam": {**_team("NYI", "Islanders", home), "sog": 27},
            "playerByGameStats": {
                "awayTeam": {"forwards": [_skater("Z. Werenski", "D", 8, g=1, sog=4, toi="24:01", hits=2, blocks=3)],
                             "defense": [_skater("I. Provorov", "D", 9, hits=4, pim=2)],
                             "goalies": [{"name": {"default": "E. Merzlikins"}, "saveShotsAgainst": "24/27",
                                          "toi": "40:00", "decision": "L" if state == "OFF" else ""},
                                         {"name": {"default": "J. Backup"}, "toi": "00:00"}]},
                "homeTeam": {"forwards": [_skater("B. Horvat", "C", 14, g=1, a=1, sog=5, pm=2, hits=1, ppg=1),
                                          _skater("M. Barzal", "C", 13, a=2, sog=3, hits=3),
                                          _skater("A. Lee", "L", 27, sog=1)],
                             "defense": [_skater("M. Schaefer", "D", 48, g=1, sog=2, toi="22:10", blocks=2, pim=2)],
                             "goalies": [{"name": {"default": "I. Sorokin"}, "shotsAgainst": 19, "saves": 18,
                                          "toi": "40:00", "decision": "W" if state == "OFF" else ""}]}}}


def landing(stars=False):
    goal = {"name": {"default": "M. Schaefer"}, "teamAbbrev": {"default": "NYI"}, "goalsToDate": 3,
            "timeInPeriod": "05:48", "strength": "pp", "goalModifier": "none"}
    s = {"scoring": [{"periodDescriptor": {"number": 1, "periodType": "REG"},
                      "goals": [{"name": {"default": "B. Horvat"}, "teamAbbrev": {"default": "NYI"},
                                 "timeInPeriod": "02:00", "strength": "ev"}]},
                     {"periodDescriptor": {"number": 2, "periodType": "REG"}, "goals": [goal]}]}
    if stars:
        s["threeStars"] = [
            {"star": 2, "name": {"default": "B. Horvat"}, "teamAbbrev": "NYI", "position": "C", "goals": 1, "assists": 1},
            {"star": 1, "name": {"default": "M. Schaefer"}, "teamAbbrev": "NYI", "position": "D", "goals": 1, "assists": 0},
            {"star": 3, "name": {"default": "I. Sorokin"}, "teamAbbrev": "NYI", "position": "G", "savePctg": 0.947}]
    return {"summary": s}


class Feed:
    def __init__(self):
        self.calls: list[str] = []
        self.box = box()
        self.land = landing()
        self.fail = False

    def __call__(self, url):
        self.calls.append(url)
        if self.fail:
            raise OSError("offline")
        if "club-schedule-season" in url:
            return sched_payload()
        if url.endswith("/boxscore"):
            return self.box
        return self.land


@pytest.fixture
def feed(clock):
    store.init_store()
    f = Feed()
    nhl.set_http(f)
    yield f
    nhl.set_http(None)


def test_season_schedule_keeps_regular_season_and_computes_the_record(feed):
    games = nhl.refresh_schedule()
    assert nhl.season_id() == "20262027"
    assert feed.calls == [f"{nhl.API}/club-schedule-season/NYI/20262027"]
    assert [g.id for g in games][0] == 2026020100 and len(games) == 5   # preseason dropped, sorted
    g = next(x for x in games if x.id == GAME)
    assert g.venue == "UBS Arena" and g.tv == ["MSGSN"] and g.opponent("NYI") == ("CBJ", "Blue Jackets")
    assert nhl.record(games, "NYI") == "1-1-1"   # W at BOS, OTL to PIT, L to NYR
    nhl.refresh_schedule()
    assert len(feed.calls) == 1   # cached


def test_schedule_fetch_failure_keeps_the_cache_and_backs_off(feed, clock):
    nhl.refresh_schedule()
    clock.advance(hours=13)
    feed.fail = True
    assert len(nhl.refresh_schedule()) == 5 and len(feed.calls) == 2
    nhl.refresh_schedule()
    assert len(feed.calls) == 2   # not retried for RETRY_MIN
    db.kv_delete(nhl.SCHEDULE_KV)
    with pytest.raises(nhl.NHLError):
        nhl.refresh_schedule(force=True)


def test_parse_live_game(feed):
    lv = nhl.parse_game(box(), landing())
    assert lv.state == "live" and nhl._status(lv) == "2nd 4:12"
    assert (lv.away.sog, lv.home.sog, lv.home.hits, lv.away.hits, lv.away.blocks, lv.home.ppg) == (19, 27, 4, 6, 3, 1)
    assert [g.name for g in lv.goalies] == ["E. Merzlikins", "I. Sorokin"]   # the backup never played
    assert lv.goalies[0].saves == 24 and lv.goalies[0].shots == 27 and lv.goalies[1].pct == ".947"
    assert lv.last_goal == "M. Schaefer (3), NYI, 2nd 5:48, power play"
    assert [p.name for p in nhl.key_players(lv, "NYI", 2)] == ["B. Horvat", "M. Barzal"]
    assert nhl._status(nhl.parse_game(box(intermission=True, clock="18:00"))) == "2nd intermission"
    assert nhl._status(nhl.parse_game(box(period=4, ptype="OT", clock="03:01"))) == "OT 3:01"
    assert nhl.period_label(6, "OT") == "3OT" and nhl.period_label(5, "SO") == "SO"


def test_live_tile_has_score_shots_players_and_goalies(feed):
    spec = nhl.game_tile(nhl.parse_game(box(), landing()), "NYI")
    assert spec["title"] == "Islanders live · 2nd 4:12" and spec["value"] == "CBJ 1 @ NYI 3"
    assert spec["body"].splitlines()[0] == "Shots on goal: CBJ 19, NYI 27"
    rows = {r["text"]: r for r in spec["rows"]}
    assert rows["Shots on goal"]["note"] == "CBJ 19 · NYI 27"
    assert rows["B. Horvat #14 C"]["note"] == "1G 1A · 5 SOG · +2 · 18:00 TOI" and rows["B. Horvat #14 C"]["mark"]
    assert rows["I. Sorokin G (NYI)"]["note"] == "18/19 saves · .947" and rows["I. Sorokin G (NYI)"]["mark"]
    assert "Z. Werenski D (CBJ)" in rows and not rows["Z. Werenski D (CBJ)"]["mark"]
    assert "Islanders lead Blue Jackets 3 to 1, 2nd 4:12. Shots 27 to 19." == spec["summary"]
    assert len(spec["rows"]) <= tiles.ROWS_MAX and len(spec["body"]) <= tiles.BODY_MAX


def test_final_tile_shows_the_three_stars(feed):
    spec = nhl.game_tile(nhl.parse_game(box(state="OFF", period=4, ptype="OT", clock="00:00"), landing(stars=True)), "NYI")
    assert spec["title"] == "Islanders final · Final/OT"
    assert "Stars: 1. M. Schaefer, 2. B. Horvat, 3. I. Sorokin" in spec["body"]
    star_rows = [r for r in spec["rows"] if r["text"].startswith("★")]
    assert [r["text"] for r in star_rows] == ["★1 M. Schaefer (NYI)", "★2 B. Horvat (NYI)", "★3 I. Sorokin (NYI)"]
    assert star_rows[2]["note"] == "SV% .947"
    assert spec["summary"] == "Islanders beat Blue Jackets 3 to 1 in overtime. First star: M. Schaefer."


def test_tick_follows_a_game_from_preview_to_final(feed, clock):
    # 10:00 New York, game at 19:00: nothing yet, and no game-feed calls
    assert nhl.tick() == "idle" and tiles.live() == []
    clock.set(db.parse_iso("2026-11-13T23:00:00Z"))     # an hour before puck drop
    assert nhl.tick() == "preview"
    t = tiles.live()[0]
    assert t.title == "Islanders tonight" and t.value == "CBJ @ NYI" and "7:00 PM at UBS Arena" in t.body
    assert "TV: MSGSN." in t.body and "Islanders 1-1-1." in t.body
    assert not any("gamecenter" in c for c in feed.calls)

    clock.set(db.parse_iso("2026-11-14T00:40:00Z"))     # second period
    assert nhl.tick() == "live"
    live = tiles.live()
    assert len(live) == 1 and live[0].tile_key == nhl.TILE_KEY and live[0].value == "CBJ 1 @ NYI 3"
    first = live[0].id
    clock.advance(minutes=1)
    assert nhl.tick() == "live" and tiles.live()[0].id == first      # unchanged -> not rewritten
    feed.box = box(away=1, home=4, clock="02:00")
    clock.advance(minutes=1)
    nhl.tick()
    assert tiles.live()[0].value == "CBJ 1 @ NYI 4" and tiles.live()[0].id != first

    feed.box, feed.land = box(state="OFF", period=3, clock="00:00", home=4), landing(stars=True)
    clock.advance(hours=1)
    assert nhl.tick() == "final"
    t = tiles.live()[0]
    assert t.title == "Islanders final · Final" and any(r["text"].startswith("★1") for r in t.rows)
    assert (db.parse_iso(t.expires_at) - db.utcnow()) == timedelta(minutes=nhl.FINAL_MINUTES)
    n = len(feed.calls)
    clock.advance(minutes=1)
    assert nhl.tick() == "idle" and len(feed.calls) == n            # done: no more polling


def test_closing_the_tile_hides_that_game_until_he_asks(feed, clock):
    clock.set(db.parse_iso("2026-11-14T00:40:00Z"))
    nhl.tick()
    tiles.remove(tiles.live()[0].id, by="owner")
    feed.box = box(home=5)
    clock.advance(minutes=1)
    nhl.tick()
    assert tiles.live() == [] and nhl.tick() == "muted"
    r = nhl.show_now()
    assert r["status"] == "live" and tiles.live()[0].value == "CBJ 1 @ NYI 5"
    clock.advance(minutes=1)
    assert nhl.tick() == "live"


def test_feed_failure_mid_game_just_logs(feed, clock):
    clock.set(db.parse_iso("2026-11-14T00:40:00Z"))
    nhl.refresh_schedule()
    feed.fail = True
    assert nhl.tick() == "skipped"
    assert any("islanders tile not updated" in e.message for e in db.list_events(limit=5))


def test_tool_show_and_schedule(feed, clock):
    tools.load_extensions()
    from assistant import registry

    main = registry.load_agents(skills=registry.load_skills(), known_tools=tools.known_tool_names())["main"]
    assert "nhl_games" in main.tools
    ctx = tools.ToolContext(channel="voice", agent="main", source="owner")
    r = tools.dispatch("nhl_games", {"action": "schedule", "limit": 2}, ctx, allowed=set(main.tools))
    assert r.ok and "1-1-1" in r.content and "CBJ @ NYI" in r.content and "NYI @ TOR" in r.content
    r = tools.dispatch("nhl_games", {}, ctx, allowed=set(main.tools))   # 10:00, game tonight -> next game preview
    assert r.ok and "Islanders vs Blue Jackets tonight at 7:00 PM" in r.content
    assert tiles.live()[0].title == "Islanders tonight"
    feed.fail = True
    db.kv_delete(nhl.SCHEDULE_KV)
    r = tools.dispatch("nhl_games", {"action": "refresh"}, ctx, allowed=set(main.tools))
    assert not r.ok and "offline" in r.content


def test_games_show_on_the_scheduled_page_and_the_job_is_registered(feed, clock, monkeypatch):
    nhl.refresh_schedule()
    snap = schedule.snapshot()
    games = [x for x in snap["upcoming"] if x["kind"] == "game"]
    assert [g["title"] for g in games] == ["Islanders vs Blue Jackets", "Islanders at Maple Leafs"]
    assert games[0]["kind_label"] == "Islanders game" and "MSGSN" in games[0]["detail"]

    import main
    from assistant import settings as aset

    monkeypatch.setenv("ASSISTANT_ENABLED", "true")
    aset.reset_assistant_settings()
    specs = {s.id: s for s in main.assistant_job_specs()}
    assert specs["assistant_nhl_live"].trigger == "interval" and specs["assistant_nhl_live"].args == {"minutes": 1}
