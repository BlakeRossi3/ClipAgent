"""Everything planned, in one place: Cecil's own scheduled tasks, check-in questions, recurring jobs,
the usage-handoff resume and the morning surprise.

Cecil schedules work for later with the ``schedule_prompt`` tool (or the owner on the Scheduled page):
a row in ``as_deferred`` (assistant/store.py) with ``kind`` ``task`` (or ``handoff_resume`` /
``surprise``), a time (absolute local, "in 20 minutes", "tomorrow 7am", or "after the plan resets"),
an optional recurrence (``daily`` / ``weekdays`` / ``weekly``), a title, and who scheduled it and why
(``meta``). When the time comes :func:`assistant.usage.run_due` (every 10 minutes) claims the row and
:func:`start_row` runs it exactly like the brain's ``start_task``: a background worker session
(``assistant/tasks.py``) that does real work, streams its steps to the Jarvis page and reports back
(History + one spoken line). A recurring row books its next occurrence when it finishes.

:func:`snapshot` merges every source for the dashboard's Scheduled page (``/dashboard/scheduled``), its
JSON twin and the ``schedule_list`` tool:

* ``running``  - scheduled rows running now (+ other background tasks)
* ``upcoming`` - time-ordered: unsent check-in questions, queued rows, the next run of each daily/weekly job
* ``routine``  - jobs that repeat every few minutes (one line each)
* ``recent``   - finished rows with their outcome
* ``handoff`` / ``surprise`` - the pending usage-handoff resume and the latest morning surprise
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

import db
from assistant import store

log = logging.getLogger(__name__)

RECURS = ("daily", "weekdays", "weekly")
TASK_KINDS = ("task", "handoff_resume", "surprise")   # run through the background-task path
MAX_RUNNING = 2                                        # scheduled background tasks at once
STALE_AFTER = timedelta(minutes=20)                    # beyond the task timeout: a lost run (app restarted)
UPCOMING_DAYS = 7
KIND_LABELS = {"task": "Task", "handoff_resume": "Resume after usage reset", "surprise": "Morning surprise",
               "checkin": "Question for you", "job": "Routine", "prompt": "Task", "owner_request": "Your request",
               "improve": "Self-improvement review"}
#: Plain words for the scheduler's jobs (main.job_specs); unknown ids fall back to their description.
JOB_LABELS = {
    "scout": "Look for new videos to clip", "process": "Download, transcribe, pick and render clips",
    "upload": "Upload finished Shorts", "send_queued": "Send queued texts and calls",
    "expire_decisions": "Expire old approval requests", "analytics": "Pull YouTube stats",
    "cleanup": "Clear out old video files", "daily_summary": "Evening summary call",
    "weekly_review": "Weekly Shorts strategy review", "assistant_plan": "Plan the day's questions for you",
    "assistant_checkins": "Ask questions that are due", "assistant_tidy": "Tidy the memory vault",
    "assistant_improve": "Weekly self-improvement ideas", "assistant_deferred": "Run scheduled tasks that are due",
    "assistant_evals": "Nightly self-tests", "assistant_history": "Distil imported Claude chats",
    "assistant_reels_cleanup": "Delete old downloaded reels", "assistant_heartbeat": "Heartbeat check",
    "assistant_morning_surprise": "Make you something cool",
    "assistant_initiative": "Build things from the backlog while you're away",
    "assistant_initiative_harvest": "Turn what you said into backlog items",
}

_IN_RE = re.compile(r"^in\s+(\d+(?:\.\d+)?)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$")
_CLOCK_RE = re.compile(r"^(?:(today|tonight|tomorrow)\s+)?(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$")


class ScheduleError(ValueError):
    """Bad input for a scheduled task (shown to Cecil / the owner as is)."""


# ---------------------------------------------------------------------------
# times
# ---------------------------------------------------------------------------

def parse_when(text: str | None, *, now: datetime | None = None) -> datetime | None:
    """When to run, as aware UTC. ``None`` = just after the Claude plan's usage window resets.

    Accepts: "after_reset", "now", "in 20 minutes" / "in 2 hours" / "in 1 day", "7:10am", "19:30",
    "tomorrow 9am", "tonight 8pm", "YYYY-MM-DD HH:MM" (local) or ISO-8601. Raises :class:`ScheduleError`.
    """
    from assistant import usage

    now = now or db.utcnow()
    txt = " ".join(str(text or "").strip().lower().split())
    if txt in ("", "after_reset", "after reset", "reset", "after the reset"):
        return None
    if txt in ("now", "asap", "right now"):
        return now
    m = _IN_RE.match(txt)
    if m:
        n, unit = float(m.group(1)), m.group(2)
        delta = timedelta(days=n) if unit.startswith("d") else timedelta(hours=n) if unit.startswith("h") \
            else timedelta(minutes=n)
        if delta <= timedelta(0) or delta > timedelta(days=60):
            raise ScheduleError("pick a delay between 1 minute and 60 days")
        return now + delta
    m = _CLOCK_RE.match(txt)
    if m:
        day_word, h, mi, ampm = m.group(1), int(m.group(2)), int(m.group(3) or 0), m.group(4)
        if ampm:
            if not 1 <= h <= 12:
                raise ScheduleError("hour must be 1-12 with am/pm")
            h = h % 12 + (12 if ampm == "pm" else 0)
        if not (0 <= h < 24 and 0 <= mi < 60):
            raise ScheduleError("that is not a time of day")
        loc_now = now.astimezone(db.local_tz())
        day = loc_now.date() + timedelta(days=1 if day_word == "tomorrow" else 0)
        at = datetime(day.year, day.month, day.day, h, mi, tzinfo=db.local_tz())
        if at <= loc_now and day_word is None:   # "7am" said in the evening = tomorrow
            at += timedelta(days=1)
        return at.astimezone(UTC)
    try:
        return usage.parse_at(str(text).strip())
    except ValueError:
        raise ScheduleError("time must be 'after_reset', 'in 30 minutes', '7:10am', 'tomorrow 9am', "
                            "'YYYY-MM-DD HH:MM' (local) or ISO-8601") from None


def next_occurrence(prev: datetime, recur: str, *, after: datetime | None = None) -> datetime:
    """The next local time-of-day of ``prev`` that matches ``recur`` and is strictly after ``after``."""
    if recur not in RECURS:
        raise ScheduleError(f"repeat must be one of {', '.join(RECURS)}")
    after = after or db.utcnow()
    loc = prev.astimezone(db.local_tz())
    step = 7 if recur == "weekly" else 1
    day = loc.date()
    for _ in range(400):
        day += timedelta(days=step)
        cand = datetime(day.year, day.month, day.day, loc.hour, loc.minute, tzinfo=db.local_tz())
        if recur == "weekdays" and cand.weekday() >= 5:
            continue
        if cand.astimezone(UTC) > after:
            return cand.astimezone(UTC)
    raise ScheduleError("no next occurrence")   # pragma: no cover


def _hm(loc: datetime) -> str:
    return loc.strftime("%I").lstrip("0") + (f":{loc.minute:02d}" if loc.minute else "") + loc.strftime("%p").lower()


def when_words(dt: datetime | None, *, now: datetime | None = None) -> str:
    """'in 12 min', 'today 7:10pm', 'tomorrow 7am', 'Wed 9:30am', 'Nov 30, 7am'; past: '5 min ago'..."""
    if dt is None:
        return "after the plan resets"
    now = now or db.utcnow()
    secs = (dt - now).total_seconds()
    loc, loc_now = dt.astimezone(db.local_tz()), now.astimezone(db.local_tz())
    if 0 <= secs < 3600:
        return "in under a minute" if secs < 60 else f"in {int(secs // 60)} min"
    if -3600 < secs < 0:
        return "just now" if secs > -60 else f"{int(-secs // 60)} min ago"
    days = (loc.date() - loc_now.date()).days
    if days == 0:
        return f"today {_hm(loc)}"
    if days == 1:
        return f"tomorrow {_hm(loc)}"
    if days == -1:
        return f"yesterday {_hm(loc)}"
    if -6 <= days <= 6:
        return f"{loc.strftime('%a')} {_hm(loc)}"
    return f"{loc.strftime('%b')} {loc.day}, {_hm(loc)}"


def _reset_eta() -> datetime | None:
    from assistant import usage

    r = usage.next_reset()
    return r + usage.RESET_GRACE if r else None


# ---------------------------------------------------------------------------
# scheduling
# ---------------------------------------------------------------------------

def schedule(prompt: str, *, when: datetime | None, recur: str | None = None, title: str | None = None,
             by: str = "Cecil", why: str = "", channel: str = "dashboard", kind: str = "task",
             agent: str = "main", meta: dict[str, Any] | None = None) -> int:
    """Queue ``prompt`` to run at ``when`` (aware; ``None`` = after the plan resets). Returns the row id."""
    prompt = (prompt or "").strip()
    if not prompt or len(prompt) > 8000:
        raise ScheduleError("the task text must be 1-8000 characters")
    recur = (recur or "").strip().lower() or None
    if recur is not None and recur not in RECURS:
        raise ScheduleError(f"repeat must be one of {', '.join(RECURS)}")
    if recur and when is None:
        raise ScheduleError("a repeating task needs a time of day")
    title = " ".join((title or prompt).split())[:90]
    info = {"by": by[:60], "why": " ".join(why.split())[:300], **(meta or {})}
    did = store.add_deferred(prompt, kind=kind, channel=channel if channel in ("sms", "dashboard") else "dashboard",
                             agent=agent, run_after=db.to_iso(when) if when else None, after_reset=when is None,
                             reason=f"scheduled by {by}"[:200], recur=recur, title=title, meta=info)
    db.log_event("assistant", f"scheduled {kind} #{did}: {title[:80]}",
                 data={"when": db.to_iso(when) if when else "after_reset", "recur": recur, "by": by})
    return did


def cancel(did: int, *, via: str = "dashboard") -> bool:
    d = store.get_deferred(int(did))
    if d is None or not store.cancel_deferred(d.id):
        return False
    db.log_event("assistant", f"scheduled {d.kind} #{d.id} cancelled", data={"via": via, "title": d.title})
    return True


def run_now(did: int, *, via: str = "dashboard") -> bool:
    """Run a queued row at once (in a background thread). False when it is not queued any more."""
    from assistant import usage

    d = store.get_deferred(int(did))
    if d is None or d.status != "queued":
        return False
    store.update_deferred(d.id, run_after=db.now_iso(), after_reset=False)
    db.log_event("assistant", f"scheduled {d.kind} #{d.id} started by hand", data={"via": via})
    d = store.get_deferred(d.id)
    if d is None or not store.set_deferred_status(d.id, "running"):
        return False
    threading.Thread(target=usage.run_claimed, args=(d,), name=f"sched-{d.id}", daemon=True).start()
    return True


def can_start_task() -> bool:
    from assistant import tasks

    return sum(1 for t in tasks.running_tasks() if (t.get("meta") or {}).get("scheduled_id")) < MAX_RUNNING


def brief_for(d: store.Deferred) -> str:
    meta = d.meta if isinstance(d.meta, dict) else {}
    if d.kind == "handoff_resume":
        from assistant import handoff

        return handoff.resume_brief(d)
    if d.kind == "surprise":
        return d.prompt
    who = meta.get("by") or "you"
    why = f" Why: {meta['why']}." if meta.get("why") else ""
    return (f"{d.prompt}\n\n<scheduled_task id=\"{d.id}\">\nThis was scheduled by {who} on {d.created_at[:16]}Z"
            f"{' (repeats ' + d.recur + ')' if d.recur else ''}.{why} It runs in the background now. Do the work "
            "fully, then finish with a short plain answer: what you did or found, key facts first. It will be told "
            "to him.\n</scheduled_task>")


def start_row(d: store.Deferred) -> str:
    """Run a claimed (``running``) task-kind row as a background task. Returns the task id."""
    from assistant import tasks
    from assistant.settings import get_assistant_settings

    s = get_assistant_settings()
    meta = d.meta if isinstance(d.meta, dict) else {}
    cheap = d.kind == "surprise"
    tid = tasks.start(d.title or d.prompt[:120], heard="", origin="any", brief=brief_for(d),
                      model=s.cc_subagent_model if cheap else None, effort="medium" if cheap else None,
                      announce=d.kind != "surprise",
                      meta={"scheduled_id": d.id, "kind": d.kind},
                      on_finish=lambda row, full, status, did=d.id: finished(did, row, full, status))
    store.update_deferred(d.id, meta={**meta, "task_id": tid})
    return tid


def finished(did: int, row: dict[str, Any], full: str, status: str) -> None:
    """A scheduled background task ended: record the outcome, text it if asked, book the next repeat."""
    d = store.get_deferred(did)
    if d is None:
        return
    ok = status == "ok"
    result = (row.get("summary") or full or "")[:500]
    if status == "budget":
        result = "Stopped by the plan limit; handed over to the next usage window. " + result
    store.set_deferred_status(did, "done" if ok else "failed", result=result[:500])
    if ok and d.channel == "sms":
        try:
            from assistant import tools

            tools.send_to_owner(f"{d.title or 'Scheduled task'}: {row.get('summary') or full}"[:600],
                                kind="scheduled", agent=d.agent)
        except Exception as exc:
            log.info("scheduled result not texted: %s", exc)
    if d.kind == "surprise":
        from assistant import surprise

        surprise.on_finished(d, row, full, status)
    book_next(d)


def book_next(d: store.Deferred) -> int | None:
    """Queue the next occurrence of a repeating row (once per row)."""
    if not d.recur or not d.run_after:
        return None
    if not db.kv_set_if_absent(f"assistant:schedule:next_booked:{d.id}", db.now_iso()):
        return None
    nxt = next_occurrence(db.parse_iso(d.run_after), d.recur)
    meta = {k: v for k, v in (d.meta if isinstance(d.meta, dict) else {}).items() if k != "task_id"}
    nid = store.add_deferred(d.prompt, kind=d.kind, channel=d.channel, agent=d.agent, run_after=db.to_iso(nxt),
                             reason=d.reason, recur=d.recur, title=d.title, meta={**meta, "previous": d.id})
    return nid


def sweep_stale(now: datetime | None = None) -> int:
    """Rows stuck in ``running`` (the app restarted mid-task) -> failed, so they stop blocking."""
    from assistant import tasks
    from assistant.settings import get_assistant_settings

    now = now or db.utcnow()
    limit = timedelta(seconds=get_assistant_settings().cc_task_timeout_sec) + STALE_AFTER
    live = {t["id"] for t in tasks.running_tasks()}
    n = 0
    for d in store.list_deferred("running"):
        tid = (d.meta or {}).get("task_id") if isinstance(d.meta, dict) else None
        if d.started_at and now - db.parse_iso(d.started_at) > limit and tid not in live:
            store.set_deferred_status(d.id, "failed", result="Interrupted (the app restarted while it ran).")
            book_next(d)
            n += 1
    return n


# ---------------------------------------------------------------------------
# the page / tool data
# ---------------------------------------------------------------------------

def _row_item(d: store.Deferred, now: datetime, reset_eta: datetime | None) -> dict[str, Any]:
    meta = d.meta if isinstance(d.meta, dict) else {}
    at = db.parse_iso(d.run_after) if d.run_after else None
    when = at or (reset_eta if d.after_reset else None)
    words = when_words(at, now=now) if at else ("after the plan resets" + (f" (~{when_words(reset_eta, now=now)})"
                                                                          if reset_eta else ""))
    by = meta.get("by") or (d.reason or "").removeprefix("scheduled by ").strip() or "Cecil"
    return {"id": d.id, "source": "scheduled", "kind": d.kind, "kind_label": KIND_LABELS.get(d.kind, d.kind.title()),
            "at": db.to_iso(when) if when else None, "when": words, "title": d.title or d.prompt[:90],
            "detail": d.prompt[:400], "by": by, "why": meta.get("why") or "", "recur": d.recur or "",
            "channel": d.channel, "status": d.status, "actions": d.status == "queued",
            "notes": meta.get("notes") or []}


def _job_items(now: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(timeline items: next run of each cron job within the window, routine: interval jobs)."""
    try:
        import main
        from apscheduler.triggers.cron import CronTrigger

        specs = main.job_specs()
    except Exception as exc:   # the page still renders without the job list
        log.warning("job list unavailable: %s", exc)
        return [], []
    tz = db.local_tz()
    horizon = now + timedelta(days=UPCOMING_DAYS)
    timeline, routine = [], []
    for spec in specs:
        last = db.kv_get(f"main:job:{spec.id}")
        last_txt = ""
        if isinstance(last, dict) and last.get("started_at"):
            outcome = "skipped (busy)" if last.get("skipped") else "ok" if last.get("ok") else "failed"
            last_txt = f"last ran {when_words(db.parse_iso(last['started_at']), now=now)}, {outcome}"
        label = JOB_LABELS.get(spec.id, spec.description)
        if spec.trigger == "cron":
            try:
                nxt = CronTrigger(timezone=tz, **spec.args).get_next_fire_time(None, now.astimezone(tz))
            except Exception:
                continue
            if nxt is None or nxt.astimezone(UTC) > horizon:
                continue
            kind = "surprise" if spec.id == "assistant_morning_surprise" else "job"
            timeline.append({"id": spec.id, "source": "job", "kind": kind, "kind_label": KIND_LABELS[kind],
                             "at": db.to_iso(nxt.astimezone(UTC)), "when": when_words(nxt.astimezone(UTC), now=now),
                             "title": label, "detail": last_txt, "by": "ClipAgent", "why": spec.description,
                             "recur": _cron_words(spec.args), "actions": False})
        elif spec.trigger == "interval":
            mins = int(spec.args.get("minutes") or 0) + 60 * int(spec.args.get("hours") or 0)
            routine.append({"id": spec.id, "title": label, "every": f"every {mins} min" if mins < 60
                            else f"every {mins // 60} h", "detail": last_txt})
    return timeline, routine


def _cron_words(args: dict[str, Any]) -> str:
    if "day_of_week" in args:
        return f"weekly ({args['day_of_week']})"
    if "hour" in args:
        return "daily"
    return "hourly"


def snapshot(now: datetime | None = None) -> dict[str, Any]:
    """Everything planned and recently done (see the module docstring)."""
    from assistant import handoff, initiative, surprise, tasks

    now = now or db.utcnow()
    reset_eta = _reset_eta()
    upcoming: list[dict[str, Any]] = []
    for c in store.list_checkins("pending", limit=200):
        if c.sent_at is None:
            at = db.parse_iso(c.scheduled_for)
            upcoming.append({"id": c.id, "source": "checkin", "kind": "checkin", "kind_label": KIND_LABELS["checkin"],
                             "at": c.scheduled_for, "when": when_words(at, now=now) if at > now else "due now",
                             "title": c.question, "detail": "", "by": "Cecil (check-in plan)",
                             "why": c.kind.replace("_", " ").replace(":", ": "), "recur": "", "actions": False})
    queued = [_row_item(d, now, reset_eta) for d in store.list_deferred("queued", limit=200)]
    jobs, routine = _job_items(now)
    upcoming += queued + jobs
    upcoming.sort(key=lambda x: (x["at"] or "9999", x["title"]))
    running = [_row_item(d, now, reset_eta) for d in store.list_deferred("running", limit=50)]
    others = [{"task": t["task"], "started": when_words(db.parse_iso(t["started"]), now=now),
               "step": ((t.get("steps") or [{}])[-1]).get("label", "")}
              for t in tasks.running_tasks() if not (t.get("meta") or {}).get("scheduled_id")]
    recent = []
    for d in store.list_deferred_finished(15):
        it = _row_item(d, now, reset_eta)
        fin = db.parse_iso(d.finished_at) if d.finished_at else None
        it.update(when=when_words(fin, now=now) if fin else "", at=d.finished_at, result=d.result or "",
                  outcome={"done": "done", "failed": "failed", "cancelled": "cancelled"}.get(d.status, d.status))
        recent.append(it)
    pend = handoff.pending_resume()
    return {"now": db.to_iso(now), "upcoming": upcoming, "running": running, "other_tasks": others,
            "routine": routine, "recent": recent,
            "handoff": _row_item(pend, now, reset_eta) if pend else None,
            "surprise": surprise.latest(), "surprise_time": surprise.configured_time(),
            "ideas": _ideas(),
            "initiative": initiative.schedule_summary(), "usage": _usage_line()}


def _ideas() -> list[dict[str, Any]]:
    try:
        from assistant import ideas

        return ideas.list_ideas(10)
    except Exception as exc:   # the page still loads
        log.warning("ideas unavailable: %s", exc)
        return []


def _usage_line() -> str:
    try:
        from assistant import usage

        tight, until = usage.constrained()
        st = usage.usage_status()
        left = [w["left_pct"] for w in st["windows"].values() if w.get("left_pct") is not None]
        if st["limited"]:
            return f"Claude plan limit reached; it resets {st['limited_text']}."
        if left:
            return (f"Claude plan: {min(left)}% left in the tightest window"
                    + (f", resets {when_words(until)}" if tight and until else "") + ".")
        return "Claude plan usage: not reported yet."
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

def _tool_schedule(ctx: Any, a: dict[str, Any]) -> Any:
    from assistant.tools import ToolError

    try:
        when = parse_when(a.get("at"))
        if when is not None and when < db.utcnow() - timedelta(minutes=1):
            raise ScheduleError("that time is in the past")
        did = schedule(str(a.get("text") or ""), when=when, recur=a.get("repeat"), title=a.get("title"),
                       by=f"Cecil ({getattr(ctx, 'agent', 'main')})", why=str(a.get("why") or ""),
                       channel=str(a.get("channel") or "dashboard"))
    except ScheduleError as exc:
        raise ToolError(str(exc)) from None
    d = store.get_deferred(did)
    item = _row_item(d, db.utcnow(), _reset_eta()) if d else {}
    return {"id": did, "runs": item.get("when"), "repeat": a.get("repeat") or None,
            "note": "It runs as a background task and reports back. Listed on the Scheduled page; cancel with "
                    "schedule_cancel."}


def _tool_list(ctx: Any, a: dict[str, Any]) -> Any:
    snap = snapshot()
    keep = ("id", "source", "kind", "when", "title", "by", "why", "recur", "actions")
    return {"upcoming": [{k: x.get(k) for k in keep} for x in snap["upcoming"][:40]],
            "running": [{k: x.get(k) for k in keep} for x in snap["running"]],
            "other_background_tasks": snap["other_tasks"],
            "routine": snap["routine"],
            "recent": [{"id": x["id"], "title": x["title"], "when": x["when"], "outcome": x["outcome"],
                        "result": x["result"][:200]} for x in snap["recent"][:8]],
            "handoff_resume": snap["handoff"], "morning_surprise": snap["surprise"], "usage": snap["usage"],
            "note": "Only items with source 'scheduled' can be cancelled (schedule_cancel id)."}


def _tool_cancel(ctx: Any, a: dict[str, Any]) -> Any:
    from assistant.tools import ToolError

    try:
        did = int(a.get("id"))
    except (TypeError, ValueError):
        raise ToolError("id must be the number from schedule_list") from None
    if not cancel(did, via=f"tool:{getattr(ctx, 'agent', 'main')}"):
        raise ToolError(f"#{did} is not waiting to run (already ran, running, cancelled, or no such id)")
    return {"cancelled": did}


def register_tools(register: Callable[..., None]) -> None:
    from assistant.tools import Tool, _obj

    register(Tool("schedule_prompt", "Schedule a task for yourself to do later; it runs as a background task (real "
                  "work, full powers) and reports back. at: 'in 20 minutes', 'in 2 hours', '7:10am', 'tomorrow 9am', "
                  "'YYYY-MM-DD HH:MM' (local) or 'after_reset' (just after the Claude plan's usage window resets). "
                  "repeat: daily | weekdays | weekly (optional). text must be self-contained (every fact the later "
                  "run needs). title: a few words for the Scheduled page; why: one line. channel sms = also text "
                  "him the result.",
                  _obj({"text": {"type": "string"}, "at": {"type": "string"},
                        "repeat": {"type": "string", "enum": list(RECURS)}, "title": {"type": "string"},
                        "why": {"type": "string"}, "channel": {"type": "string", "enum": ["sms", "dashboard"]}},
                       ["text", "at"]),
                  _tool_schedule, owner="assistant.schedule", side_effects=True), replace=True)
    register(Tool("schedule_list", "Everything planned, in time order (the Scheduled page): your scheduled tasks "
                  "(with who scheduled them and why), questions you plan to ask him, routine jobs and their next "
                  "run, the usage-handoff resume, the morning surprise, and recently finished items with outcomes.",
                  _obj({}, []), _tool_list, owner="assistant.schedule"), replace=True)
    register(Tool("schedule_cancel", "Cancel one of your scheduled tasks before it runs (id from schedule_list).",
                  _obj({"id": {"type": "integer"}}, ["id"]), _tool_cancel, owner="assistant.schedule",
                  side_effects=True), replace=True)
