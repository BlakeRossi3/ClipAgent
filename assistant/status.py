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
import re
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
    except Exception as exc:
        log.debug("ignored error (best effort): %r", exc)
        return ""
    return "just now" if mins < 1 else (f"{mins} min ago" if mins < 90 else f"{mins // 60} h ago")


def local_hm(iso: str | None) -> str:
    try:
        return db.parse_iso(iso).astimezone(db.local_tz()).strftime("%H:%M") if iso else ""
    except Exception as exc:
        log.debug("ignored error (best effort): %r", exc)
        return ""


def plan_line() -> str:
    """The Claude plan as he thinks of it: the 5-hour and weekly windows (the same two meters as the home
    tile, ``home_live.plan_meters``), plus a hit limit. No daily run count (he asked for it gone)."""
    import home_live

    p = home_live.plan_meters()
    bits = []
    for m in p["meters"]:
        if m["left_pct"] is not None:
            fig = f"{m['left_pct']}% left" + (f", resets {m['resets_text']}" if m["resets_text"] else "")
        else:
            fig = m["note"] or "unknown"
        bits.append(f"{m['label'].lower()} {fig}")
    if p["limited"]:
        bits.append(f"plan limited until {p['limited_text']}")
    return "Claude plan " + "; ".join(bits)


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
        return work_lines()

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
        return ["Usage: " + plan_line()
                + f"; assistant API spend today ${store.assistant_spent_today():.2f} of ${s.daily_budget_usd:.2f}"]

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


#: A spoken quick-status or money question ("what's going on today?", "how much did I make this week?").
#: Only these get :func:`glance_lines` in their context, so the answer needs no tool round.
GLANCE_RE = re.compile(
    r"\b(going on|what'?s up|what'?s new|status|update|today|this week|last week|make|made|earn\w*|money|"
    r"pay|paid|income|hours|worked|uploads?|shorts|views|channel|approvals?|pending|how are things|anything new|"
    r"needs?|waiting|failures?|failed)\b",
    re.I)


def wants_glance(heard: str) -> bool:
    return bool(GLANCE_RE.search(heard or ""))


def glance_lines() -> list[str]:
    """The few facts a quick status or money question needs, so the model answers from the context
    instead of calling studio_status / ledger_summary first. Measured (docs/VOICE_SPEED_RESEARCH.md
    section 6): that one tool round cost about 2 s of a 3.2-3.4 s answer."""
    def shorts() -> list[str]:
        snap = tools.studio_snapshot(7)
        line = (f"Shorts: {snap['uploads_today']} of {snap['upload_cap']} uploaded today, "
                f"{snap['period_views']} views in 7 days")
        failed = int((snap.get("sources_by_status") or {}).get("failed") or 0)
        if failed:
            line += f", {failed} source(s) failed"
        if snap.get("pipeline_paused"):
            line += ", pipeline spending paused"
        out = [line]
        out += [f"Approval #{d['id']} waiting ({d['kind']}): {str(d['summary'])[:100]}"
                for d in snap["pending_approvals"][:2]]
        return out

    def money() -> list[str]:
        return work_lines()[:1]

    def needs_now() -> list[str]:
        from assistant import needs

        its = needs.items()
        if not its:
            return ["Needs him: nothing (the badge shows none)."]
        return [f"Needs him ({len(its)}, the same list and count as his 'need you' badge):",
                *[f"  {i}. {it['text'][:200]} -> {it['how']}" for i, it in enumerate(its, 1)]]

    lines = _parts([needs_now, shorts, money])
    return ["At a glance (use only for what he asked; details: studio_status, ledger_summary):", *lines] \
        if lines else []


def work_lines() -> list[str]:
    """This week's and last week's hours, gross pay and take-home (pay = hours x rate, recomputed on
    read, so weeks logged before the rate was known are not read as zero), plus other income."""
    from assistant import ledger

    ledger.sync_job_income()
    today = db.local_today()
    wk = store.week_start_of(today)
    lw = wk - timedelta(days=7)
    out = []
    for label, start, end in (("This week", wk, today), ("Last week", lw, lw + timedelta(days=6))):
        p = ledger.period_totals(start, end)
        line = (f"{label} (from Monday {start.isoformat()}): {ledger.fmt_hours(p['hours'])} h worked, "
                f"{ledger.fmt_usd(p['job_gross'], whole=False)} gross pay, "
                f"{ledger.fmt_usd(p['job_net_estimate'], whole=False)} estimated take-home")
        if p["other_income"]:
            line += f", other income {ledger.fmt_usd(p['other_income'], whole=False)}"
        out.append(line)
    if not store.list_jobs():
        out.append("No job or pay rate set yet")
    else:
        out += [f"No pay rate set for {j.name}" for j in store.list_jobs() if j.hourly_rate <= 0]
    return out


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
        except Exception as exc:
            log.debug("ignored error (best effort): %r", exc)
            continue
        if now - timedelta(minutes=1) <= when <= now + timedelta(minutes=window_min) and (chk is None or c.id != chk.id):
            out.append(f"Check-in due at {local_hm(c.scheduled_for)}: {c.question[:140]}")
    return out


def turn_context() -> str:
    """The short per-turn context for a spoken turn (no unrelated check-ins, no event log)."""
    lines = [f"Local time: {db.local_now().strftime('%A %d %B %Y, %H:%M')}"]

    def running() -> list[str]:
        from assistant import tasks

        out = [f"Background task running (started {ago(t['started'])}): {t['task'][:160]}"
               for t in tasks.running_tasks()]
        out += [f"Background task waiting to retry (attempt {t.get('attempts', 1)} did not finish: "
                f"{t.get('last_error') or 'unknown'}): {t['task'][:160]}"
                for t in tasks.list_tasks() if t.get("status") == "retrying"]
        return out

    def approvals() -> list[str]:
        from assistant import needs        # the pill under the orb (approvals, questions, failures)

        return needs.pill_lines()

    def on_screen() -> list[str]:
        from assistant import screen

        line = screen.summary_line()
        return [line] if line else []

    lines += _parts([running, approvals, due_checkins, on_screen])
    return "\n".join(lines)
