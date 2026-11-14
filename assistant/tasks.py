"""Background tasks: long work Sterling starts mid-conversation and reports back on, out loud, once.

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

Task rows live in ``kv`` (:data:`KV_TASKS`, the newest :data:`TASK_KEEP`).
"""

from __future__ import annotations

import inspect
import logging
import threading
import uuid
from typing import Any, Callable

import db
from assistant import events, persona, store
from assistant.settings import get_assistant_settings

log = logging.getLogger(__name__)

KV_TASKS = "assistant:tasks"
KV_ANNOUNCED = "assistant:tasks:announced:{id}"
TASK_KEEP = 12
MAX_STEPS = 30

_lock = threading.Lock()
_threads: dict[str, threading.Thread] = {}
Runner = Callable[..., tuple[str, str]]   # runner(brief, context[, on_step]) -> (full_text, status)
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
    with _lock:
        rows = [t for t in list_tasks() if t.get("id") != task["id"]]
        rows.append(task)
        db.kv_set(KV_TASKS, rows[-TASK_KEEP:])
    if publish:
        events.publish("task", task=public(task))


def public(task: dict[str, Any]) -> dict[str, Any]:
    return {k: task.get(k) for k in ("id", "task", "status", "started", "finished", "summary", "origin", "steps")}


def brief_for(task: str, heard: str) -> str:
    return (f"{task.strip()}\n\n<background_task>\nHe asked for this in conversation"
            + (f' ("{heard.strip()[:300]}")' if heard and heard.strip() != task.strip() else "")
            + ". You are working on it in the background while he gets on with his day. Do the work now. Finish "
              "with a short plain answer: what you found or did, key facts first. It will be told to him out loud."
              "\n</background_task>")


def start(task: str, *, heard: str = "", origin: str = "browser") -> str:
    """Start ``task`` in the background; returns its id at once."""
    tid = uuid.uuid4().hex[:10]
    row = {"id": tid, "task": " ".join(task.split())[:300], "heard": heard[:300], "origin": origin,
           "status": "running", "started": db.now_iso(), "finished": None, "summary": "", "steps": []}
    _save(row)
    db.log_event("assistant", "background task started", data={"task_id": tid, "task": row["task"][:200]})
    th = threading.Thread(target=_run, args=(row,), name=f"task-{tid[:6]}", daemon=True)
    _threads[tid] = th
    th.start()
    return tid


delegate = start   # heartbeat and older callers


def wait(task_id: str, timeout: float = 10.0) -> bool:
    th = _threads.get(task_id)
    if th is not None:
        th.join(timeout)
        return not th.is_alive()
    return True


def _call_runner(fn: Runner, brief: str, context: dict[str, Any], on_step: Callable[[str], None]) -> tuple[str, str]:
    try:
        n = len(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        n = 3
    return fn(brief, context, on_step) if n >= 3 else fn(brief, context)


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
    except Exception:
        context = {}
    brief = brief_for(row["task"], row["heard"] or row["task"])
    try:
        if _runner is not None:
            full, st = _call_runner(_runner, brief, context, on_step)
        else:
            from assistant import brain

            full, st = brain.run_worker(brief, on_step=on_step)
    except Exception as exc:
        log.exception("background task failed")
        db.log_event("assistant", "background task failed", level="error", data={"error": repr(exc)[:300]})
        full, st = f"I couldn't finish that, {s.owner_honorific}: something went wrong on my side.", "error"
    finish(row, full, st)


def finish(row: dict[str, Any], full: str, status: str) -> None:
    row.update(status="done" if status == "ok" else status, finished=db.now_iso(),
               summary=fallback_summary(full, 1)[:300])
    _save(row)
    try:
        store.add_message("out", "dashboard", persona.polish(full or "", "dashboard"), kind="voice_detail",
                          agent="main", meta={"task_id": row["id"], "task": row["task"]})
    except Exception as exc:
        log.warning("task result not recorded: %s", exc)
    db.log_event("assistant", "background task finished", data={"task_id": row["id"], "status": row["status"]})
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
