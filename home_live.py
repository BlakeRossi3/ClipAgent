"""The Cecil home's live panels: recent chats, staff at work, recently worked on, "new stuff", charts.

Plain code over the database, the in-memory event bus and ``git log`` - no model call. Rendered into the
home page (``templates/home.html`` via ``templates/cecil_parts.html``) and refreshed by
``static/cecil.js`` from two read-only routes (registered on ``dashboard.router`` by :func:`register`):

* ``GET /dashboard/api/cecil``  JSON: server-rendered (escaped) panel fragments + a few numbers for the
                                 status strip and the ring's orbiting dots. Same auth as the dashboard.
* ``GET /dashboard/staff``      the whole staff (characters from *Invincible*) with live state.

Everything user- or model-written (messages, task text, commit titles) reaches the page only through
Jinja autoescaping (the fragments) or ``textContent`` (the JS) - never as markup.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from datetime import date, timedelta
from typing import Any

from fastapi import Request
from fastapi.responses import Response

import db

log = logging.getLogger(__name__)

CHAT_EXCHANGES = 5
WORK_ITEMS = 7
NEW_STUFF = 15
NEW_STUFF_TTL = 300.0           # seconds the git log is cached
REFRESH_SECONDS = 15


def _home():
    import home

    return home


def _d():
    import dashboard

    return dashboard


def _safe(fn: Callable[[], Any], default: Any, what: str) -> Any:
    try:
        return fn()
    except Exception as exc:
        log.warning("cecil home: %s unavailable: %s: %s", what, type(exc).__name__, exc)
        return default


# ---------------------------------------------------------------------------
# recent chats: the last few exchanges on every channel
# ---------------------------------------------------------------------------

def recent_chats(limit: int = CHAT_EXCHANGES) -> list[dict[str, Any]]:
    """Newest first: [{ts, channel, you, them, who}] - each of his messages with the reply that followed it
    on the same channel; a message from the assistant with nothing before it stands alone."""
    from assistant import store

    s = _home()._asettings()
    msgs = list(reversed(store.list_messages(limit * 6)))       # oldest first
    out: list[dict[str, Any]] = []
    open_by_channel: dict[str, dict[str, Any]] = {}
    for seq, m in enumerate(msgs):
        if m.direction == "in":
            ex = {"ts": m.ts, "seq": seq, "channel": m.channel, "you": m.body, "them": None, "who": s.name}
            out.append(ex)
            open_by_channel[m.channel] = ex
            continue
        ex = open_by_channel.get(m.channel)
        if ex is not None and ex["them"] is None:
            ex["them"], ex["ts"], ex["seq"] = m.body, m.ts, seq
            ex["who"] = _home().agent_display(m.agent, settings=s)
        else:
            out.append({"ts": m.ts, "seq": seq, "channel": m.channel, "you": None, "them": m.body,
                        "who": _home().agent_display(m.agent, settings=s)})
    out.sort(key=lambda e: (e["ts"] or "", e["seq"]), reverse=True)
    return out[:limit]


# ---------------------------------------------------------------------------
# staff at work
# ---------------------------------------------------------------------------

def _bus_snapshot() -> dict[str, Any]:
    from assistant import events

    return events.snapshot()


def _running_tasks() -> list[dict[str, Any]]:
    from assistant import tasks

    return tasks.running_tasks()


def staff_at_work(today: date | None = None) -> list[dict[str, Any]]:
    """Every agent with its live state: working (a run in flight, or for the main agent a turn or a
    background task), error (failed in the last 24 h), else idle, plus what it is on and its day."""
    from assistant import store

    h = _home()
    today = today or db.local_today()
    start, _ = db.local_day_bounds(today)
    now = db.utcnow()
    snap = _safe(_bus_snapshot, {"current": None, "turns": [], "tasks": []}, "event bus")
    turn = next((t for t in snap.get("turns", []) if t.get("id") == snap.get("current")), None)
    bg = _safe(_running_tasks, [], "background tasks")
    out = []
    for m in _safe(h.staff_nav, [], "staff"):
        runs = store.list_agent_runs(6, agent=m["name"])
        today_runs = store.list_agent_runs(500, agent=m["name"], since=start)
        last = runs[0] if runs else None
        state, doing = "idle", ""
        if last is not None:
            age = now - db.parse_iso(last.ts)
            if last.status == "running" and age < timedelta(minutes=h.WORKING_MINUTES):
                state, doing = "working", last.task
            elif last.status == "error" and age < timedelta(hours=24):
                state, doing = "error", (last.summary or last.task)
        if m["name"] == "main":
            if turn is not None and turn.get("status") == "running":
                steps = [x for x in turn.get("steps", []) if x.get("status") == "running"]
                state = "working"
                doing = (steps[-1].get("label") if steps else "") or ("Answering: " + (turn.get("heard") or "")).strip(": ")
            elif bg:
                state, doing = "working", f"{len(bg)} background task{'s' if len(bg) != 1 else ''}: {bg[-1].get('task', '')}"
        done = next((r for r in runs if r.status != "running"), None)
        out.append({**{k: m[k] for k in ("name", "display", "role", "character")},
                    "sprite": h.sprite_svg(m["name"], size=34),
                    "state": state, "doing": (doing or "")[:160],
                    "last": None if done is None else {"ts": done.ts, "text": (done.summary or done.task or "")[:140],
                                                       "status": done.status},
                    "runs_today": len(today_runs), "cost_today": round(sum(r.cost_usd for r in today_runs), 4)})
    return out


# ---------------------------------------------------------------------------
# recently worked on: finished runs, finished background tasks, the assistant's own code changes
# ---------------------------------------------------------------------------

_SELF_PREFIX = re.compile(r"^(?:Sterling|Cecil)\s*:\s*", re.I)


def _self_commits() -> list[dict[str, Any]]:
    from assistant import selfedit

    return selfedit.self_commits(5)


def _all_tasks() -> list[dict[str, Any]]:
    from assistant import tasks

    return tasks.list_tasks()


def recent_work(limit: int = WORK_ITEMS) -> list[dict[str, Any]]:
    """Newest first: [{ts, who, text, kind (run|task|code), status}]."""
    from assistant import store

    h = _home()
    s = h._asettings()
    items: list[dict[str, Any]] = []
    for r in store.list_agent_runs(limit * 3):
        if r.status == "running" or (r.parent_id is not None and r.agent == "main"):
            continue
        items.append({"ts": r.ts, "who": h.agent_display(r.agent, settings=s), "agent": r.agent,
                      "text": (r.summary or r.task or "")[:180], "kind": "run",
                      "status": "ok" if r.status == "ok" else r.status})
    for t in _safe(_all_tasks, [], "tasks"):
        if t.get("status") in (None, "running"):
            continue
        items.append({"ts": t.get("finished") or t.get("started") or "", "who": s.name, "agent": "main",
                      "text": str(t.get("summary") or t.get("task") or "")[:180], "kind": "task",
                      "status": "ok" if t.get("status") == "done" else str(t.get("status"))})
    for c in _safe(_self_commits, [], "self-edits"):
        items.append({"ts": _iso(c.get("at")), "who": s.name, "agent": "main",
                      "text": "Changed its own code: " + friendly_title(str(c.get("title") or "")),
                      "kind": "code", "status": "undone" if c.get("reverted") else "ok"})
    items.sort(key=lambda i: i["ts"] or "", reverse=True)
    return items[:limit]


def _iso(value: Any) -> str:
    try:
        return db.to_iso(db.parse_iso(str(value))) if value else ""
    except Exception:
        try:
            from datetime import datetime

            return db.to_iso(datetime.fromisoformat(str(value)))
        except Exception:
            return ""


# ---------------------------------------------------------------------------
# new stuff: what was just built (the app checkout's recent commits, with friendly titles)
# ---------------------------------------------------------------------------

GitLog = Callable[[int], list[tuple[str, str, str]]]      # n -> [(short sha, committed ISO, subject)]
_git_log_override: GitLog | None = None
_new_cache: dict[str, Any] = {"at": 0.0, "rows": None}
_new_lock = threading.Lock()

_CONVENTIONAL = re.compile(r"^(?:feat|fix|chore|docs|refactor|perf|test|tests|style|build|ci)(?:\([^)]*\))?!?:\s*", re.I)
_TRAILING = re.compile(r"[\s.;:]+$")


def set_git_log(fn: GitLog | None) -> None:
    """Tests: replace ``git log`` (and drop the cache)."""
    global _git_log_override
    _git_log_override = fn
    _new_cache.update(at=0.0, rows=None)


def _real_git_log(n: int) -> list[tuple[str, str, str]]:
    from assistant import selfedit

    root = selfedit.app_dir()
    if not (root / ".git").exists():
        return []
    kw: dict[str, Any] = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
    r = subprocess.run(["git", "log", "-n", str(n), "--no-merges", "--format=%h%x1f%cI%x1f%s"], cwd=str(root),
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, check=False, **kw)
    if r.returncode != 0:
        return []
    rows = []
    for line in r.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 3:
            rows.append((parts[0], parts[1], parts[2]))
    return rows


def friendly_title(subject: str) -> str:
    """A commit subject as a plain headline: no "Cecil:"/"Sterling:" or "feat(x):" prefix, no trailing
    full stop, a capital first letter, at most 110 characters."""
    t = _SELF_PREFIX.sub("", subject.strip())
    t = _CONVENTIONAL.sub("", t)
    t = _TRAILING.sub("", t)
    t = re.sub(r"\s+", " ", t)
    if not t:
        return "Small fix"
    t = t[0].upper() + t[1:]
    return t if len(t) <= 110 else t[:107].rstrip() + "…"


def new_stuff(limit: int = NEW_STUFF) -> list[dict[str, Any]]:
    """Newest first: [{sha, ts, title}] from the app's git history, cached for :data:`NEW_STUFF_TTL`."""
    with _new_lock:
        if _new_cache["rows"] is not None and time.monotonic() - _new_cache["at"] < NEW_STUFF_TTL:
            return _new_cache["rows"][:limit]
        fn = _git_log_override or _real_git_log
        try:
            raw = fn(max(limit, NEW_STUFF))
        except Exception as exc:
            log.info("new stuff: git log unavailable: %s", exc)
            raw = []
        rows = [{"sha": str(sha)[:12], "ts": _iso(at), "title": friendly_title(str(subj))} for sha, at, subj in raw]
        _new_cache.update(at=time.monotonic(), rows=rows)
        return rows[:limit]


# ---------------------------------------------------------------------------
# charts (tiny inline SVG, geometry computed here; the template only places it)
# ---------------------------------------------------------------------------

def spark(values: Sequence[float | None], w: float = 120, h: float = 34) -> dict[str, Any] | None:
    """A sparkline in a ``w`` x ``h`` box: {line, area, last: (x, y)}; None with fewer than 2 points."""
    pts = [(i, float(v)) for i, v in enumerate(values) if v is not None]
    if len(pts) < 2:
        return None
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    span = (hi - lo) or 1.0
    n = max(1, len(values) - 1)
    xy = [(round(i / n * w, 2), round(h - 3 - (v - lo) / span * (h - 6), 2)) for i, v in pts]
    line = " ".join(f"{'M' if k == 0 else 'L'}{x},{y}" for k, (x, y) in enumerate(xy))
    area = f"{line} L{xy[-1][0]},{h} L{xy[0][0]},{h} Z"
    return {"line": line, "area": area, "last": xy[-1], "w": w, "h": h}


def bars(values: Sequence[float], w: float = 120, h: float = 34, gap: float = 3) -> list[dict[str, float]]:
    """Bar rects (x, y, w, h) in a ``w`` x ``h`` box; the tallest fills it, zero gets a 1px stub."""
    if not values:
        return []
    top = max(max(values), 0.0) or 1.0
    bw = (w - gap * (len(values) - 1)) / len(values)
    out = []
    for i, v in enumerate(values):
        bh = max(1.0, (max(v, 0.0) / top) * (h - 2))
        out.append({"x": round(i * (bw + gap), 2), "y": round(h - bh, 2), "w": round(bw, 2), "h": round(bh, 2)})
    return out


def _voice_timings() -> list[dict[str, Any]]:
    from voice import web

    return list(web.TIMINGS)


def _spend_by_day(today: date, days: int = 7) -> list[tuple[date, float]]:
    from assistant import store

    out = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        a, b = db.local_day_bounds(d)
        out.append((d, round(store.assistant_spend_between(a, b), 4)))
    return out


def charts(today: date | None = None) -> dict[str, Any]:
    """The five HUD charts: Claude usage today, assistant spend, voice latency, money this week, Shorts views."""
    h = _home()
    today = today or db.local_today()
    s = h._asettings()
    # 1. Claude usage today (plan runs vs the daily cap; the 5-hour window when Claude Code reported it)
    def claude() -> dict[str, Any]:
        from assistant import usage

        u = usage.usage_status()
        cap = max(1, int(u.get("runs_cap") or s.max_turns_per_day or 1))
        runs = int(u.get("runs_today") or 0)
        five = (u.get("windows") or {}).get("five_hour") or {}
        return {"runs": runs, "cap": cap, "pct": min(100.0, runs / cap * 100), "five_left": five.get("left_pct"),
                "limited": bool(u.get("limited")), "limited_text": u.get("limited_text") or ""}
    claude_d = _safe(claude, {"runs": 0, "cap": max(1, s.max_turns_per_day), "pct": 0.0, "five_left": None,
                              "limited": False, "limited_text": ""}, "claude usage")
    # 2. assistant spend, 7 days
    spend = _safe(lambda: _spend_by_day(today), [], "assistant spend")
    spend_today = spend[-1][1] if spend else 0.0
    # 3. voice latency: speech end -> first spoken word (ms), newest last
    lat = [t.get("speech_to_first_word_ms") for t in _safe(_voice_timings, [], "voice timings")]
    lat = [float(v) for v in lat if isinstance(v, (int, float))][-20:]
    # 4. money this week (8 weeks of pay)
    money = _safe(lambda: h.money_data(today), None, "money")
    pay = [w["pay"] for w in money["series"]] if money else []
    # 5. Shorts views, 14 days
    def views() -> list[float]:
        d = _d()
        start = today - timedelta(days=13)
        with d._conn() as c:
            daily = d.stats_by_day(c, start, today)
        return [float(daily.get((start + timedelta(days=i)).isoformat(), {}).get("views") or 0) for i in range(14)]
    v14 = _safe(views, [], "views")
    return {
        "claude": claude_d,
        "spend": {"today": spend_today, "budget": s.daily_budget_usd, "week": round(sum(v for _, v in spend), 4),
                  "bars": bars([v for _, v in spend]), "days": [d.strftime("%a")[0] for d, _ in spend],
                  "fake": s.fake},
        "latency": {"last": lat[-1] if lat else None, "median": sorted(lat)[len(lat) // 2] if lat else None,
                    "n": len(lat), "spark": spark(lat)},
        "money": {"week": money["week_pay"] if money else None, "hours": money["week_hours"] if money else None,
                  "has_job": bool(money and money["has_job"]), "bars": bars(pay)},
        "views": {"week": sum(v14[-7:]) if v14 else 0, "spark": spark(v14) if any(v14) else None},
    }


# ---------------------------------------------------------------------------
# the whole panel set + routes
# ---------------------------------------------------------------------------

def _brain_status() -> dict[str, Any]:
    from assistant import brain

    if not brain.running():
        return {"state": "cold", "label": "Brain asleep"}
    st = brain.get_brain().status()
    if st.get("recovering"):
        return {"state": "reconnecting", "label": "Brain reconnecting"}
    if st.get("busy"):
        return {"state": "busy", "label": "Brain working"}
    if st.get("connected"):
        return {"state": "warm", "label": "Brain warm"}
    return {"state": "connecting", "label": "Brain connecting"}


def cecil_data() -> dict[str, Any]:
    today = db.local_today()
    a = _home()._asettings()
    staff = _safe(lambda: staff_at_work(today), [], "staff at work")
    brain = _safe(_brain_status, {"state": "unknown", "label": "Brain unknown"}, "brain")
    if a.fake:
        brain = {"state": "practice", "label": "Practice brain"}
    if not a.enabled:
        brain = {"state": "off", "label": "Assistant off"}
    return {
        "chats": _safe(recent_chats, [], "recent chats"),
        "staff": staff,
        "work": _safe(recent_work, [], "recent work"),
        "new": _safe(new_stuff, [], "new stuff"),
        "charts": _safe(lambda: charts(today), None, "charts"),
        "brain": brain,
        "working": sum(1 for x in staff if x["state"] == "working"),
        "tasks": len(_safe(_running_tasks, [], "tasks")),
        "refresh": REFRESH_SECONDS,
    }


PARTS = ("chats", "staff", "work", "new", "charts")


def render_parts(request: Request, data: dict[str, Any]) -> dict[str, str]:
    d = _d()
    ctx = {"csrf": d.csrf_token(), "link": d._link_factory(request), "glyph": _home().glyph}
    mod = d._env().get_template("cecil_parts.html").make_module(ctx)
    return {name: str(getattr(mod, name)(data)) for name in PARTS}


def api_cecil(request: Request) -> Response:
    d = _d()
    resp, _ = d._guard(request, api=True)
    if resp is not None:
        return resp
    data = cecil_data()
    return d._json({"generated_at": db.now_iso(), "brain": data["brain"], "working": data["working"],
                    "tasks": data["tasks"],
                    "staff": [{k: x[k] for k in ("name", "display", "state", "doing")} for x in data["staff"]],
                    "html": render_parts(request, data)})


def staff_page(request: Request) -> Response:
    d = _d()
    resp, _ = d._guard(request)
    if resp is not None:
        return resp
    h = _home()
    today = db.local_today()
    roster = _safe(lambda: h.roster_data(today), [], "roster")
    live = {x["name"]: x for x in _safe(lambda: staff_at_work(today), [], "staff at work")}
    return d._render(request, "staff.html", {"roster": roster, "live": live, "glyph": h.glyph,
                                              "a": h._asettings()}, nav="staff")


def register(router: Any) -> None:
    router.add_api_route("/api/cecil", api_cecil, methods=["GET"])
    router.add_api_route("/staff", staff_page, methods=["GET"], include_in_schema=False)
