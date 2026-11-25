"""NFL scores on the Jarvis page: every game of the current week plus a few stats, his team first.

The owner: "I'm a big football fan and I care about the scores on Sunday... every Monday morning I want
a widget that shows all the scores from all the games and interesting stats, and specifically I'm a
Giants fan."

* Data: ESPN's public scoreboard JSON (no key). :func:`fetch` gets the current week; tests inject a
  fake with :func:`set_http` (no network in tests).
* :func:`parse` turns the payload into :class:`Slate` / :class:`Game` (pure).
* :func:`build_tile` -> title, his team's result as the big value, up to four lines of stats
  (highest-scoring game, biggest win, closest finish, best passer/rusher/receiver) and one row per game,
  his team's row marked. :func:`show` puts it up as a wide tile (key ``nfl-scores``, replaced on refresh).
  Shown on a Monday or Tuesday it stays up until Wednesday 00:00 local (:func:`until_wednesday`); other
  days, or an explicit ``minutes``, the usual lifetime.
* His team: kv ``nfl:team`` (abbreviation, default ``NYG``); the tool can change it.
* Scheduled (main.assistant_job_specs): Monday 08:05 (after Sunday) and Monday 23:40 (after Monday night).
* Tool ``nfl_scores``; CLI ``python -m assistant.nfl [show | print] [--team NYG]``.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import db

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
TILE_KEY = "nfl-scores"
TEAM_KV = "nfl:team"
DEFAULT_TEAM = "NYG"
DEFAULT_MINUTES = 16 * 60
LEADER_LABELS = {"passingYards": "passing", "rushingYards": "rushing", "receivingYards": "receiving"}

_http: Callable[[str], dict[str, Any]] | None = None


class NFLError(RuntimeError):
    """The scoreboard could not be fetched or read."""


def set_http(fn: Callable[[str], dict[str, Any]] | None) -> None:
    """Tests: replace the HTTP GET (url -> parsed JSON). None = the real one."""
    global _http
    _http = fn


def _real_get(url: str) -> dict[str, Any]:
    import httpx

    with httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": "ClipAgent/1.0 (personal assistant)"}) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.json()


def fetch() -> dict[str, Any]:
    try:
        data = (_http or _real_get)(SCOREBOARD_URL)
    except Exception as exc:
        raise NFLError(f"scoreboard fetch failed: {type(exc).__name__}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise NFLError("scoreboard had no games")
    return data


# --- model ----------------------------------------------------------------------------------------

@dataclass
class Side:
    abbr: str
    name: str            # "Giants"
    score: int | None
    record: str = ""
    winner: bool = False


@dataclass
class Leader:
    category: str        # passing | rushing | receiving
    player: str
    line: str            # "23/36, 293 YDS, 1 TD, 1 INT"
    value: float
    team: str            # abbreviation


@dataclass
class Game:
    away: Side
    home: Side
    state: str           # pre | in | post
    detail: str          # "Final", "Final/OT", "Q3 4:12", "11/16 - 8:15 PM EST"
    start: datetime | None = None
    neutral: bool = False
    headline: str = ""
    leaders: list[Leader] = field(default_factory=list)

    def has(self, abbr: str) -> bool:
        return abbr in (self.away.abbr, self.home.abbr)

    @property
    def total(self) -> int:
        return (self.away.score or 0) + (self.home.score or 0)

    @property
    def margin(self) -> int:
        return abs((self.away.score or 0) - (self.home.score or 0))

    @property
    def overtime(self) -> bool:
        return "OT" in self.detail


@dataclass
class Slate:
    week: int | None
    games: list[Game]
    byes: list[str] = field(default_factory=list)   # abbreviations

    def finals(self) -> list[Game]:
        return [g for g in self.games if g.state == "post"]


def _int(v: Any) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _when(s: Any) -> datetime | None:
    if not isinstance(s, str):
        return None
    for fmt in ("%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _side(c: dict[str, Any], state: str) -> Side:
    team = c.get("team") or {}
    rec = next((r.get("summary", "") for r in c.get("records") or [] if r.get("type") == "total"), "")
    return Side(abbr=str(team.get("abbreviation") or "?"), name=str(team.get("shortDisplayName") or team.get("name") or ""),
                score=_int(c.get("score")) if state != "pre" else None, record=str(rec or ""),
                winner=bool(c.get("winner")))


def parse(data: dict[str, Any]) -> Slate:
    games: list[Game] = []
    for e in data.get("events") or []:
        comp = (e.get("competitions") or [{}])[0]
        st = ((comp.get("status") or e.get("status") or {}).get("type") or {})
        state = str(st.get("state") or "pre")
        sides = {c.get("homeAway"): c for c in comp.get("competitors") or []}
        if "home" not in sides or "away" not in sides:
            continue
        away, home = _side(sides["away"], state), _side(sides["home"], state)
        ids = {str((c.get("team") or {}).get("id")): (c.get("team") or {}).get("abbreviation", "")
               for c in sides.values()}
        leaders: list[Leader] = []
        for cat in comp.get("leaders") or []:
            label = LEADER_LABELS.get(cat.get("name"))
            top = (cat.get("leaders") or [None])[0]
            if not label or not top or state == "pre":
                continue
            ath = top.get("athlete") or {}
            team_id = str((top.get("team") or ath.get("team") or {}).get("id"))
            leaders.append(Leader(label, str(ath.get("displayName") or ath.get("shortName") or "?"),
                                  str(top.get("displayValue") or ""), float(top.get("value") or 0), ids.get(team_id, "")))
        heads = comp.get("headlines") or []
        games.append(Game(away=away, home=home, state=state,
                          detail=str(st.get("shortDetail") or st.get("detail") or ""),
                          start=_when(comp.get("date") or e.get("date")), neutral=bool(comp.get("neutralSite")),
                          headline=str((heads[0].get("shortLinkText") or heads[0].get("description") or "") if heads else ""),
                          leaders=leaders))
    games.sort(key=lambda g: g.start or datetime.max.replace(tzinfo=timezone.utc))
    week = _int((data.get("week") or {}).get("number"))
    byes = [str(t.get("abbreviation")) for t in (data.get("week") or {}).get("teamsOnBye") or [] if t.get("abbreviation")]
    return Slate(week=week, games=games, byes=byes)


# --- the tile -------------------------------------------------------------------------------------

def _kickoff(g: Game) -> str:
    if not g.start:
        return g.detail
    loc = g.start.astimezone(db.local_tz())
    return loc.strftime("%a %I:%M %p").replace(" 0", " ")


def _status(g: Game) -> str:
    if g.state == "post":
        return "F/OT" if g.overtime else "F"
    if g.state == "in":
        return g.detail or "live"
    return _kickoff(g)


def game_row(g: Game) -> str:
    sep = "vs" if g.neutral else "@"
    if g.state == "pre":
        return f"{g.away.abbr} {sep} {g.home.abbr}"
    return f"{g.away.abbr} {g.away.score} {sep} {g.home.abbr} {g.home.score}"


def team_result(slate: Slate, team: str) -> tuple[str, str]:
    """(big value, one plain line) for his team this week."""
    g = next((x for x in slate.games if x.has(team)), None)
    if g is None:
        return (f"{team} bye", f"{team} on a bye this week." if team in slate.byes else f"No {team} game on the board.")
    me, them = (g.home, g.away) if g.home.abbr == team else (g.away, g.home)
    nick = me.name or team
    if g.state == "pre":
        return (f"{team} {_kickoff(g)}", f"{nick} play {them.name or them.abbr} {_kickoff(g)}.")
    if g.state == "in":
        return (f"{team} {me.score}-{them.score}", f"{nick} {me.score}, {them.name or them.abbr} {them.score}, {g.detail}.")
    if me.score == them.score:
        verb, wl = "tied", "T"
    elif me.winner or (me.score or 0) > (them.score or 0):
        verb, wl = "beat", "W"
    else:
        verb, wl = "lost to", "L"
    rec = f", now {me.record}" if me.record else ""
    ot = " in overtime" if g.overtime else ""
    return (f"{team} {me.score}-{them.score} {wl}", f"{nick} {verb} {them.name or them.abbr} {me.score}-{them.score}{ot}{rec}.")


def insights(slate: Slate, team: str = DEFAULT_TEAM) -> list[str]:
    """A few short lines worth knowing: his team's top performer, then the week's standouts."""
    out: list[str] = []
    finals = slate.finals()
    mine = next((g for g in finals if g.has(team)), None)
    if mine:
        best = [ld for ld in mine.leaders if ld.team == team]
        if best:
            top = max(best, key=lambda ld: ld.value)
            out.append(f"{team} star: {top.player}, {top.line}")
    if finals:
        hi = max(finals, key=lambda g: g.total)
        out.append(f"Shootout: {game_row(hi)} ({hi.total} pts)")
        blow = max(finals, key=lambda g: g.margin)
        w = blow.away if (blow.away.score or 0) > (blow.home.score or 0) else blow.home
        if blow.margin >= 14:
            out.append(f"Rout: {w.abbr} by {blow.margin}, {game_row(blow)}")
        close = [g for g in finals if g.margin <= 3 or g.overtime]
        if close:
            out.append(f"Nail-biters: {len(close)} decided by 3 or fewer" if len(close) > 1
                       else f"Nail-biter: {game_row(close[0])}{' in OT' if close[0].overtime else ''}")
        ldrs = [ld for g in finals for ld in g.leaders if ld.category == "passing"]
        if ldrs:
            top = max(ldrs, key=lambda ld: ld.value)
            out.append(f"Top passer: {top.player} ({top.team}), {top.line}")
    return out


def build_tile(slate: Slate, team: str = DEFAULT_TEAM) -> dict[str, Any]:
    value, line = team_result(slate, team)
    stats = insights(slate, team)
    body = "\n".join([line, *stats][:4])
    ordered = sorted(slate.games, key=lambda g: (not g.has(team), g.state == "pre"))
    rows = [{"text": game_row(g), "note": _status(g), "mark": g.has(team)} for g in ordered]
    title = f"NFL week {slate.week} scores" if slate.week else "NFL scores"
    finals = len(slate.finals())
    if finals < len(slate.games):
        title += f" · {finals}/{len(slate.games)} final"
    return {"title": title, "value": value, "body": body, "rows": rows, "kind": "metric"}


def get_team() -> str:
    return str(db.kv_get(TEAM_KV, DEFAULT_TEAM) or DEFAULT_TEAM).upper()


def set_team(abbr: str) -> str:
    a = "".join(ch for ch in str(abbr or "").upper() if ch.isalpha())[:4]
    if not a:
        raise NFLError("team must be an abbreviation like NYG")
    db.kv_set(TEAM_KV, a)
    return a


def spoken(slate: Slate, team: str) -> str:
    """One or two sentences for voice."""
    _, line = team_result(slate, team)
    left = [g for g in slate.games if g.state != "post"]
    tail = f" {len(left)} game{'s' if len(left) != 1 else ''} still to finish." if left else ""
    return f"{line}{tail}"


def until_wednesday(now: datetime | None = None) -> datetime | None:
    """On Monday or Tuesday (local): the start of Wednesday, when the scoreboard comes down. Else None."""
    local = (now or db.local_now()).astimezone(db.local_tz())
    if local.weekday() not in (0, 1):
        return None
    day = local.date() + timedelta(days=2 - local.weekday())
    return datetime(day.year, day.month, day.day, tzinfo=db.local_tz())


def show(team: str | None = None, minutes: int | None = None, *, source: str = "nfl") -> dict[str, Any]:
    """Fetch the week, put the scoreboard tile up (replacing the last one) and return a summary."""
    from assistant import tiles

    t = (team or get_team()).upper()
    slate = parse(fetch())
    spec = build_tile(slate, t)
    end = None if minutes else until_wednesday()
    tile = tiles.show(spec["title"], spec["body"], value=spec["value"], kind=spec["kind"], rows=spec["rows"],
                      minutes=minutes or DEFAULT_MINUTES, until=end, key=TILE_KEY, source=source)
    db.log_event("assistant", f"nfl scores tile: week {slate.week}, {len(slate.games)} games",
                 data={"tile_id": tile.id, "team": t})
    return {"tile_id": tile.id, "week": slate.week, "team": t, "headline": spec["value"], "summary": spoken(slate, t),
            "stats": insights(slate, t), "games": [f"{game_row(g)} {_status(g)}" for g in slate.games],
            "team_headline": next((g.headline for g in slate.games if g.has(t)), "")}


def scheduled() -> str:
    """The Monday jobs: never raise into the scheduler for a flaky feed, just log."""
    try:
        return show(source="schedule")["headline"]
    except NFLError as exc:
        db.log_event("assistant", f"nfl scores tile skipped: {exc}", level="warning")
        return "skipped"


# --- tool -----------------------------------------------------------------------------------------

DESCRIPTION = ("NFL scores for the current week: puts a wide scoreboard tile on his Jarvis page (every game, his "
               "team first, a few standout stats) and returns the results. `team` = abbreviation (default his "
               "team, the Giants, NYG); `remember_team: true` makes it his default.")


def _tool(ctx: Any, a: dict[str, Any]) -> Any:
    from assistant.tools import ToolError

    try:
        team = a.get("team")
        if team and a.get("remember_team"):
            team = set_team(team)
        return show(team, a.get("minutes"), source=str(getattr(ctx, "agent", "assistant")))
    except NFLError as exc:
        raise ToolError(str(exc)) from None


def register_tools(register: Any) -> None:
    from assistant.tools import Tool, _obj

    register(Tool("nfl_scores", DESCRIPTION, _obj({
        "team": {"type": "string"}, "minutes": {"type": "integer"}, "remember_team": {"type": "boolean"},
    }), _tool, owner=__name__, side_effects=True), replace=True)


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="python -m assistant.nfl", description="NFL scores tile")
    p.add_argument("cmd", nargs="?", default="print", choices=("show", "print"))
    p.add_argument("--team")
    args = p.parse_args(argv)
    if args.cmd == "show":
        r = show(args.team)
        print(f"tile {r['tile_id']}: {r['headline']}")
        return 0
    slate = parse(fetch())
    spec = build_tile(slate, (args.team or get_team()).upper())
    print(spec["title"], "|", spec["value"])
    print(spec["body"])
    for r in spec["rows"]:
        print(("* " if r["mark"] else "  ") + f"{r['text']:<22} {r['note']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
