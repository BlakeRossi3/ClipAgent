"""Plain-code facts about right now, for the heartbeat and for each spoken turn.

* :func:`status_snapshot` - the full picture (tasks, approvals, today's check-ins, money, Shorts,
  plan usage, heartbeat, recent events). The heartbeat model reads it as untrusted data.
* :func:`turn_context` - the few lines a spoken turn gets: local time, what is running in the
  background, approvals waiting, and a check-in only when it is due within
  :data:`CHECKIN_WINDOW_MIN` minutes (Blake: "it nagged an unrelated check-in twice"). The warm
  session already remembers the conversation, so nothing else is repeated per turn.

Every part is optional: a failing source is skipped, never fatal.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Callable

import db
from assistant import store, tools
from assistant.settings import get_assistant_settings

log = logging.getLogger(__name__)

CHECKIN_WINDOW_MIN = 5


def ago(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        mins = int((db.utcnow() - db.parse_iso(iso)).total_seconds() // 60)
    except Exception:
        return ""
    return "just now" if mins < 1 else (f"{mins} min ago" if mins < 90 else f"{mins // 60} h ago")


def local_hm(iso: str | None) -> str:
    try:
        return db.parse_iso(iso).astimezone(db.local_tz()).strftime("%H:%M") if iso else ""
    except Exception:
        return ""


def _parts(fns: list[Callable[[], list[str]]]) -> list[str]:
    out: list[str] = []
    for fn in fns:
        try:
            out.extend(fn())
        except Exception as exc:
            log.debug("status part skipped: %s", exc)
    return out


def _tasks() -> list[str]:
    from assistant import tasks

    out = []
    for t in tasks.list_tasks():
        if t["status"] == "running":
            out.append(f"Background task running (started {ago(t['started'])}): {t['task']}")
        elif t.get("finished") and db.parse_iso(t["finished"]) > db.utcnow() - timedelta(hours=12):
            out.append(f"Background task finished {ago(t['finished'])} ({t['status']}): {t['task']}"
                       + (f" -> {t['summary']}" if t.get("summary") else ""))
    return out or ["Background tasks: none running"]


def _approvals() -> list[str]:
    pend = db.list_decisions("pending", limit=5)
    if not pend:
        return ["Approvals waiting: none"]
    return [f"Approvals waiting: {len(pend)}"] + [f"- #{d.id} {d.kind}: {d.summary[:120]}" for d in pend[:3]]


def status_snapshot() -> str:
    """Compact facts about right now (the heartbeat's input)."""
    s = get_assistant_settings()
    lines = [f"Local time: {db.local_now().strftime('%A %d %B, %H:%M')}"]

    def checkins() -> list[str]:
        today = db.local_today()
        rows = [c for c in store.list_checkins(None, limit=200) if db.local_date_of(c.scheduled_for) == today]
        return [f"Check-in today {local_hm(c.scheduled_for)} ({c.status}): {c.question[:100]}" for c in rows[:4]]

    def money() -> list[str]:
        wk = store.week_start_of()
        total = store.income_total(wk, db.local_today())
        hours = sum(w.hours for w in store.list_work_log(since_week=wk))
        return [f"This week (from Monday {wk.isoformat()}): income logged ${total:,.2f}, hours logged {hours:g}"]

    def shorts() -> list[str]:
        snap = tools.studio_snapshot(7)
        top = snap["top_clips"][0] if snap["top_clips"] else None
        line = (f"Shorts: {snap['uploads_today']} uploaded today (cap {snap['upload_cap']}), "
                f"{snap['period_views']} views in 7 days over {snap['period_clips_tracked']} clips")
        if top:
            line += f"; best: {str(top['title'])[:60]!r} with {top['views']} views"
        if snap.get("pipeline_paused"):
            line += "; pipeline spending paused"
        return [line]

    def plan() -> list[str]:
        from assistant import usage

        u = usage.usage_status()
        bits = [f"{k} window {w['left_pct']}% left" for k, w in u["windows"].items() if w.get("left_pct") is not None]
        if u["limited"]:
            bits.append(f"plan limited until {u['limited_text']}")
        bits.append(f"{u['runs_today']} of {u['runs_cap']} Claude runs used today")
        bits.append(f"assistant API spend today ${store.assistant_spent_today():.2f} of ${s.daily_budget_usd:.2f}")
        return ["Usage: " + "; ".join(bits)]

    def events() -> list[str]:
        evs = db.list_events(limit=4)
        return ["Recent events:"] + [f"- {local_hm(e.ts)} {e.module} ({e.level}): {e.message[:90]}" for e in evs] \
            if evs else []

    def heartbeat() -> list[str]:
        from assistant import heartbeat as hb

        line = hb.status_line()
        return [line] if line else []

    lines += _parts([_tasks, _approvals, checkins, money, shorts, plan, heartbeat, events])
    return "\n".join(lines)


def due_checkins(window_min: int = CHECKIN_WINDOW_MIN) -> list[str]:
    """Check-ins that are open (sent, unanswered) or due within ``window_min`` minutes."""
    now = db.utcnow()
    out = []
    chk = store.open_checkin()
    if chk is not None:
        out.append(f"Waiting for his answer to check-in #{chk.id}: {chk.question[:140]}")
    for c in store.list_checkins("pending", limit=50):
        try:
            when = db.parse_iso(c.scheduled_for)
        except Exception:
            continue
        if now - timedelta(minutes=1) <= when <= now + timedelta(minutes=window_min) and (chk is None or c.id != chk.id):
            out.append(f"Check-in due at {local_hm(c.scheduled_for)}: {c.question[:140]}")
    return out


def turn_context() -> str:
    """The short per-turn context for a spoken turn (no unrelated check-ins, no event log)."""
    lines = [f"Local time: {db.local_now().strftime('%A %d %B %Y, %H:%M')}"]

    def running() -> list[str]:
        from assistant import tasks

        return [f"Background task running (started {ago(t['started'])}): {t['task'][:160]}"
                for t in tasks.running_tasks()]

    def approvals() -> list[str]:
        n = len(db.list_decisions("pending", limit=20))
        return [f"Approvals waiting for him: {n}"] if n else []

    lines += _parts([running, approvals, due_checkins])
    return "\n".join(lines)
