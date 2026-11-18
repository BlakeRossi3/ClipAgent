"""Background tasks: long work Cecil starts mid-conversation and reports back on, out loud, once.

"That'll take a few minutes, sir. I'll tell you when it's done." The warm brain calls the
``start_task`` tool (or the heartbeat calls :func:`start`); the work runs in a parallel worker
session (``brain.run_worker``: a separate Claude Code session with the same powers, so the
conversation stays responsive), its steps stream to the Jarvis page's activity rail, and when it
finishes:

1. the full result goes to History (dashboard message, kind ``voice_detail``);
2. :func:`announce` runs exactly once per task (a ``kv`` claim): the warm brain turns the result
   into one to three spoken sentences in the same conversation, so he can ask follow-ups; if the
   brain is not available, the first sentences of the result are used instead;
3. the spoken text is saved as a voice message of kind ``task_done`` (``meta.origin`` = who should
   speak it: "browser", "voice" or "any"). The page speaks it from the event stream; the laptop
   voice process from :class:`Announcer`.

Task rows live in ``kv`` (:data:`KV_TASKS`: every unfinished row plus the newest :data:`TASK_KEEP`
finished ones).

Nothing he asked for is left unfinished (``assistant.retry``): a conversation task (no ``on_finish``
owner, not a self-edit) whose run ends ``error`` (incl. a timeout), ``stopped`` or ``rounds`` is parked as
``retrying`` with a ``next_try`` (:func:`_schedule_retry`) and he hears once that it will be tried again;
:func:`retry_due` (job ``assistant_task_retry``, every 5 minutes) restarts due rows one at a time with a
``<retry>`` note in the brief, after first requeueing rows a restart cut off (:func:`_requeue_orphans`).
A plan limit (``budget``) is not retried here: ``assistant.handoff`` resumes that work after the reset.
Blockers he can fix are texted to him once.

Everything he asks for shows on the rail, however it arrived (Blake: "if I text it to take a look at this
reel, that should show up in the running tasks section"). Work that does not run as a background worker -
a text or dashboard message being answered, a scheduled staff job - is a *tracked* row (:func:`track`,
:func:`track_step`, :func:`track_done`): same list, same rail, cancellable (``on_cancel``), never retried
or announced (the answer goes back the way the request came). A tracked row a restart cut off is closed
as ``error`` by :func:`_requeue_orphans`.

A task can be given to one of the staff (``start(..., staff="researcher")``): the worker hands the work to
that specialist (Claude Code's Agent tool, recorded as their run by ``assistant.staff``) and the row shows
who has it (``meta.staff`` / ``meta.label``).
"""

from __future__ import annotations

import inspect
import logging
import threading
import time
import uuid
from datetime import timedelta
from typing import Any, Callable

import db
from assistant import events, persona, retry, store
from assistant.settings import get_assistant_settings

log = logging.getLogger(__name__)

KV_TASKS = "assistant:tasks"
KV_ANNOUNCED = "assistant:tasks:announced:{id}"
TASK_KEEP = 12
MAX_STEPS = 30
UNFINISHED = ("running", "retrying")   # never trimmed from the kv list
MAX_PARALLEL = 4                       # background workers at once (retries included); not one at a time
ORPHAN_GRACE_SEC = 600                 # a "running" row this long past the run timeout was cut off by a restart
RETRY_STATUSES = ("error", "stopped", "rounds")   # budget -> assistant.handoff instead
RESULT_CHARS = 4000   # full result kept on the row for the expanded rail view (History has all of it)
CONVO_WINDOW_MIN = 30   # how far back the lead-up conversation reaches (detail)
CONVO_KEEP = 10         # lines of lead-up shown

_lock = threading.Lock()
_threads: dict[str, threading.Thread] = {}
_on_finish: dict[str, Callable[[dict[str, Any], str, str], None]] = {}   # task id -> callback (in memory)
_cancellers: dict[str, Callable[[], None]] = {}   # tracked row id -> stops its run (in memory)
_tracked: set[str] = set()                         # tracked rows alive in this process
Runner = Callable[..., tuple]   # runner(brief, context[, on_step]) -> (full_text, status[, kind])
_runner: Runner | None = None


def set_runner(fn: Runner | None) -> None:
    """Tests: ``fn(brief, context[, on_step]) -> (full_text, status)`` instead of the worker session."""
    global _runner
    _runner = fn


set_task_runner = set_runner   # the old name (voice_front) still reads well in tests


def list_tasks() -> list[dict[str, Any]]:
    v = db.kv_get(KV_TASKS, [])
    return v if isinstance(v, list) else []


def running_tasks() -> list[dict[str, Any]]:
    return [t for t in list_tasks() if t.get("status") == "running"]


def get_task(task_id: str) -> dict[str, Any] | None:
    return next((t for t in list_tasks() if t.get("id") == task_id), None)


def _save(task: dict[str, Any], *, publish: bool = True) -> None:
    """Unfinished rows (running / retrying) are always kept; finished ones are trimmed to ``TASK_KEEP``."""
    def put(cur: Any) -> list[dict[str, Any]]:
        rows = [t for t in (cur if isinstance(cur, list) else []) if t.get("id") != task["id"]]
        rows.append(dict(task))
        keep = {r.get("id") for r in [r for r in rows if r.get("status") not in UNFINISHED][-TASK_KEEP:]}
        return [t for t in rows if t.get("status") in UNFINISHED or t.get("id") in keep]

    with _lock:   # atomic in the DB too: the tool process of a text turn (task_cancel/retry) writes here as well
        db.kv_update(KV_TASKS, put, [])
    if publish:
        events.publish("task", task=public(task))


def public(task: dict[str, Any]) -> dict[str, Any]:
    """What the page sees. ``summary`` is the one-line collapsed view; ``result`` (the full answer,
    capped at :data:`RESULT_CHARS`) and ``heard`` fill the row when he expands it."""
    return {k: task.get(k) for k in ("id", "task", "status", "started", "finished", "summary", "origin", "steps",
                                     "meta", "heard", "result", "attempts", "next_try", "last_error", "tracked")}


def _msg_line(m: Any) -> dict[str, Any]:
    return {"id": m.id, "ts": m.ts, "who": "you" if m.direction == "in" else "cecil", "channel": m.channel,
            "kind": m.kind, "text": (m.body or "")[:2000]}


def detail(task_id: str) -> dict[str, Any] | None:
    """Everything about one task for the expanded rail row (``GET /voice/api/task/<id>``): the request,
    what he said that led to it, every step, the full result and what was said when it finished.
    ``None`` when unknown."""
    row = get_task(task_id)
    if row is None:
        return None
    out = {**public(row), "heard": row.get("heard") or "",
           "brief": row.get("brief") or brief_for(row.get("task") or "", row.get("heard") or ""),
           "result": row.get("result") or "", "announced": "", "conversation": [], "followups": []}
    started = row.get("started") or db.now_iso()
    try:
        since = db.to_iso(db.parse_iso(started) - timedelta(minutes=CONVO_WINDOW_MIN))
        msgs = sorted(store.list_messages(400, since=since), key=lambda m: (m.ts, m.id))
    except Exception as exc:   # the row still opens without the transcript
        log.warning("task conversation unreadable: %s", exc)
        msgs = []

    def tid_of(m: Any) -> Any:
        return m.meta.get("task_id") if isinstance(m.meta, dict) else None

    finished = row.get("finished")
    for m in msgs:
        if tid_of(m) == task_id:
            if m.kind == "voice_detail":
                out["result"] = m.body or ""
            elif m.kind == "task_done":
                out["announced"] = m.body or ""
            continue
        if m.channel not in ("voice", "dashboard", "sms") or m.kind in ("voice_detail", "task_done"):
            continue
        if m.ts <= started:
            out["conversation"].append(_msg_line(m))
        elif finished and m.ts <= finished and m.channel == "voice":
            out["followups"].append(_msg_line(m))   # what was said while it ran
    out["conversation"] = out["conversation"][-CONVO_KEEP:]
    out["followups"] = out["followups"][-CONVO_KEEP:]
    return out


def brief_for(task: str, heard: str) -> str:
    return (f"{task.strip()}\n\n<background_task>\nHe asked for this in conversation"
            + (f' ("{heard.strip()[:300]}")' if heard and heard.strip() != task.strip() else "")
            + ". You are working on it in the background while he gets on with his day. Do the work now. Finish "
              "with a short plain answer: what you found or did, key facts first. It will be told to him out loud."
              "\n</background_task>")


def start(task: str, *, heard: str = "", origin: str = "browser", brief: str | None = None,
          model: str | None = None, effort: str | None = None, announce: bool = True,
          meta: dict[str, Any] | None = None,
          on_finish: Callable[[dict[str, Any], str, str], None] | None = None, self_edit: bool = False,
          timeout: float | None = None, usage_share: float | None = None, staff: str | None = None) -> str:
    """Start ``task`` in the background; returns its id at once.

    Scheduled work (assistant/schedule.py) passes its own ``brief`` (the full prompt; ``task`` is then the
    short label), a cheaper ``model``/``effort``, ``announce=False`` to stay quiet when it finishes, ``meta``
    (shown with the task) and ``on_finish(row, full_text, status)``. ``self_edit=True`` (initiative work)
    wraps the run in the self-edit guardrail (``selfedit.begin``/``finish``): code it changed is tested and
    committed (or rolled back) before ``on_finish`` runs, which then sees ``row["edit"]`` = {status, summary,
    files, commit, commits}; a committed change restarts the app afterwards."""
    tid = uuid.uuid4().hex[:10]
    meta = dict(meta or {})
    if staff:
        brief = (brief or brief_for(task, heard or task)) + staff_note(staff)
        meta.update(staff=staff, label=_display(staff))
    row = {"id": tid, "task": " ".join(task.split())[:300], "heard": heard[:300], "origin": origin,
           "status": "running", "started": db.now_iso(), "finished": None, "summary": "", "steps": [],
           "announce": announce, "meta": meta}
    if brief:
        row["brief"] = brief
    if model or effort:
        row["model"], row["effort"] = model, effort
    if timeout:
        row["timeout"] = float(timeout)
    if usage_share:
        row["usage_share"] = float(usage_share)
    if self_edit:
        row["self_edit"] = True
    if on_finish is not None:
        _on_finish[tid] = on_finish
    elif not self_edit:
        row["retry"] = True     # a conversation task: retried until it finishes (assistant.retry)
    _save(row)
    db.log_event("assistant", "background task started", data={"task_id": tid, "task": row["task"][:200]})
    _start(row)
    return tid


def _display(agent: str) -> str:
    try:
        import home

        return home.agent_display(agent)
    except Exception:
        return agent.replace("-", " ").capitalize()


def staff_note(agent: str) -> str:
    """The part of a brief that gives the work to one of the staff."""
    return (f"\n\n<assigned_to staff=\"{agent}\">\nThis is {_display(agent)}'s area. Hand the work to them with the "
            f"Agent tool (subagent_type \"{agent}\") and a self-contained brief with every fact they need, check what "
            "they bring back (send them back if it falls short), then finish with the answer. Do not do their part "
            "yourself unless they cannot.\n</assigned_to>")


# ---------------------------------------------------------------------------
# tracked rows: work that runs elsewhere (a text being answered, a scheduled staff job)
# ---------------------------------------------------------------------------

def track(task: str, *, origin: str = "sms", heard: str = "", meta: dict[str, Any] | None = None,
          on_cancel: Callable[[], None] | None = None, task_id: str | None = None) -> str:
    """Put work that is running elsewhere on the rail; returns its id. Close it with :func:`track_done`.
    ``task_id`` of an existing row = the same work again (a retry): that row runs again, one more attempt."""
    old = get_task(task_id) if task_id else None
    tid = old["id"] if old else uuid.uuid4().hex[:10]
    row = {"id": tid, "task": " ".join(str(task).split())[:300] or "Request", "heard": heard[:300], "origin": origin,
           "status": "running", "started": db.now_iso(), "finished": None, "summary": "", "steps": [],
           "announce": False, "tracked": True, "meta": {**((old or {}).get("meta") or {}), **dict(meta or {})},
           "attempts": int((old or {}).get("attempts") or 0) + 1, "next_try": None,
           "last_error": (old or {}).get("last_error")}
    _tracked.add(tid)
    if on_cancel is not None:
        _cancellers[tid] = on_cancel
    _save(row)
    return tid


def track_step(task_id: str, label: str) -> None:
    label = " ".join(str(label or "").split())[:120]
    row = get_task(task_id)
    if not label or row is None or row.get("status") != "running":
        return
    steps = row.get("steps") or []
    if steps and steps[-1].get("label") == label:
        return
    row["steps"] = steps[-(MAX_STEPS - 1):] + [{"label": label, "ts": db.now_iso()}]
    _save(row)


def track_done(task_id: str, text: str, status: str = "ok", *, summary: str | None = None,
               error: str = "") -> None:
    """Close a tracked row. One he cancelled (or that a newer request replaced) keeps its cancelled state.
    ``error``: what actually went wrong (kept as ``last_error``, shown on the rail)."""
    _tracked.discard(task_id)
    _cancellers.pop(task_id, None)
    row = get_task(task_id)
    if row is None or row.get("status") == "cancelled":
        return
    polished = persona.polish(text or "", "dashboard")
    result = polished if len(polished) <= RESULT_CHARS else polished[:RESULT_CHARS - 1].rstrip() + "…"
    row.update(status="done" if status in ("ok", "rounds") else status, finished=db.now_iso(),
               summary=(summary or fallback_summary(text, 1))[:300], result=result)
    if error:
        row["last_error"] = error[:300]
    elif status in ("ok", "rounds"):
        row["last_error"] = None
    _save(row)


def track_retrying(task_id: str | None, next_try: Any, why: str) -> None:
    """A tracked row whose owner (e.g. the deferred queue) will run it again at ``next_try``."""
    row = get_task(task_id) if task_id else None
    if row is None or row.get("status") == "cancelled":
        return
    from assistant import claude_code

    row.update(status="retrying", next_try=db.to_iso(next_try), last_error=why[:300], finished=None,
               summary=f"Retrying {claude_code.friendly_time(next_try)}: {why}"[:300])
    _save(row)


def _start(row: dict[str, Any]) -> None:
    th = threading.Thread(target=_run, args=(row,), name=f"task-{row['id'][:6]}", daemon=True)
    _threads[row["id"]] = th
    th.start()


def _alive(task_id: str) -> bool:
    th = _threads.get(task_id)
    return th is not None and th.is_alive()


delegate = start   # heartbeat and older callers


def wait(task_id: str, timeout: float = 10.0) -> bool:
    th = _threads.get(task_id)
    if th is not None:
        th.join(timeout)
        return not th.is_alive()
    return True


def join_all(timeout: float = 30.0) -> list[str]:
    """Wait for every background-task thread to end; the ids still running after ``timeout`` seconds.

    The test harness calls this before restoring the environment, so a task's slow ending (its
    finish bookkeeping, a usage handoff) can never land in the owner's real database or vault."""
    deadline = time.monotonic() + max(0.0, timeout)
    for th in list(_threads.values()):
        th.join(max(0.0, deadline - time.monotonic()))
    return [tid for tid, th in list(_threads.items()) if th.is_alive()]


def _call_runner(fn: Runner, brief: str, context: dict[str, Any],
                 on_step: Callable[[str], None]) -> tuple[str, str, str]:
    try:
        n = len(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        n = 3
    out = fn(brief, context, on_step) if n >= 3 else fn(brief, context)
    return out[0], out[1], (out[2] if len(out) > 2 else "")


def _run(row: dict[str, Any]) -> None:
    s = get_assistant_settings()

    def on_step(label: str) -> None:
        label = " ".join(str(label).split())[:120]
        if not label:
            return
        row["steps"] = (row.get("steps") or [])[-(MAX_STEPS - 1):] + [{"label": label, "ts": db.now_iso()}]
        _save(row)

    try:
        from assistant import status

        context = {"status": status.turn_context()[:800]}
    except Exception as exc:
        log.debug("ignored error (best effort): %r", exc)
        context = {}
    attempt = int(row.get("attempts") or 0) + 1
    row["attempts"] = attempt
    brief = row.get("brief") or brief_for(row["task"], row["heard"] or row["task"])
    if attempt > 1:
        brief += retry.retry_note(attempt, row.get("last_error", ""))
    if row.get("model") or row.get("effort"):
        context.update(model=row.get("model"), effort=row.get("effort"))
    snap = _edit_begin() if row.get("self_edit") else None
    kind = ""
    try:
        if _runner is not None:
            full, st, kind = _call_runner(_runner, brief, context, on_step)
        else:
            from assistant import brain

            full, st, kind = brain.run_worker(
                brief, on_step=on_step, model=row.get("model"), effort=row.get("effort"), with_kind=True,
                timeout=row.get("timeout"), usage_share=row.get("usage_share"),
                should_stop=lambda: (get_task(row["id"]) or {}).get("status") == "cancelled")
    except Exception as exc:
        log.exception("background task failed")
        db.log_event("assistant", "background task failed", level="error",
                     data={"error": repr(exc)[:300], "task_id": row["id"]})
        reason = " ".join(str(exc).split())[:200] or type(exc).__name__
        full, st = f"I couldn't finish that, {s.owner_honorific}: {type(exc).__name__}: {reason}", "error"
        kind = getattr(exc, "kind", "") or "error"
    restart = _edit_finish(snap, row) if snap is not None else False
    if (get_task(row["id"]) or {}).get("status") == "cancelled":   # he stopped it while it ran: leave it stopped
        _on_finish.pop(row["id"], None)
        if restart:
            from assistant import selfedit

            selfedit.schedule_restart()
        return
    if row.get("retry") and st in RETRY_STATUSES:
        _schedule_retry(row, full, st, kind)
        return
    finish(row, full, st)
    if restart:
        from assistant import selfedit

        selfedit.schedule_restart()


def _edit_begin() -> Any:
    try:
        from assistant import selfedit

        return selfedit.begin()
    except Exception as exc:
        log.warning("self-edit guard unavailable for a background task: %s", exc)
        return None


def _edit_finish(snap: Any, row: dict[str, Any]) -> bool:
    """Test + commit (or roll back) what the task changed; record it on ``row["edit"]``. True = restart."""
    from assistant import selfedit

    try:
        out = selfedit.finish(snap, summary=row["task"][:72])
        commits = selfedit.commits_since(snap.head) if getattr(snap, "ok", False) else []
    except Exception as exc:
        log.warning("self-edit check after a background task failed: %s", exc)
        row["edit"] = {"status": "error", "summary": repr(exc)[:300], "files": [], "commit": None, "commits": []}
        return False
    row["edit"] = {"status": out.status if out else "unchanged", "summary": (out.summary if out else "")[:600],
                   "files": (out.files if out else [])[:30], "commit": out.commit if out else None,
                   "commits": commits}
    return bool(out and out.restart)


def _schedule_retry(row: dict[str, Any], full: str, status: str, kind: str) -> None:
    """The run did not finish: park it as ``retrying`` (:func:`retry_due` starts it again), tell him once
    on the first failure, and text him if the blocker is something he can fix."""
    from assistant import claude_code

    s = get_assistant_settings()
    attempt = int(row.get("attempts") or 1)
    nxt = retry.next_try(status, attempt, kind)
    why = retry.describe(status, kind)
    detail = " ".join((full or "").split())
    if status == "error" and kind in ("", "error") and detail:   # the real reason, not "it failed with an error"
        why = f"{why} ({detail[:180]})"
    row.update(status="retrying", next_try=db.to_iso(nxt), last_error=why, last_status=status, finished=None,
               summary=f"Retrying {claude_code.friendly_time(nxt)}: {why}"[:300])
    _save(row)
    db.log_event("assistant", "background task will be retried", level="warning",
                 data={"task_id": row["id"], "attempt": attempt, "status": status, "kind": kind,
                       "next_try": row["next_try"]})
    try:
        store.add_message("out", "dashboard", persona.polish(full or why, "dashboard"), kind="voice_detail",
                          agent="main", meta={"task_id": row["id"], "task": row["task"], "attempt": attempt})
        if attempt == 1 and row.get("announce", True):   # said once; later failures stay quiet unless he can fix them
            say(f"I couldn't finish that yet, {s.owner_honorific}: {why}. I'll keep trying, next "
                f"{claude_code.friendly_time(nxt)}.", origin=row.get("origin") or "any", task=row["task"],
                task_id=row["id"])
    except Exception as exc:
        log.warning("retry notice not recorded: %s", exc)
    try:
        retry.report_blocker(status, kind, attempt, row["task"])
    except Exception as exc:
        log.warning("blocker report failed: %s", exc)


def _requeue_orphans(now: Any = None) -> int:
    """Retryable rows still "running" with no thread here, older than any run can last, were cut off by a
    restart or a crash: park them as due now. Returns how many."""
    s = get_assistant_settings()
    now = now or db.utcnow()
    stale = timedelta(seconds=s.cc_task_timeout_sec + ORPHAN_GRACE_SEC)
    n = 0
    for t in list_tasks():
        if (t.get("status") == "running" and t.get("tracked") and t["id"] not in _tracked
                and db.parse_iso(t["started"]) < now - stale):
            t.update(status="error", finished=db.to_iso(now), summary="Interrupted: the app restarted while it ran")
            _save(t)
            continue
        if (t.get("status") == "running" and t.get("retry") and not _alive(t["id"])
                and db.parse_iso(t["started"]) < now - stale):
            t.update(status="retrying", next_try=db.to_iso(now), last_error="the app restarted mid-task",
                     attempts=int(t.get("attempts") or 1))
            _save(t)
            n += 1
    if n:
        db.log_event("assistant", f"{n} interrupted background task(s) requeued", level="warning")
    return n


def cancel(task_id: str, why: str = "stopped at his request") -> dict[str, Any] | None:
    """Stop a background task: a retrying one never runs again; a running one is left to end on its own
    but its result is dropped and it is not retried. The page is told at once (an already-cancelled row
    is re-published, which clears a stale "Retrying" line). ``None`` when unknown."""
    row = get_task(task_id)
    if row is None:
        return None
    stop = _cancellers.pop(task_id, None)
    if row.get("status") not in ("done",):
        row.update(status="cancelled", next_try=None, finished=row.get("finished") or db.now_iso(),
                   summary=f"Cancelled: {why}"[:300])
    _save(row)
    if stop is not None:
        try:
            stop()
        except Exception as exc:
            log.warning("could not stop a tracked run: %s", exc)
    db.log_event("assistant", "background task cancelled", data={"task_id": task_id, "why": why[:200]})
    return row


def retry_now(task_id: str) -> dict[str, Any] | None:
    """Run an unfinished/failed task again NOW - the same work, never a duplicate. ``None`` when unknown;
    a running or finished-ok task is left alone.

    A background worker row restarts at once (no waiting for other tasks). A *tracked* row is re-run by
    whoever owns it: a deferred item goes back to the deferred queue and runs now
    (``usage.retry_deferred``); a text or dashboard request is queued as his request again
    (``usage.defer`` kind ``owner_request``), which answers on the same channel."""
    row = get_task(task_id)
    if row is None:
        return None
    if row.get("status") in ("running", "done"):
        return row
    meta = row.get("meta") or {}
    if row.get("tracked"):
        from assistant import usage

        did = meta.get("deferred_id")
        if not did:
            channel = str(meta.get("via") or "dashboard")
            did = usage.defer(row.get("heard") or row.get("task") or "", kind="owner_request",
                              channel=channel if channel in ("sms", "dashboard") else "dashboard",
                              at=db.utcnow(), reason="retry asked for")
            store.merge_deferred_meta(did, track_id=task_id)
            meta = {**meta, "deferred_id": did}
        # mark it first: the rerun may finish (and update this row) before retry_deferred returns
        row.update(status="retrying", next_try=db.now_iso(), finished=None, meta=meta, summary="Retrying now")
        _save(row)
        usage.retry_deferred(int(did))
        return get_task(task_id)
    row.update(status="retrying", next_try=db.now_iso(), finished=None)
    _save(row)
    retry_due(force=True)
    return get_task(task_id)


def _find(ref: str) -> dict[str, Any] | None:
    """A task by id, or by words of its request (newest unfinished first)."""
    ref = (ref or "").strip()
    if not ref:
        return None
    row = get_task(ref)
    if row is not None:
        return row
    low = ref.lower()
    rows = sorted(list_tasks(), key=lambda t: (t.get("status") in UNFINISHED, t.get("started") or ""), reverse=True)
    return next((t for t in rows if low in str(t.get("task") or "").lower()), None)


def register_tools(register: Callable[..., None]) -> None:
    from assistant.tools import Tool, ToolError, _obj

    def _cancel(ctx: Any, a: dict[str, Any]) -> Any:
        row = _find(str(a.get("task") or ""))
        if row is None:
            raise ToolError("no such background task (use task_list for ids)")
        out = cancel(row["id"], str(a.get("why") or "stopped at his request"))
        return {"cancelled": row["id"], "status": (out or {}).get("status"), "task": row.get("task")}

    def _retry(ctx: Any, a: dict[str, Any]) -> Any:
        row = _find(str(a.get("task") or ""))
        if row is None:
            raise ToolError("no such background task (use task_list for ids)")
        out = retry_now(row["id"]) or {}
        return {"task": row["id"], "status": out.get("status"), "note": "the same task runs again; no new one was started"}

    register(Tool("task_cancel", "Cancel a background task (id or words of its request): a retrying one never runs "
                  "again and leaves the list as cancelled; a running one stops.", _obj({"task": {"type": "string"},
                  "why": {"type": "string"}}, ["task"]), _cancel, owner=__name__, side_effects=True), replace=True)
    register(Tool("task_retry", "Run a failed or retrying task from the rail again NOW - the same work, never a new "
                  "copy, in parallel with whatever else runs. Works for background tasks, deferred/scheduled passes "
                  "and failed text or dashboard requests (each goes back through its own path). Every row has "
                  "last_error = the real reason it failed: tell him that, not a guess.",
                  _obj({"task": {"type": "string"}}, ["task"]), _retry, owner=__name__, side_effects=True),
             replace=True)


def _due_now(t: dict[str, Any], now: Any, plan_free: bool) -> bool:
    if not t.get("next_try"):
        return True
    if db.parse_iso(t["next_try"]) <= now:
        return True
    # stopped by the plan limit: the moment usage is free again it goes, whatever the estimated reset said
    return plan_free and t.get("last_status") == "budget"


def retry_due(now: Any = None, *, limit: int | None = None, force: bool = False) -> int:
    """Scheduler job (every minute) and :func:`assistant.usage.kick`: requeue orphans, then restart every
    background task whose retry time has come - in parallel, up to :data:`MAX_PARALLEL` workers alive at
    once (never "one at a time" and never waiting for unrelated tasks). Skipped while switched off or while
    the plan is limited (``force``: one he asked for goes anyway when the plan is free). Tracked rows (texts,
    deferred items) are re-run by their owner, never here. Returns how many were started."""
    from assistant import usage

    s = get_assistant_settings()
    if not s.enabled:
        return 0
    now = now or db.utcnow()
    _requeue_orphans(now)
    if usage.wait_reason(optional=False):     # switched off or at the plan's limit
        return 0
    plan_free = not usage.wait_reason()
    room = MAX_PARALLEL - sum(1 for tid in list(_threads) if _alive(tid))
    if limit is not None:
        room = min(room, limit)
    due = [t for t in list_tasks() if t.get("status") == "retrying" and not t.get("tracked")
           and (force or _due_now(t, now, plan_free))]
    n = 0
    for t in due[:max(0, room)]:
        t.update(status="running", started=db.to_iso(now), next_try=None)
        _save(t)
        db.log_event("assistant", "background task retry started",
                     data={"task_id": t["id"], "attempt": int(t.get("attempts") or 0) + 1})
        _start(t)
        n += 1
    if len(due) > n:
        db.log_event("assistant", f"{len(due) - n} task retry(ies) waiting for a free worker",
                     data={"max_parallel": MAX_PARALLEL})
    return n


def finish(row: dict[str, Any], full: str, status: str) -> None:
    polished = persona.polish(full or "", "dashboard")
    result = polished if len(polished) <= RESULT_CHARS else polished[:RESULT_CHARS - 1].rstrip() + "…"
    row.update(status="done" if status == "ok" else status, finished=db.now_iso(),
               summary=fallback_summary(full, 1)[:300], result=result)
    _save(row)
    try:
        store.add_message("out", "dashboard", polished, kind="voice_detail",
                          agent="main", meta={"task_id": row["id"], "task": row["task"]})
    except Exception as exc:
        log.warning("task result not recorded: %s", exc)
    db.log_event("assistant", "background task finished", data={"task_id": row["id"], "status": row["status"]})
    cb = _on_finish.pop(row["id"], None)
    if cb is not None:
        try:
            cb(row, full, status)
        except Exception as exc:
            log.warning("task finish callback failed: %s", exc)
            db.log_event("assistant", "scheduled task bookkeeping failed", level="warning",
                         data={"task_id": row["id"], "error": repr(exc)[:300]})
    if status == "budget":   # the plan ran out mid-task: hand the work over to the next usage window
        try:
            from assistant import handoff

            handoff.on_task_stopped(row)
        except Exception as exc:
            log.warning("usage handoff failed: %s", exc)
    if row.get("announce", True):
        announce(row, full)


def fallback_summary(full: str, n: int = 3) -> str:
    from voice import speech

    sents = [speech.clean_spoken(x) for x in speech.split_sentences(persona.polish(full or "", "dashboard"))]
    out = " ".join([x for x in sents if x][:n]).strip()
    return out or f"That task is finished, {get_assistant_settings().owner_honorific}."


def announce(row: dict[str, Any], full: str) -> bool:
    """Tell him the task is done, exactly once per task. True if this call claimed it."""
    if not db.kv_set_if_absent(KV_ANNOUNCED.format(id=row["id"]), db.now_iso()):
        return False
    try:
        from assistant import brain

        if brain.announce_task(row, full):
            return True        # the warm brain speaks it and records the task_done message
    except Exception as exc:
        log.warning("brain announcement failed: %s", exc)
    say(fallback_summary(full), origin=row.get("origin") or "any", task=row["task"], task_id=row["id"])
    return True


def say(text: str, *, origin: str = "any", task: str = "", task_id: str | None = None,
        urgency: str | None = None) -> int:
    """Record a spoken announcement (``task_done``) and stream it to the page as a short turn."""
    meta: dict[str, Any] = {"origin": origin, "task": task[:200], "task_id": task_id}
    if urgency:
        meta["urgency"] = urgency
    mid = store.add_message("out", "voice", text, kind="task_done", agent="main", meta=meta)
    turn = f"a{mid}"
    events.publish("turn", turn=turn, origin=origin, kind="announce", heard="")
    from voice import speech

    for sent in speech.split_sentences(text):
        events.publish("sentence", turn=turn, origin=origin, text=sent)
    events.publish("done", turn=turn, origin=origin, text=text, status="ok", cost_usd=0.0)
    return mid


# ---------------------------------------------------------------------------
# finished-task announcements for the laptop voice process
# ---------------------------------------------------------------------------

def task_events(after_id: int = 0, *, limit: int = 20) -> list[dict[str, Any]]:
    """Spoken ``task_done`` messages newer than ``after_id`` (oldest first)."""
    out = []
    for m in store.list_messages(limit, channel="voice", direction="out"):
        if m.kind == "task_done" and m.id > after_id:
            meta = m.meta if isinstance(m.meta, dict) else {}
            out.append({"id": m.id, "text": m.body, "origin": meta.get("origin", ""), "task_id": meta.get("task_id"),
                        "task": meta.get("task", "")})
    return sorted(out, key=lambda e: e["id"])


def latest_event_id() -> int:
    ev = task_events(0)
    return ev[-1]["id"] if ev else 0


class Announcer:
    """For the laptop voice process: the summaries meant for it, once each."""

    def __init__(self, origin: str = "voice"):
        self.origin = origin
        self.after: int | None = None

    def __call__(self) -> list[str]:
        if self.after is None:
            self.after = latest_event_id()
            return []
        evs = task_events(self.after)
        if evs:
            self.after = evs[-1]["id"]
        return [e["text"] for e in evs if e["origin"] in (self.origin, "any")]
