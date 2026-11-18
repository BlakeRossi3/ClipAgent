"""New York Islanders, live: every game of the season, and a tile on the Jarvis page that follows each one.

The owner: "scan every Islanders game this season and I want a dynamic tile to be put on the screen for
every Islanders game that shows the score live. So updated live with other stats like shots on goal,
stars of the game, key player stats."

* Data: the NHL's free public API (api-web.nhle.com, no key). :func:`fetch_schedule` reads the club's
  season schedule (regular season + playoffs); :func:`fetch_game` reads one game's boxscore + landing
  (score, clock, shots, player lines, three stars, scoring). Tests inject a fake with :func:`set_http`.
* The season's games are cached in kv ``nhl:schedule`` (refetched every :data:`SCHEDULE_TTL_H` hours,
  a failed fetch is retried after :data:`RETRY_MIN` minutes and the old cache kept).
* :func:`tick` (job ``assistant_nhl_live``, every minute) costs nothing between games. From
  :data:`PREVIEW_MIN` minutes before puck drop it puts a preview tile up (from the cache, no network);
  from :data:`POLL_LEAD_MIN` minutes before it polls the game every tick and keeps one wide tile
  (key ``nhl-islanders``) live with the score, period and clock, shots on goal, hits / blocks / PIM /
  power-play goals, the last goal, the Islanders' top players, both goalies and the three stars. At the
  final horn the final tile stays up :data:`FINAL_MINUTES` minutes and the game is done (kv ``nhl:done:<id>``).
  The tile is only rewritten when it changes. If he closes it, that game stays off his screen (kv
  ``nhl:muted:<id>``) until he asks for it (tool ``nhl_games``).
* Every game also shows on the Scheduled page (:func:`schedule_items`, used by assistant/schedule.py).
* Team: kv ``nhl:team`` (default ``NYI``). Tool ``nhl_games``; CLI ``python -m assistant.nhl [tick|show|schedule|refresh]``.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

import db

API = "https://api-web.nhle.com/v1"
TILE_KEY = "nhl-islanders"
TEAM_KV = "nhl:team"
SCHEDULE_KV = "nhl:schedule"
TRIED_KV = "nhl:schedule:tried"
DEFAULT_TEAM = "NYI"
TEAM_NAMES = {"NYI": "Islanders"}
GAME_TYPES = (2, 3)          # regular season, playoffs
SCHEDULE_TTL_H = 12
RETRY_MIN = 30
PREVIEW_MIN = 90             # preview tile this long before puck drop
POLL_LEAD_MIN = 10           # start polling the game feed this long before puck drop
GAME_WINDOW_H = 6            # give up on a game this long after its start
LIVE_MINUTES = 15            # a live tile's lifetime (renewed every tick, so it vanishes if polling stops)
FINAL_MINUTES = 240
REFRESH_LEFT_SEC = 5 * 60    # rewrite an unchanged tile when it has less than this left
LIVE_STATES = {"LIVE", "CRIT"}
FINAL_STATES = {"FINAL", "OFF"}

_http: Callable[[str], dict[str, Any]] | None = None


class NHLError(RuntimeError):
    """The NHL feed could not be fetched or read."""


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


def _get(url: str) -> dict[str, Any]:
    try:
        data = (_http or _real_get)(url)
    except Exception as exc:
        raise NHLError(f"NHL feed failed: {type(exc).__name__}: {exc}") from exc
    if not isinstance(data, dict):
        raise NHLError("NHL feed returned nothing readable")
    return data


def _d(v: Any) -> str:
    """The NHL's localised strings: {"default": "Islanders"} -> "Islanders"."""
    if isinstance(v, dict):
        return str(v.get("default") or "")
    return str(v or "")


def _int(v: Any) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _when(s: Any) -> datetime | None:
    if not isinstance(s, str):
        return None
    try:
        return db.parse_iso(s if s.endswith("Z") else s + "Z")
    except Exception:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None


def season_id(day: date | None = None) -> str:
    """"20262027" for any day from August 2026 to July 2027."""
    d = day or db.local_today()
    y = d.year if d.month >= 8 else d.year - 1
    return f"{y}{y + 1}"


def get_team() -> str:
    return str(db.kv_get(TEAM_KV, DEFAULT_TEAM) or DEFAULT_TEAM).upper()


def team_name(abbr: str) -> str:
    return TEAM_NAMES.get(abbr, abbr)


# --- the season schedule ----------------------------------------------------------------------------

@dataclass
class Game:
    id: int
    start: datetime
    away: str
    home: str
    away_name: str = ""
    home_name: str = ""
    state: str = "FUT"
    schedule_state: str = "OK"
    game_type: int = 2
    venue: str = ""
    tv: list[str] = field(default_factory=list)
    away_score: int | None = None
    home_score: int | None = None
    last_period: str = ""        # REG | OT | SO once final

    def has(self, abbr: str) -> bool:
        return abbr in (self.away, self.home)

    def opponent(self, abbr: str) -> tuple[str, str]:
        return (self.home, self.home_name) if self.away == abbr else (self.away, self.away_name)

    @property
    def final(self) -> bool:
        return self.state in FINAL_STATES

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["start"] = db.to_iso(self.start)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Game":
        kw = {k: d.get(k) for k in cls.__dataclass_fields__ if k in d}
        kw["start"] = db.parse_iso(d["start"])
        kw["tv"] = list(d.get("tv") or [])
        return cls(**kw)


def parse_schedule(data: dict[str, Any]) -> list[Game]:
    out: list[Game] = []
    for g in data.get("games") or []:
        if _int(g.get("gameType")) not in GAME_TYPES:
            continue
        start = _when(g.get("startTimeUTC"))
        gid = _int(g.get("id"))
        a, h = g.get("awayTeam") or {}, g.get("homeTeam") or {}
        if start is None or gid is None or not a.get("abbrev") or not h.get("abbrev"):
            continue
        tv = []
        for b in g.get("tvBroadcasts") or []:
            n = str(b.get("network") or "")
            if n and n not in tv and b.get("countryCode", "US") == "US":
                tv.append(n)
        out.append(Game(id=gid, start=start, away=str(a["abbrev"]), home=str(h["abbrev"]),
                        away_name=_d(a.get("commonName")), home_name=_d(h.get("commonName")),
                        state=str(g.get("gameState") or "FUT"), schedule_state=str(g.get("gameScheduleState") or "OK"),
                        game_type=_int(g.get("gameType")) or 2, venue=_d(g.get("venue")), tv=tv,
                        away_score=_int(a.get("score")), home_score=_int(h.get("score")),
                        last_period=str((g.get("gameOutcome") or {}).get("lastPeriodType") or "")))
    out.sort(key=lambda x: x.start)
    return out


def fetch_schedule(team: str | None = None, season: str | None = None) -> list[Game]:
    t = (team or get_team()).upper()
    s = season or season_id()
    return parse_schedule(_get(f"{API}/club-schedule-season/{t}/{s}"))


def refresh_schedule(*, force: bool = False) -> list[Game]:
    """The cached season (refetched when stale). A failed fetch keeps the old cache; raises only with none."""
    team, season = get_team(), season_id()
    cache = db.kv_get(SCHEDULE_KV)
    now = db.utcnow()
    fresh = (isinstance(cache, dict) and cache.get("team") == team and cache.get("season") == season
             and now - db.parse_iso(cache["fetched_at"]) < timedelta(hours=SCHEDULE_TTL_H))
    if fresh and not force:
        return [Game.from_dict(g) for g in cache["games"]]
    tried = db.kv_get(TRIED_KV)
    usable = isinstance(cache, dict) and cache.get("team") == team and cache.get("season") == season
    if not force and usable and tried and now - db.parse_iso(tried) < timedelta(minutes=RETRY_MIN):
        return [Game.from_dict(g) for g in cache["games"]]
    db.kv_set(TRIED_KV, db.to_iso(now))
    try:
        games = fetch_schedule(team, season)
    except NHLError as exc:
        db.log_event("assistant", f"islanders schedule not refreshed: {exc}", level="warning")
        if usable:
            return [Game.from_dict(g) for g in cache["games"]]
        raise
    db.kv_set(SCHEDULE_KV, {"team": team, "season": season, "fetched_at": db.to_iso(now),
                            "games": [g.to_dict() for g in games]})
    if not usable:
        db.log_event("assistant", f"islanders schedule: {len(games)} games in {season}", data={"team": team})
    return games


def record(games: list[Game], team: str) -> str:
    """Regular-season record W-L-OTL from the finished games."""
    w = l_ = otl = 0
    for g in games:
        if not g.final or g.game_type != 2 or g.away_score is None or g.home_score is None:
            continue
        mine, theirs = (g.away_score, g.home_score) if g.away == team else (g.home_score, g.away_score)
        if mine > theirs:
            w += 1
        elif g.last_period in ("OT", "SO"):
            otl += 1
        else:
            l_ += 1
    return f"{w}-{l_}-{otl}"


# --- one game, live -----------------------------------------------------------------------------

@dataclass
class Side:
    abbr: str
    name: str
    score: int = 0
    sog: int = 0
    hits: int = 0
    blocks: int = 0
    pim: int = 0
    ppg: int = 0


@dataclass
class Player:
    team: str
    name: str
    pos: str
    number: int | None
    goals: int = 0
    assists: int = 0
    points: int = 0
    sog: int = 0
    toi: str = ""
    plus_minus: int = 0
    hits: int = 0


@dataclass
class Goalie:
    team: str
    name: str
    saves: int = 0
    shots: int = 0
    toi: str = ""
    decision: str = ""

    @property
    def pct(self) -> str:
        return f"{self.saves / self.shots:.3f}".lstrip("0") if self.shots else "-"


@dataclass
class Star:
    n: int
    name: str
    team: str
    pos: str
    goals: int = 0
    assists: int = 0
    line: str = ""       # goalies: "SV% .966"


@dataclass
class Live:
    id: int
    state: str               # pre | live | final
    raw_state: str
    period: int
    period_type: str         # REG | OT | SO
    clock: str
    intermission: bool
    away: Side
    home: Side
    start: datetime | None = None
    venue: str = ""
    players: list[Player] = field(default_factory=list)
    goalies: list[Goalie] = field(default_factory=list)
    stars: list[Star] = field(default_factory=list)
    last_goal: str = ""

    def side(self, abbr: str) -> tuple[Side, Side]:
        return (self.away, self.home) if self.away.abbr == abbr else (self.home, self.away)


def _ordinal(n: int) -> str:
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(n, f"{n}th")


def period_label(period: int, ptype: str, *, regulation: int = 3) -> str:
    if ptype == "SO":
        return "SO"
    if ptype == "OT" or period > regulation:
        k = period - regulation
        return "OT" if k <= 1 else f"{k}OT"
    return _ordinal(period)


def _status(lv: Live) -> str:
    if lv.state == "final":
        return "Final" + ("/SO" if lv.period_type == "SO" else "/OT" if lv.period_type == "OT" else "")
    if lv.state == "pre":
        return "Pregame"
    p = period_label(lv.period, lv.period_type)
    if lv.intermission:
        return f"{p} intermission"
    if lv.clock in ("00:00", "0:00"):
        return f"End of {p}"
    clock = lv.clock[1:] if len(lv.clock) == 5 and lv.clock.startswith("0") else lv.clock
    return f"{p} {clock}".strip()


def fetch_game(game_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    box = _get(f"{API}/gamecenter/{int(game_id)}/boxscore")
    try:
        land = _get(f"{API}/gamecenter/{int(game_id)}/landing")
    except NHLError:
        land = {}   # stars and scoring are extras; the score still updates
    return box, land


def parse_game(box: dict[str, Any], land: dict[str, Any] | None = None) -> Live:
    land = land or {}
    raw = str(box.get("gameState") or land.get("gameState") or "FUT")
    state = "live" if raw in LIVE_STATES else "final" if raw in FINAL_STATES else "pre"
    pd = box.get("periodDescriptor") or land.get("periodDescriptor") or {}
    clock = box.get("clock") or land.get("clock") or {}
    sides = {}
    for k in ("awayTeam", "homeTeam"):
        t = box.get(k) or {}
        sides[k] = Side(abbr=str(t.get("abbrev") or "?"), name=_d(t.get("commonName")),
                        score=_int(t.get("score")) or 0, sog=_int(t.get("sog")) or 0)
    players: list[Player] = []
    goalies: list[Goalie] = []
    pbg = box.get("playerByGameStats") or {}
    for k, side in sides.items():
        grp = pbg.get(k) or {}
        for p in (grp.get("forwards") or []) + (grp.get("defense") or []):
            pl = Player(team=side.abbr, name=_d(p.get("name")), pos=str(p.get("position") or ""),
                        number=_int(p.get("sweaterNumber")), goals=_int(p.get("goals")) or 0,
                        assists=_int(p.get("assists")) or 0, points=_int(p.get("points")) or 0,
                        sog=_int(p.get("sog")) or 0, toi=str(p.get("toi") or ""),
                        plus_minus=_int(p.get("plusMinus")) or 0, hits=_int(p.get("hits")) or 0)
            players.append(pl)
            side.hits += pl.hits
            side.blocks += _int(p.get("blockedShots")) or 0
            side.pim += _int(p.get("pim")) or 0
            side.ppg += _int(p.get("powerPlayGoals")) or 0
        for gk in grp.get("goalies") or []:
            shots = _int(gk.get("shotsAgainst"))
            saves = _int(gk.get("saves"))
            if shots is None and "/" in str(gk.get("saveShotsAgainst") or ""):
                saves, shots = (_int(x) for x in str(gk["saveShotsAgainst"]).split("/", 1))
            toi = str(gk.get("toi") or "")
            if not shots and toi in ("", "00:00"):
                continue   # dressed, did not play
            goalies.append(Goalie(team=side.abbr, name=_d(gk.get("name")), saves=saves or 0, shots=shots or 0,
                                  toi=toi, decision=str(gk.get("decision") or "")))
            side.pim += _int(gk.get("pim")) or 0
    summary = land.get("summary") or {}
    stars = []
    for s in summary.get("threeStars") or []:
        n = _int(s.get("star"))
        if not n:
            continue
        line = ""
        if s.get("savePctg") is not None:
            try:
                line = f"SV% {float(s['savePctg']):.3f}".replace("0.", ".")
            except (TypeError, ValueError):
                line = ""
        team = _d(s.get("teamAbbrev"))
        stars.append(Star(n=n, name=_d(s.get("name")) or _d(s.get("lastName")), team=team,
                          pos=str(s.get("position") or ""), goals=_int(s.get("goals")) or 0,
                          assists=_int(s.get("assists")) or 0, line=line))
    stars.sort(key=lambda s: s.n)
    last_goal = ""
    for per in summary.get("scoring") or []:
        pdesc = per.get("periodDescriptor") or {}
        for gl in per.get("goals") or []:
            who = _d(gl.get("name")) or _d(gl.get("lastName"))
            team = _d(gl.get("teamAbbrev"))
            plabel = period_label(_int(pdesc.get("number")) or 0, str(pdesc.get("periodType") or "REG"))
            strength = str(gl.get("strength") or "").lower()
            tag = {"pp": ", power play", "sh": ", shorthanded"}.get(strength, "")
            if gl.get("goalModifier") == "empty-net":
                tag += ", empty net"
            n = _int(gl.get("goalsToDate"))
            last_goal = (f"{who}{f' ({n})' if n else ''}, {team}, {plabel} {str(gl.get('timeInPeriod') or '').lstrip('0') or '0:00'}"
                         f"{tag}")
    return Live(id=_int(box.get("id")) or 0, state=state, raw_state=raw, period=_int(pd.get("number")) or 0,
                period_type=str(pd.get("periodType") or "REG"), clock=str(clock.get("timeRemaining") or ""),
                intermission=bool(clock.get("inIntermission")), away=sides["awayTeam"], home=sides["homeTeam"],
                start=_when(box.get("startTimeUTC")), venue=_d(box.get("venue")),
                players=players, goalies=goalies, stars=stars, last_goal=last_goal)


# --- the tiles ----------------------------------------------------------------------------------

def _local_time(dt: datetime) -> str:
    return dt.astimezone(db.local_tz()).strftime("%I:%M %p").lstrip("0")


def _day_words(dt: datetime, now: datetime) -> str:
    d = dt.astimezone(db.local_tz()).date()
    today = now.astimezone(db.local_tz()).date()
    if d == today:
        return "tonight" if dt.astimezone(db.local_tz()).hour >= 17 else "today"
    if d == today + timedelta(days=1):
        return "tomorrow"
    return dt.astimezone(db.local_tz()).strftime("%a %b %d").replace(" 0", " ")


def _scoreline(lv: Live) -> str:
    return f"{lv.away.abbr} {lv.away.score} @ {lv.home.abbr} {lv.home.score}"


def _pline(p: Player) -> str:
    bits = [f"{p.goals}G {p.assists}A", f"{p.sog} SOG"]
    if p.plus_minus:
        bits.append(f"{p.plus_minus:+d}")
    if p.toi:
        bits.append(f"{p.toi.lstrip('0')} TOI")
    return " · ".join(bits)


def key_players(lv: Live, team: str, n: int = 4) -> list[Player]:
    mine = [p for p in lv.players if p.team == team]
    mine.sort(key=lambda p: (p.points, p.goals, p.sog, p.toi), reverse=True)
    return mine[:n]


def preview_tile(g: Game, team: str, now: datetime, rec: str = "") -> dict[str, Any]:
    opp, opp_name = g.opponent(team)
    where = "vs" if g.home == team else "at"
    lines = [f"Puck drop {_local_time(g.start)}{f' at {g.venue}' if g.venue else ''}."]
    if g.tv:
        lines.append("TV: " + ", ".join(g.tv[:4]) + ".")
    if rec:
        lines.append(f"{team_name(team)} {rec}.")
    return {"title": f"{team_name(team)} {_day_words(g.start, now)}", "value": f"{g.away} @ {g.home}",
            "body": "\n".join(lines), "rows": [], "kind": "info",
            "summary": f"{team_name(team)} {where} {opp_name or opp} {_day_words(g.start, now)} at {_local_time(g.start)}."}


def game_tile(lv: Live, team: str) -> dict[str, Any]:
    """The live (or final) tile: score as the big value, a few lines, then a scoreboard of stats and players."""
    me, them = lv.side(team)
    status = _status(lv)
    title = f"{team_name(team)} {'final' if lv.state == 'final' else 'live'} · {status}"
    a, h = lv.away, lv.home
    body = [f"Shots on goal: {a.abbr} {a.sog}, {h.abbr} {h.sog}"]
    if lv.last_goal:
        body.append(f"Last goal: {lv.last_goal}")
    if lv.stars:
        body.append("Stars: " + ", ".join(f"{s.n}. {s.name}" for s in lv.stars[:3]))
    rows: list[dict[str, Any]] = [
        {"text": "Shots on goal", "note": f"{a.abbr} {a.sog} · {h.abbr} {h.sog}", "mark": False},
        {"text": "Hits · blocks", "note": f"{a.abbr} {a.hits}/{a.blocks} · {h.abbr} {h.hits}/{h.blocks}", "mark": False},
        {"text": "PP goals · PIM", "note": f"{a.abbr} {a.ppg}/{a.pim} · {h.abbr} {h.ppg}/{h.pim}", "mark": False},
    ]
    for s in lv.stars[:3]:
        line = s.line or f"{s.goals}G {s.assists}A"
        rows.append({"text": f"★{s.n} {s.name} ({s.team})", "note": line, "mark": s.team == team})
    for p in key_players(lv, team):
        num = f" #{p.number}" if p.number is not None else ""
        rows.append({"text": f"{p.name}{num} {p.pos}", "note": _pline(p), "mark": True})
    opp_best = key_players(lv, them.abbr, 1)
    if opp_best and opp_best[0].points:
        p = opp_best[0]
        rows.append({"text": f"{p.name} {p.pos} ({p.team})", "note": _pline(p), "mark": False})
    for gk in sorted(lv.goalies, key=lambda x: x.team != team):
        dec = f" · {gk.decision}" if gk.decision else ""
        rows.append({"text": f"{gk.name} G ({gk.team})", "note": f"{gk.saves}/{gk.shots} saves · {gk.pct}{dec}",
                     "mark": gk.team == team})
    lead = "lead" if me.score > them.score else "trail" if me.score < them.score else "are tied with"
    if lv.state == "final":
        verb = "beat" if me.score > them.score else "lost to"
        extra = {"OT": " in overtime", "SO": " in a shootout"}.get(lv.period_type, "")
        summary = f"{team_name(team)} {verb} {them.name or them.abbr} {me.score} to {them.score}{extra}."
        if lv.stars:
            summary += f" First star: {lv.stars[0].name}."
    else:
        summary = (f"{team_name(team)} {lead} {them.name or them.abbr} {me.score} to {them.score}, {status}. "
                   f"Shots {me.sog} to {them.sog}.")
    return {"title": title, "value": _scoreline(lv), "body": "\n".join(body[:4]), "rows": rows,
            "kind": "metric", "summary": summary}


# --- putting it on the page -------------------------------------------------------------------------

def _post(spec: dict[str, Any], game_id: int, minutes: int, *, force: bool = False) -> int | None:
    """Show/refresh the tile; skipped when nothing changed and it has time left. Returns the live tile id."""
    from assistant import store, tiles

    sig = hashlib.sha1(json.dumps({k: spec[k] for k in ("title", "value", "body", "rows")},
                                  sort_keys=True).encode()).hexdigest()
    last = db.kv_get(f"nhl:tile:{game_id}")
    if isinstance(last, dict) and not force:
        t = store.get_tile(int(last.get("id") or 0))
        if t is not None and t.removed_by == "owner":
            db.kv_set(f"nhl:muted:{game_id}", True)
            return None
        if (t is not None and t.removed_at is None and last.get("sig") == sig
                and (db.parse_iso(t.expires_at) - db.utcnow()).total_seconds() > REFRESH_LEFT_SEC):
            return t.id
    tile = tiles.show(spec["title"], spec["body"], value=spec["value"], kind=spec["kind"], rows=spec["rows"] or None,
                      minutes=minutes, key=TILE_KEY, source="nhl")
    db.kv_set(f"nhl:tile:{game_id}", {"id": tile.id, "sig": sig})
    return tile.id


def _active(games: list[Game], now: datetime) -> Game | None:
    for g in games:
        if g.schedule_state not in ("OK", "") or db.kv_get(f"nhl:done:{g.id}"):
            continue
        if g.start - timedelta(minutes=PREVIEW_MIN) <= now <= g.start + timedelta(hours=GAME_WINDOW_H):
            return g
    return None


def _track(g: Game, team: str, now: datetime, games: list[Game], *, force: bool = False) -> dict[str, Any]:
    """One step for one game: preview, live or final tile. Returns what happened."""
    if now < g.start - timedelta(minutes=POLL_LEAD_MIN):
        spec = preview_tile(g, team, now, record(games, team))
        mins = int((g.start - now).total_seconds() // 60) + 30
        tid = _post(spec, g.id, mins, force=force)
        return {"status": "preview", "game_id": g.id, "tile_id": tid, "summary": spec["summary"]}
    lv = parse_game(*fetch_game(g.id))
    if lv.state == "pre":
        if now > g.start + timedelta(hours=GAME_WINDOW_H - 1):
            db.kv_set(f"nhl:done:{g.id}", "never started")
            return {"status": "gave_up", "game_id": g.id}
        spec = preview_tile(g, team, now, record(games, team))
        tid = _post(spec, g.id, LIVE_MINUTES + 30, force=force)
        return {"status": "pregame", "game_id": g.id, "tile_id": tid, "summary": spec["summary"]}
    spec = game_tile(lv, team)
    if lv.state == "final":
        tid = _post(spec, g.id, FINAL_MINUTES, force=True)
        db.kv_set(f"nhl:done:{g.id}", db.now_iso())
        db.kv_set("nhl:last", {"game_id": g.id, "summary": spec["summary"], "at": db.now_iso()})
        db.log_event("assistant", f"islanders final: {spec['value']}", data={"game_id": g.id, "tile_id": tid})
        return {"status": "final", "game_id": g.id, "tile_id": tid, "summary": spec["summary"], "score": spec["value"]}
    tid = _post(spec, g.id, LIVE_MINUTES, force=force)
    db.kv_set("nhl:last", {"game_id": g.id, "summary": spec["summary"], "at": db.now_iso()})
    return {"status": "live", "game_id": g.id, "tile_id": tid, "summary": spec["summary"], "score": spec["value"]}


def tick(now: datetime | None = None) -> str:
    """The every-minute job. Never raises into the scheduler."""
    now = now or db.utcnow()
    try:
        games = refresh_schedule()
        g = _active(games, now)
        if g is None:
            return "idle"
        if db.kv_get(f"nhl:muted:{g.id}"):
            return "muted"
        return _track(g, get_team(), now, games)["status"]
    except NHLError as exc:
        db.log_event("assistant", f"islanders tile not updated: {exc}", level="warning")
        return "skipped"


def upcoming(games: list[Game], now: datetime, limit: int = 5) -> list[Game]:
    return [g for g in games if g.start + timedelta(hours=GAME_WINDOW_H) > now and not g.final][:limit]


def show_now(game_id: int | None = None) -> dict[str, Any]:
    """He asked: the game in progress (or about to start), else the last result, else the next game - on screen now."""
    now = db.utcnow()
    team = get_team()
    games = refresh_schedule()
    g = next((x for x in games if x.id == game_id), None) if game_id else None
    g = g or _active(games, now) or next(
        (x for x in games if x.start <= now and x.start + timedelta(hours=GAME_WINDOW_H) > now), None)
    if g is None:
        past = [x for x in games if x.start <= now]
        g = past[-1] if past and now - past[-1].start < timedelta(hours=36) else None
    if g is None:
        nxt = upcoming(games, now, 1)
        if not nxt:
            return {"status": "no_games", "summary": "No more Islanders games on the schedule."}
        g = nxt[0]
        spec = preview_tile(g, team, now, record(games, team))
        tid = _post(spec, g.id, max(60, int((g.start - now).total_seconds() // 60) + 30)
                    if (g.start - now) < timedelta(hours=23) else 60, force=True)
        return {"status": "next", "game_id": g.id, "tile_id": tid, "summary": spec["summary"]}
    db.kv_delete(f"nhl:muted:{g.id}")
    if g.final or db.kv_get(f"nhl:done:{g.id}") or now >= g.start - timedelta(minutes=POLL_LEAD_MIN):
        lv = parse_game(*fetch_game(g.id))
        if lv.state != "pre":
            spec = game_tile(lv, team)
            tid = _post(spec, g.id, FINAL_MINUTES if lv.state == "final" else LIVE_MINUTES, force=True)
            return {"status": lv.state, "game_id": g.id, "tile_id": tid, "summary": spec["summary"], "score": spec["value"]}
    return _track(g, team, now, games, force=True)


# --- the Scheduled page ---------------------------------------------------------------------------

def schedule_items(now: datetime, horizon: datetime) -> list[dict[str, Any]]:
    """Islanders games between now and ``horizon`` for the Scheduled page (cache only, no network)."""
    cache = db.kv_get(SCHEDULE_KV)
    if not isinstance(cache, dict):
        return []
    team = str(cache.get("team") or DEFAULT_TEAM)
    from assistant.schedule import when_words

    out = []
    for d in cache.get("games") or []:
        g = Game.from_dict(d)
        if g.start + timedelta(hours=3) < now or g.start > horizon or g.final:
            continue
        opp, opp_name = g.opponent(team)
        where = "vs" if g.home == team else "at"
        out.append({"id": f"nhl-{g.id}", "source": "game", "kind": "game", "kind_label": "Islanders game",
                    "at": db.to_iso(g.start), "when": when_words(g.start, now=now) if g.start > now else "on now",
                    "title": f"{team_name(team)} {where} {opp_name or opp}",
                    "detail": (f"{g.venue}. " if g.venue else "") + (f"TV: {', '.join(g.tv[:4])}. " if g.tv else "")
                    + "Live score tile on the Jarvis page from puck drop.",
                    "by": "NHL schedule", "why": "live Islanders tile", "recur": "", "actions": False})
    return out


# --- tool -----------------------------------------------------------------------------------------

DESCRIPTION = ("New York Islanders games (NHL public API). A live tile goes up on his Jarvis page by itself for every "
               "game (score, period, shots, three stars, key players). action `show` (default): put the current game "
               "(or last result / next game) on screen now and return the summary - also un-hides a tile he closed. "
               "`schedule`: the season's games (record, next `limit` games). `refresh`: refetch the season schedule.")


def _schedule_view(limit: int) -> dict[str, Any]:
    now = db.utcnow()
    team = get_team()
    games = refresh_schedule()
    nxt = upcoming(games, now, limit)
    return {"season": season_id(), "team": team, "games": len(games), "record": record(games, team),
            "played": sum(1 for g in games if g.final),
            "next": [{"game_id": g.id, "when": g.start.astimezone(db.local_tz()).strftime("%a %b %d %I:%M %p"),
                      "matchup": f"{g.away} @ {g.home}", "venue": g.venue, "tv": g.tv} for g in nxt]}


def _tool(ctx: Any, a: dict[str, Any]) -> Any:
    from assistant.tools import ToolError

    action = str(a.get("action") or "show")
    try:
        if action == "schedule":
            return _schedule_view(max(1, min(int(a.get("limit") or 5), 84)))
        if action == "refresh":
            games = refresh_schedule(force=True)
            return {"games": len(games), "season": season_id()}
        return show_now(_int(a.get("game_id")))
    except NHLError as exc:
        raise ToolError(str(exc)) from None


def register_tools(register: Any) -> None:
    from assistant.tools import Tool, _obj

    register(Tool("nhl_games", DESCRIPTION, _obj({
        "action": {"type": "string", "enum": ["show", "schedule", "refresh"]},
        "limit": {"type": "integer"}, "game_id": {"type": "integer"},
    }), _tool, owner=__name__, side_effects=True), replace=True)


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="python -m assistant.nhl", description="Islanders games and the live tile")
    p.add_argument("cmd", nargs="?", default="schedule", choices=("tick", "show", "schedule", "refresh"))
    args = p.parse_args(argv)
    if args.cmd == "tick":
        print(tick())
    elif args.cmd == "show":
        print(show_now().get("summary"))
    elif args.cmd == "refresh":
        print(f"{len(refresh_schedule(force=True))} games")
    else:
        v = _schedule_view(10)
        print(f"{v['team']} {v['season']}: {v['games']} games, {v['played']} played, record {v['record']}")
        for g in v["next"]:
            print(f"  {g['when']}  {g['matchup']}  {', '.join(g['tv'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
