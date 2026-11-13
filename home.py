"""Command centre: the dashboard home (``/dashboard/``), the memory browser and the agent pages.

Everything here is composed in plain code from the database and the vault - no LLM call on
page load. The only route that reaches a model is ``POST /dashboard/chat`` (the owner talking
to the assistant), behind the same auth, CSRF and a per-session rate limit.

Routes (registered on ``dashboard.router`` by :func:`register`):

* ``GET  /``                          home: greeting hero, "needs you", life tiles, agent roster,
                                      chat, memory, activity feed (``?period=`` -> ``/studio``)
* ``GET  /api/home``                  live JSON for the home page (polled every ~25 s)
* ``GET  /memory``                    read-only vault browser (``?note=`` path, ``?q=`` search)
* ``GET  /agents/{name}``             one agent: role, model, skills, tools, recent runs
* ``POST /chat``                      -> ``assistant.runtime.run_turn(message, channel="dashboard")``
* ``POST /checkins/{id}/answer``      answer a check-in (hours check-ins also log the hours)
* ``POST /meals/{id}/accept|decline`` meal suggestion feedback
* ``POST /jobs``                      "tell me your pay rate"

POSTs answer JSON when the request asks for it (``Accept: application/json``, used by
``static/home.js``) and otherwise redirect back with a flash message, so every form also
works without JavaScript.

This module never imports ``dashboard`` at import time (``dashboard`` imports us at the end of
its own module); helpers reach it through :func:`_d`.
"""

from __future__ import annotations

import html
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import Request
from fastapi.responses import RedirectResponse, Response

import db

log = logging.getLogger(__name__)

CHAT_MAX_CHARS = 1000
CHAT_HISTORY = 14
CHAT_LIMITS = ((60, 6), (3600, 60))          # (window seconds, max chat posts) per session
POST_LIMITS = ((60, 30), (3600, 300))        # every other new POST, per session
ANSWER_MAX_CHARS = 500
FEED_ITEMS = 10
REFRESH_SECONDS = 25
WORKING_MINUTES = 15                          # a "running" agent run older than this is stale

AGENT_ROLES = {
    "main": "Chief of staff", "studio": "Studio director", "ledger": "Bookkeeper",
    "quartermaster": "Quartermaster", "archivist": "Archivist", "researcher": "Researcher",
    "engineer": "Engineer", "briefer": "Briefer",
}
AGENT_ORDER = ("main", "studio", "ledger", "quartermaster", "archivist", "briefer", "researcher", "engineer")


def _d():
    import dashboard

    return dashboard


def _asettings():
    from assistant.settings import get_assistant_settings

    return get_assistant_settings()


# ===========================================================================
# Pixel art (original sprites, drawn here; rendered as crisp inline SVG)
# ===========================================================================

#: 16x16 base figure. k outline, h hair, s skin, e eye, b cheek, m mouth, c coat, w shirt, a tie,
#: t hat, g glass, x prop, '.' keep/empty. In overlays a space clears a pixel.
_BASE = [
    "................",
    "....kkkkkkkk....",
    "...khhhhhhhhk...",
    "...khhhhhhhhk...",
    "...khsssssshk...",
    "...ksessssesk...",
    "...kbssssssbk...",
    "...ksssmmsssk...",
    "....kssssssk....",
    "..kccwwaawwcck..",
    ".kcccwaaaawccck.",
    ".kcccwwwwwwccck.",
    ".kscccwwwwcccsk.",
    ".kkcccwwwwccckk.",
    "..kcccckkcccck..",
    "..kkkk....kkkk..",
]

_GEAR: dict[str, dict[int, str]] = {
    "main": {},
    "studio": {0: "...tttttttt.....", 1: "..tttttttttt....", 11: "..............xx", 12: "..............xx"},
    "ledger": {2: "...kttttttttk...", 3: "..ttttttttttt...", 12: ".............sxx", 13: "..............xx"},
    "quartermaster": {0: "....wwwwwwww....", 1: "...wwwwwwwwww...", 2: "...kwwwwwwwwk...", 3: "...kttttttttk...",
                      11: ".kccxxxxxxxxcck.", 12: ".kscxxxxxxxxcsk.", 13: ".kkcxxxxxxxxckk."},
    "archivist": {5: "...kgegssgegk...", 11: "..............xx", 12: ".............sxx", 13: "..............xx"},
    "researcher": {0: ".....tttttt.....", 1: "....tttttttt....", 2: "..ttttttttttt...",
                   9: ".............ggg", 10: ".............g g", 11: ".............ggg", 12: ".............x.."},
    "engineer": {0: ".....tttttt.....", 1: "....tttttttt....", 2: "..tttttttttttt..",
                 11: "..............x.", 12: ".............xxx", 13: "..............x."},
    "briefer": {11: "..........xxxxx.", 12: "..........xxxxx.", 13: "..........xxxxx."},
}

#: Alert pose for the butler: right arm up, ringing a little bell, mouth open.
_ALERT = {3: "..............a.", 4: ".............aaa", 5: ".............aaa", 6: "..............s.",
          7: "..............ck", 8: ".......mm.....ck", 9: ".............cck", 12: ".............ck "}

_PX_CLASS = {"k": "k", "h": "h", "s": "s", "e": "e", "b": "b", "m": "m", "c": "c", "w": "w", "a": "a",
             "t": "t", "g": "g", "x": "x"}

AGENT_COLORS: dict[str, tuple[str, str, str, str]] = {
    # coat, hat, prop, hair
    "main": ("#2b303d", "#2b303d", "#d8a24a", "#3a2a22"),
    "studio": ("#5f3b56", "#a8433f", "#e6e1d6", "#2a2320"),
    "ledger": ("#2f5845", "#4d8a5e", "#d8a24a", "#5a3a26"),
    "quartermaster": ("#7a5634", "#d9d2c4", "#efe9dc", "#4a3020"),
    "archivist": ("#454b74", "#454b74", "#9c4d3c", "#7d7a74"),
    "researcher": ("#5d5640", "#8a7550", "#9fc3cf", "#3b2a20"),
    "engineer": ("#3c5f70", "#d29a3a", "#9aa3ad", "#2b2018"),
    "briefer": ("#733a3a", "#733a3a", "#e9e3d6", "#5b3b2a"),
}


def _grid(agent: str, pose: str) -> list[str]:
    rows = list(_BASE)
    for overlay in (_GEAR.get(agent, {}), _ALERT if pose == "alert" else {}):
        for y, patch in overlay.items():
            row = list(rows[y])
            for x, ch in enumerate(patch[:16]):
                if ch == " ":
                    row[x] = "."
                elif ch != ".":
                    row[x] = ch
            rows[y] = "".join(row)
    return rows


def sprite_svg(agent: str = "main", *, pose: str = "idle", size: int = 48, label: str = "",
               cls: str = "") -> str:
    """An original 16x16 pixel character as inline SVG (runs merged, eyes on their own layer)."""
    key = agent if agent in AGENT_COLORS else "researcher"
    rows = _grid(key, pose)
    coat, hat, prop, hair = AGENT_COLORS[key]
    rects: list[str] = []
    eyes: list[str] = []
    for y, row in enumerate(rows):
        x = 0
        while x < 16:
            ch = row[x]
            if ch == ".":
                x += 1
                continue
            if ch == "e":
                rects.append(f'<rect x="{x}" y="{y}" width="1" height="1" class="px-s"/>')
                eyes.append(f'<rect x="{x}" y="{y}" width="1" height="1"/>')
                x += 1
                continue
            start = x
            while x < 16 and row[x] == ch:
                x += 1
            rects.append(f'<rect x="{start}" y="{y}" width="{x - start}" height="1" class="px-{_PX_CLASS.get(ch, "k")}"/>')
    title = f"<title>{html.escape(label)}</title>" if label else ""
    aria = f'role="img" aria-label="{html.escape(label)}"' if label else 'aria-hidden="true"'
    tie = "var(--accent)" if key == "main" else hat
    style = f"--coat:{coat};--hat:{hat};--prop:{prop};--hair:{hair};--tie:{tie}"
    return (f'<svg class="sprite {html.escape(cls)}" viewBox="0 0 16 16" width="{size}" height="{size}" '
            f'shape-rendering="crispEdges" style="{style}" {aria}>{title}<g class="sprite-body">{"".join(rects)}'
            f'<g class="px-eyes">{"".join(eyes)}</g></g></svg>')


#: 8x8 section glyphs ('#' = filled), drawn in currentColor.
GLYPHS = {
    "bell": ["...##...", "..####..", ".######.", ".######.", ".######.", "########", "........", "...##..."],
    "coin": ["..####..", ".#....#.", "#..##..#", "#.#....#", "#..##..#", "#....#.#", ".#.##..#", "..####.."],
    "film": ["########", "#.#..#.#", "########", "#......#", "#......#", "########", "#.#..#.#", "########"],
    "sun": ["...#....", ".#...#..", "...##...", "#.####.#", "..####..", "...##...", ".#...#..", "...#...."],
    "flag": ["#.......", "######..", "#######.", "######..", "#.......", "#.......", "#.......", "#......."],
    "team": ["........", ".##..##.", ".##..##.", "........", "###..###", "###..###", "###..###", "........"],
    "chat": ["########", "#......#", "#.##.#.#", "#......#", "########", "..##....", ".#......", "........"],
    "book": ["........", ".###.###", "#..#.#..", "#..#.#..", "#..#.#..", "#..#.#..", ".###.###", "........"],
    "scroll": [".######.", "#......#", ".#.##.#.", ".#....#.", ".#.###..", ".#....#.", "#......#", ".######."],
    "spark": ["...#....", "...#....", "..###...", "#######.", "..###...", "...#....", "...#....", "........"],
}


def glyph(name: str, cls: str = "glyph") -> str:
    rows = GLYPHS.get(name, GLYPHS["spark"])
    rects = []
    for y, row in enumerate(rows):
        x = 0
        while x < 8:
            if row[x] != "#":
                x += 1
                continue
            s = x
            while x < 8 and row[x] == "#":
                x += 1
            rects.append(f'<rect x="{s}" y="{y}" width="{x - s}" height="1"/>')
    return (f'<svg class="{html.escape(cls)}" viewBox="0 0 8 8" width="14" height="14" shape-rendering="crispEdges" '
            f'aria-hidden="true" fill="currentColor">{"".join(rects)}</svg>')


# ===========================================================================
# Safe markdown (vault notes): escape first, then a small, closed set of formatting
# ===========================================================================

_WIKI = re.compile(r"\[\[([^\]|#]+)(#[^\]|]*)?(?:\|([^\]]*))?\]\]")
_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_ITAL = re.compile(r"(?<![\w*])[*_](?=\S)(.+?)(?<=\S)[*_](?![\w*])")
_MDLINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def note_href(rel: str) -> str:
    return "/dashboard/memory?" + urlencode({"note": rel})


def _note_index() -> dict[str, str]:
    """lower-case stem / path-without-.md -> vault-relative path."""
    from assistant import vault

    out: dict[str, str] = {}
    try:
        notes = vault.list_notes()
    except Exception:
        return out
    for rel in notes:
        p = PurePosixPath(rel)
        out.setdefault(p.stem.lower(), rel)
        out[str(p.with_suffix("")).lower()] = rel
    return out


def _inline(escaped: str, resolve: Callable[[str], str | None]) -> str:
    """Formatting on ALREADY ESCAPED text. Only produces <code>, <strong>, <em>, <a class=wikilink>."""
    codes: list[str] = []

    def keep_code(m: re.Match[str]) -> str:
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    s = _CODE.sub(keep_code, escaped)

    def wiki(m: re.Match[str]) -> str:
        target = html.unescape(m.group(1)).strip()
        label = m.group(3) if m.group(3) is not None else m.group(1)
        rel = resolve(target)
        if rel is None:
            return f'<span class="wikilink is-missing" title="No note called this yet">{label.strip()}</span>'
        return f'<a class="wikilink" href="{html.escape(note_href(rel), quote=True)}">{label.strip()}</a>'

    s = _WIKI.sub(wiki, s)
    s = _MDLINK.sub(lambda m: f'{m.group(1)} <span class="md-url">({m.group(2)})</span>', s)
    s = _BOLD.sub(r"<strong>\1</strong>", s)
    s = _ITAL.sub(r"<em>\1</em>", s)
    return re.sub(r"\x00(\d+)\x00", lambda m: f"<code>{codes[int(m.group(1))]}</code>", s)


def render_markdown(text: str, resolve: Callable[[str], str | None] | None = None) -> str:
    """Markdown subset -> HTML. Every character of input is escaped before any tag is added,
    so raw HTML in a note is always shown as text."""
    if resolve is None:
        index = _note_index()

        def resolve(target: str) -> str | None:  # noqa: F811 - default resolver
            t = target.lower().removesuffix(".md")
            return index.get(t) or index.get(PurePosixPath(t).name)

    out: list[str] = []
    para: list[str] = []
    list_tag: str | None = None
    in_code = False
    code: list[str] = []

    def flush_para() -> None:
        if para:
            out.append("<p>" + "<br>".join(para) + "</p>")
            para.clear()

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        if raw.strip().startswith("```"):
            if in_code:
                out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
                code.clear()
                in_code = False
            else:
                flush_para()
                close_list()
                in_code = True
            continue
        if in_code:
            code.append(raw)
            continue
        line = raw.rstrip()
        esc = html.escape(line.strip(), quote=True)
        if not line.strip():
            flush_para()
            close_list()
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line.strip())
        if m:
            flush_para()
            close_list()
            level = min(4, len(m.group(1)) + 1)  # note h1 -> h2: the page owns h1
            out.append(f"<h{level}>{_inline(html.escape(m.group(2), quote=True), resolve)}</h{level}>")
            continue
        if re.match(r"^(-{3,}|\*{3,})$", line.strip()):
            flush_para()
            close_list()
            out.append("<hr>")
            continue
        m = re.match(r"^\s*(?:[-*+])\s+(?:\[( |x|X)\]\s+)?(.*)$", line)
        if m:
            flush_para()
            if list_tag != "ul":
                close_list()
                out.append("<ul>")
                list_tag = "ul"
            box = "" if m.group(1) is None else ('<span class="check" aria-label="done">☑</span> ' if m.group(1).lower() == "x"
                                                  else '<span class="check" aria-label="to do">☐</span> ')
            out.append(f"<li>{box}{_inline(html.escape(m.group(2), quote=True), resolve)}</li>")
            continue
        m = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        if m:
            flush_para()
            if list_tag != "ol":
                close_list()
                out.append("<ol>")
                list_tag = "ol"
            out.append(f"<li>{_inline(html.escape(m.group(1), quote=True), resolve)}</li>")
            continue
        if line.strip().startswith(">"):
            flush_para()
            close_list()
            out.append(f"<blockquote>{_inline(html.escape(line.strip().lstrip('>').strip(), quote=True), resolve)}"
                       "</blockquote>")
            continue
        close_list()
        para.append(_inline(esc, resolve))
    if in_code:
        out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
    flush_para()
    close_list()
    return "\n".join(out)


# ===========================================================================
# Data
# ===========================================================================

def _safe(fn: Callable[[], Any], default: Any, what: str) -> Any:
    """Home is a summary: one broken source of data must not take the page down."""
    try:
        return fn()
    except Exception as exc:  # pragma: no cover - logged, page still renders
        log.warning("home: %s unavailable: %s", what, exc)
        return default


def _local_date(iso: str | None) -> date | None:
    if not iso:
        return None
    try:
        return db.local_date_of(iso)
    except Exception:
        return None


def assistant_state() -> dict[str, Any]:
    """Is chat available, and if not, why (in the assistant's voice)."""
    from assistant import store

    s = _asettings()
    spent = _safe(store.assistant_spent_today, 0.0, "assistant spend")
    remaining = max(0.0, s.daily_budget_usd - spent)
    paused = db.kv_get("budget:paused") is not None
    reason = None
    if not s.enabled:
        reason = (f"I am switched off for now, {s.owner_honorific}. Set ASSISTANT_ENABLED=true in config.env "
                  "and restart, and I will answer here and by text.")
    elif paused:
        reason = "Spending is paused, so I am staying quiet. Text RESUME (or clear the pause) to bring me back."
    elif remaining <= 0.0:
        reason = f"I have spent today's allowance (${s.daily_budget_usd:.2f}). I will be back after midnight."
    elif not s.fake and s.backend == "claude_code":
        from assistant import claude_code

        until = _safe(claude_code.backoff_until, None, "claude backoff")
        if until is not None:
            reason = (f"I've reached your Claude plan's limit for now, {s.owner_honorific}. "
                      f"I'll be back {claude_code.friendly_time(until)}.")
        elif _safe(claude_code.turns_today, 0, "claude runs") >= s.max_turns_per_day:
            reason = (f"I have used today's {s.max_turns_per_day} Claude conversations, {s.owner_honorific}. "
                      "I will be back after midnight.")
    return {"name": s.name, "honorific": s.owner_honorific, "owner": s.owner_name, "enabled": s.enabled,
            "fake": s.fake, "can_chat": reason is None, "reason": reason, "spent_today": round(spent, 4),
            "budget": s.daily_budget_usd, "remaining": round(remaining, 4),
            "model": _main_model(s),
            "backend": s.backend,
            "subagent_model": s.subagent_model}


def _ledger_summary() -> dict[str, Any] | None:
    """Prefer ``assistant.ledger.summary`` when the ledger module exists and works."""
    try:
        from assistant import ledger  # type: ignore[attr-defined]
    except Exception:
        return None
    fn = getattr(ledger, "summary", None)
    if not callable(fn):
        return None
    try:
        out = fn(weeks=8)
    except Exception as exc:
        log.warning("ledger.summary failed, computing locally: %s", exc)
        return None
    return out if isinstance(out, dict) else None


def _num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


_MONEY_GOAL = re.compile(r"(income|sav|money|usd|dollar|\$|earn|pay)", re.I)


def money_data(today: date) -> dict[str, Any]:
    from assistant import store

    jobs = store.list_jobs()
    rates = {j.id: j.hourly_rate for j in store.list_jobs(active_only=False)}
    this_monday = store.week_start_of(today)
    weeks = [this_monday - timedelta(weeks=i) for i in range(7, -1, -1)]
    pay = {w.isoformat(): 0.0 for w in weeks}
    hours = {w.isoformat(): 0.0 for w in weeks}
    for row in store.list_work_log(since_week=weeks[0], limit=1000):
        if row.week_start in pay:
            pay[row.week_start] += row.hours * rates.get(row.job_id, 0.0)
            hours[row.week_start] += row.hours
    week_key = this_monday.isoformat()
    last_key = (this_monday - timedelta(weeks=1)).isoformat()
    month_start = today.replace(day=1)
    received_month = store.income_total(month_start, today)
    job_month = store.income_total(month_start, today, source="job")
    reels_income = store.income_total(month_start, today, source="reels")
    # Reels: revenue recorded in the ledger, else YouTube's estimate, minus the pipeline's LLM spend.
    m0, _ = db.local_day_bounds(month_start)
    _, m1 = db.local_day_bounds(today)
    llm_month = max(0.0, db.llm_spend_between(m0, m1) - store.assistant_spend_between(m0, m1))
    yt_revenue = None
    if not reels_income:
        d = _d()
        with d._conn() as c:
            col = d.revenue_column(c)
            if col:
                r = c.execute(f"SELECT SUM({col}) AS s, COUNT({col}) AS n FROM stats WHERE day >= ? AND day <= ?",
                              (month_start.isoformat(), today.isoformat())).fetchone()
                yt_revenue = float(r["s"]) if r and r["n"] else None
    revenue = reels_income or yt_revenue
    goal = next((g for g in store.list_goals("active") if g.target and (_MONEY_GOAL.search(g.metric or "")
                                                                       or _MONEY_GOAL.search(g.title))), None)
    series = [{"week": w.isoformat(), "label": f"{w.strftime('%b')} {w.day}", "pay": round(pay[w.isoformat()], 2),
               "hours": round(hours[w.isoformat()], 2)} for w in weeks]
    missing = []
    if not jobs:
        missing.append("pay_rate")
    elif not hours[week_key]:
        missing.append("hours_this_week")
    out = {
        "jobs": [{"id": j.id, "name": j.name, "rate": j.hourly_rate} for j in jobs],
        "has_job": bool(jobs),
        "week_pay": round(pay[week_key], 2), "week_hours": round(hours[week_key], 2),
        "last_week_pay": round(pay[last_key], 2),
        "received_month": round(received_month, 2), "job_month": round(job_month, 2),
        "reels_revenue": None if revenue is None else round(revenue, 2),
        "reels_revenue_source": "ledger" if reels_income else ("youtube" if yt_revenue is not None else None),
        "reels_costs": round(llm_month, 2),
        "reels_profit": None if revenue is None else round(revenue - llm_month, 2),
        "series": series, "max_pay": max([s["pay"] for s in series] + [0.0]),
        "goal": None if goal is None else {
            "title": goal.title, "current": goal.current or 0.0, "target": goal.target,
            "pct": max(0.0, min(100.0, (goal.current or 0.0) / goal.target * 100)) if goal.target else 0.0},
        "missing": missing, "source": "store",
    }
    led = _ledger_summary()
    if led:
        # The ledger module is the authority when present; take the fields it names plainly.
        for key, ours in (("this_week", "week_pay"), ("last_week", "last_week_pay"), ("month", "received_month")):
            v = led.get(key)
            if isinstance(v, Mapping):
                v = v.get("total", v.get("gross", v.get("amount")))
            if _num(v) is not None:
                out[ours] = round(float(v), 2)
        if isinstance(led.get("missing"), list):
            out["missing"] = [str(x) for x in led["missing"]][:6]
        out["source"] = "ledger"
    return out


def studio_data(today: date) -> dict[str, Any]:
    from assistant import tools

    d = _d()
    snap = tools.studio_snapshot(7)
    start = today - timedelta(days=13)
    with d._conn() as c:
        daily = d.stats_by_day(c, start, today)
        ups = d.daily_uploads(c, today - timedelta(days=6), today)
        latest = db.list_channel_stats(limit=1, conn=c)
    views = [daily.get((start + timedelta(days=i)).isoformat(), {}).get("views") for i in range(14)]
    return {
        "posted_7d": int(sum(v.get("n", 0) for v in ups.values())),
        "views_7d": snap.get("period_views") or 0,
        "subscribers": latest[0].subscribers if latest else None,
        "uploads_today": snap.get("uploads_today", 0), "upload_cap": snap.get("upload_cap"),
        "spark": d.sparkline([v or 0 for v in views]) if any(views) else None,
        "top": (snap.get("top_clips") or [None])[0],
        "pending": len(snap.get("pending_approvals") or []),
        "paused": snap.get("pipeline_paused"),
        "has_data": bool(any(views) or latest or snap.get("period_clips_tracked")),
    }


def today_data(today: date) -> dict[str, Any]:
    from assistant import store

    start, end = db.local_day_bounds(today)
    checkins = [c for c in store.list_checkins("pending", limit=200) if start <= c.scheduled_for < end]
    meals = {m.slot: m for m in store.list_meals(since_day=today, limit=30) if m.day == today.isoformat()}
    return {"checkins": checkins, "meals": [(slot, meals.get(slot)) for slot in store.MEAL_SLOTS],
            "has_meals": bool(meals)}


def streak_of(days: set[date], today: date) -> int:
    """Consecutive days ending today (or yesterday, if today has no entry yet)."""
    day = today if today in days else today - timedelta(days=1)
    n = 0
    while day in days:
        n += 1
        day -= timedelta(days=1)
    return n


def goals_data(today: date) -> dict[str, Any]:
    from assistant import store

    answered = {d for d in (_local_date(c.answered_at) for c in store.list_checkins("answered", limit=1000)) if d}
    goals = []
    for g in store.list_goals("active"):
        pct = None
        if g.target:
            pct = max(0.0, min(100.0, (g.current or 0.0) / g.target * 100))
        goals.append({"id": g.id, "title": g.title, "metric": g.metric, "current": g.current, "target": g.target,
                      "due": g.due, "pct": pct})
    week = {today - timedelta(days=i) for i in range(7)}
    return {"streak": streak_of(answered, today), "goals": goals, "answered_7d": len(answered & week),
            "week_dots": [(today - timedelta(days=6 - i), (today - timedelta(days=6 - i)) in answered) for i in range(7)]}


def _short_model(m: str) -> str:
    return m.removeprefix("claude-")


def _main_model(s: Any) -> str:
    if s.fake or s.backend != "claude_code":
        return s.model
    from assistant import claude_code

    return _safe(lambda: claude_code.model_in_use("main"), s.model, "claude model")


def _agent_model(spec: Any, s: Any) -> str:
    """Model shown on the roster: on the Claude account (claude_code backend), the one actually
    answering for that role (kv), marked as the plan's; else the configured API model."""
    if not s.fake and s.backend == "claude_code":
        from assistant import claude_code

        role = "main" if spec.name == "main" else "sub"
        return _short_model(_safe(lambda: claude_code.model_in_use(role), role, "claude model")) + " (Claude plan)"
    return _short_model(spec.resolved_model(s))


def roster_data(today: date) -> list[dict[str, Any]]:
    from assistant import registry, store

    s = _asettings()
    try:
        skills = registry.load_skills(strict=False)
        agents = registry.load_agents(skills=skills, strict=False)
    except Exception as exc:
        log.warning("home: agents unavailable: %s", exc)
        return []
    start, _ = db.local_day_bounds(today)
    now = db.utcnow()
    order = {n: i for i, n in enumerate(AGENT_ORDER)}
    out = []
    for name, spec in sorted(agents.items(), key=lambda kv: (order.get(kv[0], 99), kv[0])):
        runs = store.list_agent_runs(8, agent=name)
        today_runs = store.list_agent_runs(500, agent=name, since=start)
        last = runs[0] if runs else None
        status = "idle"
        if last is not None:
            age = now - db.parse_iso(last.ts)
            if last.status == "running" and age < timedelta(minutes=WORKING_MINUTES):
                status = "working"
            elif last.status == "error" and age < timedelta(hours=24):
                status = "error"
        out.append({
            "name": name, "display": s.name if name == "main" else name.capitalize(),
            "role": AGENT_ROLES.get(name, name.replace("-", " ").capitalize()),
            "description": spec.description, "model": _agent_model(spec, s),
            "tools": list(spec.tools), "skills": [{"name": k, "description": skills[k].description if k in skills else ""}
                                                    for k in spec.skills],
            "status": status, "last": last, "runs": runs,
            "runs_today": len(today_runs), "cost_today": round(sum(r.cost_usd for r in today_runs), 4),
            "sprite": sprite_svg(name, size=40), "sprite_big": sprite_svg(name, size=96, label=f"{name} sprite"),
        })
    return out


def needs_data() -> dict[str, Any]:
    from assistant import store

    d = _d()
    pending = db.list_decisions("pending", limit=10)
    now = db.now_iso()
    checkins = [c for c in store.list_checkins("pending", limit=200) if c.sent_at or c.scheduled_for <= now][:5]
    with d._conn() as c:
        fails = d.failures_24h(c)
        last_err = db.list_events(limit=1, level="error", conn=c)
    since = db.to_iso(db.utcnow() - timedelta(hours=24))
    run_errors = [r for r in store.list_agent_runs(50, since=since) if r.status == "error"]
    return {"decisions": pending, "checkins": checkins, "failures_24h": fails,
            "last_error": last_err[0] if last_err else None, "run_errors": run_errors[:3],
            "count": len(pending) + len(checkins) + (1 if fails else 0) + (1 if run_errors else 0)}


def memory_data() -> dict[str, Any]:
    from assistant import store, vault

    root = vault.vault_root()
    recent = []
    if root.is_dir():
        notes = vault.list_notes()
        stamped = []
        for rel in notes:
            try:
                stamped.append(((root / rel).stat().st_mtime, rel))
            except OSError:
                continue
        for mtime, rel in sorted(stamped, reverse=True)[:5]:
            try:
                n = vault.read_note(rel)
            except Exception:
                continue
            facts = vault.facts_in(n.body)
            line = facts[-1] if facts else next((ln.strip("#- ").strip() for ln in n.body.splitlines()
                                                 if ln.strip() and not ln.startswith("#")), "")
            line = re.sub(r"^\[[ xX]\]\s*", "", line)
            line = re.sub(r"\s+—\s+_[^_]*_\s*$", "", line)
            recent.append({"path": rel, "title": n.title, "line": line[:140],
                           "updated": str(n.meta.get("updated") or "")})
        count = len(notes)
    else:
        count = 0
    lessons = store.list_lessons(limit=4)
    return {"recent": recent, "count": count, "lessons": lessons, "exists": root.is_dir()}


def feed_data(limit: int = FEED_ITEMS) -> list[dict[str, Any]]:
    from assistant import store

    s = _asettings()
    items: list[dict[str, Any]] = []
    for m in store.list_messages(limit):
        who = s.owner_name if m.direction == "in" else (s.name if (m.agent in (None, "main")) else m.agent.capitalize())
        items.append({"ts": m.ts, "kind": "message", "icon": "chat", "who": who,
                      "text": m.body, "meta": m.channel, "tone": "neutral"})
    for r in store.list_agent_runs(limit):
        if r.parent_id is not None and r.agent == "main":
            continue
        tone = {"error": "critical", "budget": "warning", "denied": "warning", "running": "accent"}.get(r.status, "neutral")
        items.append({"ts": r.ts, "kind": "run", "icon": "team",
                      "who": s.name if r.agent == "main" else r.agent.capitalize(),
                      "text": (r.summary or r.task or "")[:220], "meta": r.status if r.status != "ok" else f"${r.cost_usd:.3f}",
                      "tone": tone, "agent": r.agent})
    for u in db.list_uploads("uploaded", limit=limit):
        clip = db.get_clip(u.clip_id)
        items.append({"ts": u.uploaded_at or u.updated_at, "kind": "upload", "icon": "film", "who": "Studio",
                      "text": "Published “" + ((clip.title if clip else None) or f"clip {u.clip_id}") + "”",
                      "meta": "dry run" if u.dry_run else "YouTube", "tone": "neutral"})
    for dcs in db.list_decisions(limit=limit):
        if dcs.status == "pending":
            items.append({"ts": dcs.requested_at, "kind": "decision", "icon": "bell", "who": "Approval needed",
                          "text": dcs.summary, "meta": dcs.kind.replace("_", " "), "tone": "warning"})
        elif dcs.resolved_at:
            items.append({"ts": dcs.resolved_at, "kind": "decision", "icon": "bell", "who": f"Decision {dcs.status}",
                          "text": dcs.summary, "meta": dcs.resolved_via or "", "tone": "neutral"})
    items.sort(key=lambda i: i["ts"] or "", reverse=True)
    return items[:limit]


def chat_history(limit: int = CHAT_HISTORY) -> list[Any]:
    from assistant import store

    msgs = [m for m in store.list_messages(limit * 2) if m.channel in ("dashboard", "sms")][:limit]
    return list(reversed(msgs))


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


_WORDS = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


def _say_n(n: int) -> str:
    return _WORDS[n] if 0 <= n < len(_WORDS) else str(n)


def greeting(now_local: datetime, a: Mapping[str, Any], needs: Mapping[str, Any], money: Mapping[str, Any],
             studio: Mapping[str, Any], today: Mapping[str, Any], fresh: bool) -> dict[str, Any]:
    """The hero line, written in plain code from data. Returns {salute, line, mood}."""
    h = a["honorific"]
    hour = now_local.hour
    if hour < 5:
        salute = f"Still up, {h}?"
    elif hour < 12:
        salute = f"Good morning, {h}."
    elif hour < 17:
        salute = f"Good afternoon, {h}."
    elif hour < 22:
        salute = f"Good evening, {h}."
    else:
        salute = f"Burning the midnight oil, {h}?"
    n_dec = len(needs["decisions"])
    n_chk = len(needs["checkins"])
    mood = "alert" if (n_dec or n_chk or needs["failures_24h"] or needs["run_errors"]) else "idle"
    if fresh:
        line = ("Everything is new and quiet. A few small things below and I will be properly useful — "
                "starting with what you are paid.")
        return {"salute": salute, "line": line, "mood": mood if mood == "alert" else "idle"}
    if n_dec:
        first = needs["decisions"][0].summary
        line = (f"{_say_n(n_dec).capitalize()} decision{'s' if n_dec != 1 else ''} await{'' if n_dec != 1 else 's'} "
                f"your word — the first: {first[:90]}{'…' if len(first) > 90 else ''}")
    elif n_chk:
        line = f"I still need an answer from you: {needs['checkins'][0].question}"
    elif needs["failures_24h"]:
        line = (f"The studio stumbled {_say_n(needs['failures_24h'])} time{'s' if needs['failures_24h'] != 1 else ''} "
                "in the last day; it carries on, but you may want to look.")
    elif needs["run_errors"]:
        r = needs["run_errors"][0]
        line = f"{r.agent.capitalize()} hit a snag on its last task; I have kept the details below."
    elif "hours_this_week" in money.get("missing", []) and now_local.weekday() >= 4:
        line = "When you have a moment, tell me this week's hours and I will settle the books."
    elif studio.get("posted_7d") and studio.get("views_7d"):
        line = (f"The studio published {_say_n(studio['posted_7d'])} Short{'s' if studio['posted_7d'] != 1 else ''} "
                f"this week, drawing {_d().fmt_compact(studio['views_7d'])} views. All else is in order.")
    elif money.get("week_pay"):
        line = f"You have earned {_d().fmt_usd(money['week_pay'])} so far this week. All is in order."
    else:
        line = "All is in order. Nothing needs you right now."
    if not a["enabled"] and mood == "idle":
        line += " (I am in read-only mode until I am switched on.)"
    return {"salute": salute, "line": line, "mood": mood}


def setup_steps(a: Mapping[str, Any], money: Mapping[str, Any], memory: Mapping[str, Any],
                studio: Mapping[str, Any]) -> list[dict[str, Any]]:
    import config

    s = config.get_settings()
    return [
        {"done": a["enabled"], "title": "Switch me on",
         "body": "Set ASSISTANT_ENABLED=true in config.env and restart. I start in a free practice mode (FAKE_ASSISTANT) "
                 "until an API key is set."},
        {"done": money["has_job"], "title": "Tell me what you are paid",
         "body": "Your job and hourly rate, in the Money tile — so I can keep the books from your weekly hours."},
        {"done": memory["count"] > 12, "title": "Introduce yourself",
         "body": "Tell me a little about yourself in the chat below. I keep it in your private vault, which you can read under Memory."},
        {"done": studio["has_data"], "title": "Let the studio run",
         "body": "python main.py run-once finds footage and makes Shorts; python main.py schedule keeps it going."},
        {"done": bool(s.dashboard_token), "title": "Lock the door",
         "body": "Set DASHBOARD_TOKEN so this page can be opened safely from your phone."},
    ]


def home_data() -> dict[str, Any]:
    today = db.local_today()
    a = assistant_state()
    needs = _safe(needs_data, {"decisions": [], "checkins": [], "failures_24h": 0, "last_error": None,
                               "run_errors": [], "count": 0}, "needs")
    money = _safe(lambda: money_data(today), None, "money")
    studio = _safe(lambda: studio_data(today), {"has_data": False, "posted_7d": 0, "views_7d": 0, "subscribers": None,
                                                "spark": None, "top": None, "pending": 0, "uploads_today": 0,
                                                "upload_cap": None, "paused": False}, "studio")
    tday = _safe(lambda: today_data(today), {"checkins": [], "meals": [], "has_meals": False}, "today")
    goals = _safe(lambda: goals_data(today), {"streak": 0, "goals": [], "answered_7d": 0, "week_dots": []}, "goals")
    roster = _safe(lambda: roster_data(today), [], "roster")
    memory = _safe(memory_data, {"recent": [], "count": 0, "lessons": [], "exists": False}, "memory")
    feed = _safe(feed_data, [], "feed")
    history = _safe(chat_history, [], "chat")
    money_ok = money or {"has_job": False, "missing": ["pay_rate"], "series": [], "max_pay": 0, "jobs": [],
                         "week_pay": 0, "week_hours": 0, "last_week_pay": 0, "received_month": 0, "job_month": 0,
                         "reels_revenue": None, "reels_costs": 0, "reels_profit": None, "goal": None,
                         "reels_revenue_source": None, "source": "none"}
    fresh = not (money_ok["has_job"] or history or feed or memory["count"] > 12)
    hello = greeting(db.local_now(), a, needs, money_ok, studio, tday, fresh)
    steps = setup_steps(a, money_ok, memory, studio)
    return {"a": a, "needs": needs, "money": money_ok, "studio": studio, "today": tday, "goals": goals,
            "roster": roster, "memory": memory, "feed": feed, "history": history, "hello": hello,
            "steps": steps, "steps_left": sum(1 for x in steps if not x["done"]), "fresh": fresh,
            "today_date": today, "refresh": REFRESH_SECONDS,
            "avatar": {"idle": sprite_svg("main", size=96, cls="avatar-idle"),
                       "alert": sprite_svg("main", pose="alert", size=96, cls="avatar-alert")}}


# ===========================================================================
# Rate limiting (per session; in memory - one process serves the web)
# ===========================================================================

_hits: dict[str, list[float]] = {}
_hits_lock = threading.Lock()


def _session_key(request: Request, method: str) -> str:
    d = _d()
    if method == "cookie":
        return "c:" + (request.cookies.get(d.COOKIE_NAME) or "")[:24]
    if method == "bearer":
        return "b:" + d._client_ip(request)
    return "ip:" + d._client_ip(request)


def rate_limited(request: Request, method: str, bucket: str,
                 limits: Sequence[tuple[int, int]]) -> int | None:
    """Record a hit; return seconds to wait when over a limit (the hit is then not recorded)."""
    now = time.monotonic()
    key = f"{bucket}|{_session_key(request, method)}"
    longest = max(w for w, _ in limits)
    with _hits_lock:
        if len(_hits) > 10_000:
            _hits.clear()
        hits = [t for t in _hits.get(key, []) if now - t < longest]
        for window, limit in limits:
            recent = [t for t in hits if now - t < window]
            if len(recent) >= limit:
                _hits[key] = hits
                return max(1, int(window - (now - recent[0])) + 1)
        hits.append(now)
        _hits[key] = hits
    return None


# ===========================================================================
# Routes
# ===========================================================================

def _wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "") or request.headers.get("authorization") is not None


def _post_reply(request: Request, ok: bool, msg: str, *, status: int = 200, anchor: str = "",
                data: dict[str, Any] | None = None) -> Response:
    d = _d()
    if _wants_json(request):
        body = {"ok": ok, "msg": msg, **(data or {})}
        return d._json(body, status_code=status)
    if not ok and status in (403, 429, 413, 400) and msg in ("csrf",):
        return d._render(request, "error.html", {"title": "Forbidden", "message": d._FLASH["csrf"][1]},
                         status_code=403)
    return d._security_headers(RedirectResponse(f"/dashboard/?msg={msg}{('#' + anchor) if anchor else ''}",
                                                status_code=303))


async def _post_guard(request: Request, *, bucket: str = "post",
                      limits: Sequence[tuple[int, int]] = POST_LIMITS) -> tuple[Response | None, Any]:
    """Auth + CSRF + rate limit for every new POST. Returns (error response, form)."""
    d = _d()
    resp, method = d._guard(request, api=_wants_json(request))
    if resp is not None:
        return resp, None
    form = await request.form()
    if not d._csrf_ok(request, method, form):
        if _wants_json(request):
            return d._json({"ok": False, "error": "csrf", "msg": d._FLASH["csrf"][1]}, status_code=403), None
        return d._render(request, "error.html", {"title": "Forbidden", "message": d._FLASH["csrf"][1]},
                         status_code=403), None
    wait = rate_limited(request, method, bucket, limits)
    if wait is not None:
        msg = "Steady on - that is a lot of requests. Give it a minute."
        if _wants_json(request):
            r = d._json({"ok": False, "error": "rate_limited", "retry_after": wait, "msg": msg}, status_code=429)
        else:
            r = d._render(request, "error.html", {"title": "Slow down", "message": msg}, status_code=429)
        r.headers["Retry-After"] = str(wait)
        return r, None
    return None, form


def _render_home_parts(request: Request, data: Mapping[str, Any]) -> dict[str, str]:
    d = _d()
    ctx = {"csrf": d.csrf_token(), "link": d._link_factory(request), "glyph": glyph}
    mod = d._env().get_template("home_parts.html").make_module(ctx)
    return {name: str(getattr(mod, name)(data)) for name in ("needs", "tiles", "roster", "feed")}


def home_page(request: Request) -> Response:
    import config

    d = _d()
    resp, _ = d._guard(request)
    if resp is not None:
        return resp
    if not config.get_settings().setup_complete:  # first run: the web setup wizard
        return d._security_headers(RedirectResponse("/setup/", status_code=303))
    if request.query_params.get("period"):  # an old overview link: the analytics moved to /studio
        return d._security_headers(RedirectResponse("/dashboard/studio?" + urlencode(
            [(k, v) for k, v in request.query_params.multi_items() if k != "token"]), status_code=303))
    data = home_data()
    return d._render(request, "home.html", {"h": data, "glyph": glyph, "chat_max": CHAT_MAX_CHARS}, nav="home")


def api_home(request: Request) -> Response:
    d = _d()
    resp, _ = d._guard(request, api=True)
    if resp is not None:
        return resp
    data = home_data()
    return d._json({
        "generated_at": db.now_iso(), "mood": data["hello"]["mood"], "salute": data["hello"]["salute"],
        "line": data["hello"]["line"], "needs": data["needs"]["count"],
        "assistant": {k: data["a"][k] for k in ("name", "enabled", "can_chat", "reason", "fake", "remaining")},
        "agents": [{k: r[k] for k in ("name", "status", "runs_today", "cost_today")} for r in data["roster"]],
        "html": _render_home_parts(request, data),
    })


async def chat_post(request: Request) -> Response:
    d = _d()
    err, form = await _post_guard(request, bucket="chat", limits=CHAT_LIMITS)
    if err is not None:
        return err
    message = str(form.get("message") or "").strip()
    if not message:
        return _post_reply(request, False, "chat_empty", status=400, anchor="talk")
    if len(message) > CHAT_MAX_CHARS:
        return _post_reply(request, False, "chat_long", status=413, anchor="talk")
    a = assistant_state()
    if not a["can_chat"]:
        return _post_reply(request, False, "chat_off", status=503, anchor="talk", data={"reason": a["reason"]})
    from fastapi.concurrency import run_in_threadpool

    from assistant import runtime

    try:
        res = await run_in_threadpool(runtime.run_turn, message, channel="dashboard")
    except Exception as exc:  # run_turn already turns model failures into a reply; this is the backstop
        db.log_event("dashboard", "chat turn failed", level="error", data={"error": repr(exc)[:300]})
        return _post_reply(request, False, "chat_failed", status=500, anchor="talk")
    db.log_event("dashboard", "owner chatted on the dashboard",
                 data={"status": res.status, "cost_usd": res.cost_usd, "message_id": res.message_in_id})
    return _post_reply(request, True, "chat_sent", anchor="talk",
                       data={"reply": res.text, "status": res.status, "cost_usd": res.cost_usd,
                             "cost_text": d.fmt_usd(res.cost_usd) if res.cost_usd else "$0",
                             "fake": a["fake"], "name": a["name"]})


_HOURS = re.compile(r"(\d+(?:\.\d+)?)")


def _week_for(checkin: Any) -> date:
    from assistant import store

    try:
        return store.week_start_of(db.local_date_of(checkin.scheduled_for))
    except Exception:
        return store.week_start_of(db.local_today())


async def checkin_answer(request: Request, checkin_id: int) -> Response:
    from assistant import store

    err, form = await _post_guard(request)
    if err is not None:
        return err
    chk = store.get_checkin(checkin_id)
    if chk is None:
        return _post_reply(request, False, "gone", status=404, anchor="needs")
    if chk.status != "pending":
        return _post_reply(request, False, "already_answered", status=409, anchor="needs")
    answer = str(form.get("answer") or "").strip()[:ANSWER_MAX_CHARS]
    if not answer:
        return _post_reply(request, False, "answer_empty", status=400, anchor="needs")
    parsed: dict[str, Any] = {"via": "dashboard"}
    logged = None
    if "hour" in chk.kind.lower():
        m = _HOURS.search(answer)
        jobs = store.list_jobs()
        if m is None or not 0 <= float(m.group(1)) <= 168:
            return _post_reply(request, False, "hours_bad", status=400, anchor="needs")
        hours = float(m.group(1))
        parsed["hours"] = hours
        if len(jobs) == 1:
            store.log_hours(jobs[0].id, hours, week_start=_week_for(chk), source="dashboard", note="check-in answer")
            parsed["job_id"] = jobs[0].id
            logged = {"job": jobs[0].name, "hours": hours, "pay": round(hours * jobs[0].hourly_rate, 2)}
    store.answer_checkin(checkin_id, answer, parsed)
    db.log_event("dashboard", f"check-in {checkin_id} answered on the dashboard", data={"kind": chk.kind})
    return _post_reply(request, True, "answered", anchor="needs", data={"logged": logged})


async def meal_feedback(request: Request, meal_id: int, action: str) -> Response:
    from assistant import store

    if action not in ("accept", "decline"):
        return Response("not found", status_code=404, media_type="text/plain")
    err, form = await _post_guard(request)
    if err is not None:
        return err
    meal = store.get_meal(meal_id)
    if meal is None:
        return _post_reply(request, False, "gone", status=404, anchor="today")
    status = "accepted" if action == "accept" else "declined"
    note = str(form.get("feedback") or "").strip()[:ANSWER_MAX_CHARS] or None
    store.update_meal(meal_id, status=status, feedback=note)
    return _post_reply(request, True, f"meal_{status}", anchor="today", data={"status": status})


async def job_post(request: Request) -> Response:
    from assistant import store

    err, form = await _post_guard(request)
    if err is not None:
        return err
    name = " ".join(str(form.get("name") or "").split())[:80]
    try:
        rate = float(str(form.get("rate") or "").replace("$", "").replace(",", "").strip())
    except ValueError:
        rate = -1.0
    if not name or not 0 < rate <= 10_000:
        return _post_reply(request, False, "job_bad", status=400, anchor="money")
    jid = store.upsert_job(name, hourly_rate=rate)
    db.log_event("dashboard", "job pay rate set on the dashboard", data={"job_id": jid})
    return _post_reply(request, True, "job_saved", anchor="money", data={"job_id": jid})


def memory_page(request: Request) -> Response:
    from assistant import store, vault

    d = _d()
    resp, _ = d._guard(request)
    if resp is not None:
        return resp
    q = (request.query_params.get("q") or "").strip()[:200]
    rel = request.query_params.get("note")
    note = None
    status = 200
    error = None
    if rel:
        try:
            _, rel_norm = vault.safe_path(rel, must_exist=True)
            n = vault.read_note(rel_norm)
            note = {"path": n.path, "title": n.title, "meta": n.meta, "html": render_markdown(n.body),
                    "backlinks": vault.backlinks(n.path), "folder": str(PurePosixPath(n.path).parent)}
        except vault.VaultError:
            status, error = 404, "There is no such note in the vault."
    hits = []
    if q:
        try:
            hits = vault.search(q, limit=20)
        except Exception as exc:
            log.warning("memory search failed: %s", exc)
            error = "Search is unavailable just now."
    notes = vault.list_notes() if vault.vault_root().is_dir() else []
    tree: dict[str, list[str]] = {}
    for p in notes:
        folder = str(PurePosixPath(p).parent)
        tree.setdefault("" if folder == "." else folder, []).append(p)
    lessons = store.list_lessons(limit=30)
    a = assistant_state()
    return d._render(request, "memory.html", {"note": note, "q": q, "hits": hits, "tree": sorted(tree.items()),
                                              "count": len(notes), "lessons": lessons, "error": error,
                                              "a": a, "glyph": glyph, "note_href": note_href},
                     status_code=status, nav="memory")


def agent_page(request: Request, name: str) -> Response:
    d = _d()
    resp, _ = d._guard(request)
    if resp is not None:
        return resp
    roster = roster_data(db.local_today())
    agent = next((r for r in roster if r["name"] == name), None)
    if agent is None:
        return d._not_found(request, "That agent")
    from assistant import store

    agent["runs"] = store.list_agent_runs(25, agent=name)
    return d._render(request, "agent.html", {"ag": agent, "glyph": glyph, "a": assistant_state()}, nav="home")


def register(router: Any) -> None:
    """Attach the home routes to the dashboard router (called once by ``dashboard``)."""
    router.add_api_route("/", home_page, methods=["GET"], include_in_schema=False)
    router.add_api_route("", home_page, methods=["GET"], include_in_schema=False)
    router.add_api_route("/api/home", api_home, methods=["GET"])
    router.add_api_route("/memory", memory_page, methods=["GET"])
    router.add_api_route("/agents/{name}", agent_page, methods=["GET"])
    router.add_api_route("/chat", chat_post, methods=["POST"])
    router.add_api_route("/checkins/{checkin_id}/answer", checkin_answer, methods=["POST"])
    router.add_api_route("/meals/{meal_id}/{action}", meal_feedback, methods=["POST"])
    router.add_api_route("/jobs", job_post, methods=["POST"])


__all__ = ["register", "render_markdown", "sprite_svg", "glyph", "home_data", "greeting", "streak_of",
           "rate_limited", "CHAT_MAX_CHARS"]
