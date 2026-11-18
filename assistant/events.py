"""Live events from Cecil's brain to the Jarvis page (``GET /voice/api/stream``, server-sent events).

One in-process bus. The brain (``assistant/brain.py``), background tasks (``assistant/tasks.py``),
the ``display`` tool (``assistant/panels.py``) and live settings changes publish small dicts;
the SSE endpoint streams them to every open page. Adapted from the WebSocket frames of
adewaskar/jarvis ``bridge/server.mjs`` (MIT License, Copyright (c) 2026 Aditya Dewaskar):
``text`` deltas, ``tool`` announcements, ``panel`` pushes and ``done``/``error`` per turn, each
tagged with the turn it belongs to so a page can tell the tail of an abandoned answer from the
start of the next one.

Event types (every event has ``id`` (monotonic), ``type``, ``ts``)::

    turn        {turn, origin, kind: chat|announce, heard}        a turn started
    delta       {turn, text}                                      raw streamed text (live caption)
    sentence    {turn, origin, text, speak}                       one finished, cleaned sentence to say
    tool        {turn, tool_id, name, label, parent}              a step started (or its label refined)
    tool_done   {turn, tool_id, ok}                               that step finished
    done        {turn, origin, text, status, cost_usd}            the turn finished
    error       {turn, message}
    task        {task: {id, task, status, started, finished, summary, steps, origin}}
    panel       {panel: {id, title, html, accent, hold, turn}}
    panel_remove {ids: [panel id, ...]}   (assistant/screen.py: taken off the page)
    settings    {voice: {...}}                                    voice/engine changed: reload the speaker
    ignored     {text, reason}                                    an utterance was treated as noise

The bus also folds the events into a small :func:`snapshot` (current turn with its steps, recent
turns, tasks, panels) so a page that opens mid-turn, or reconnects, shows the same rail.
The bus is memory only and lives as long as the server process. One exception, the **relay**: tools
that run in another process (``assistant/mcp_server.py``, the helper Claude Code starts for text and
dashboard turns) cannot reach this memory, so there :func:`set_relay` makes :func:`publish` write the
event to a kv outbox instead; the server process drains it (:func:`drain_outbox`, at most once a second,
from the page's event stream and from :func:`snapshot`) and publishes each event for real. Without it,
panels put up from a text never reached the page.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

import db

log = logging.getLogger(__name__)

KEEP_EVENTS = 600
KEEP_TURNS = 6
KEEP_PANELS = 6
KEEP_STEPS = 40


class Bus:
    def __init__(self, keep: int = KEEP_EVENTS) -> None:
        self._lock = threading.Condition()
        self._events: deque[dict[str, Any]] = deque(maxlen=keep)
        self._seq = 0
        self._turns: dict[str, dict[str, Any]] = {}
        self._turn_order: deque[str] = deque(maxlen=KEEP_TURNS)
        self._current: str | None = None
        self._tasks: dict[str, dict[str, Any]] = {}
        self._panels: deque[dict[str, Any]] = deque(maxlen=KEEP_PANELS)
        self.listeners = 0

    # -- publishing -------------------------------------------------------------
    @property
    def seq(self) -> int:
        return self._seq

    def publish(self, type_: str, **data: Any) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            evt = {"id": self._seq, "type": type_, "ts": db.now_iso(), "t": time.time(), **data}
            self._events.append(evt)
            self._fold(evt)
            self._lock.notify_all()
        return evt

    def since(self, after: int, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self._events if e["id"] > after][:limit]

    def wait(self, after: int, timeout: float) -> list[dict[str, Any]]:
        """Block until there is an event newer than ``after`` (or ``timeout`` seconds pass)."""
        deadline = time.monotonic() + timeout
        with self._lock:
            while self._seq <= after:
                left = deadline - time.monotonic()
                if left <= 0:
                    return []
                self._lock.wait(left)
            return [e for e in self._events if e["id"] > after]

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._turns.clear()
            self._turn_order.clear()
            self._current = None
            self._tasks.clear()
            self._panels.clear()

    # -- the rail's state ---------------------------------------------------------
    def _fold(self, e: dict[str, Any]) -> None:
        t = e["type"]
        tid = e.get("turn")
        if t == "turn":
            self._turns[tid] = {"id": tid, "origin": e.get("origin"), "kind": e.get("kind", "chat"),
                                "heard": e.get("heard", ""), "text": "", "steps": [], "status": "running",
                                "started": e["ts"], "t0": e["t"]}
            self._turn_order.append(tid)
            for old in list(self._turns):
                if old not in self._turn_order:
                    self._turns.pop(old, None)
            self._current = tid
            # panels held for one turn go when he next speaks (adewaskar/jarvis "hold: turn")
            if e.get("kind", "chat") == "chat":
                kept = [p for p in self._panels if p.get("hold") == "sticky"]
                self._panels.clear()
                self._panels.extend(kept)
        elif t == "sentence" and tid in self._turns:
            cur = self._turns[tid]["text"]
            self._turns[tid]["text"] = (cur + " " + e.get("text", "")).strip()
        elif t == "tool" and tid in self._turns:
            steps = self._turns[tid]["steps"]
            for s in steps:
                if s["id"] == e.get("tool_id"):
                    s["label"] = e.get("label") or s["label"]
                    break
            else:
                steps.append({"id": e.get("tool_id"), "label": e.get("label", ""), "name": e.get("name", ""),
                              "status": "running", "parent": e.get("parent"), "t": e["t"]})
                del steps[:-KEEP_STEPS]
        elif t == "tool_done" and tid in self._turns:
            for s in self._turns[tid]["steps"]:
                if s["id"] == e.get("tool_id"):
                    s["status"] = "ok" if e.get("ok", True) else "error"
        elif t in ("done", "error") and tid in self._turns:
            self._turns[tid]["status"] = e.get("status", "ok") if t == "done" else "error"
            if t == "done" and e.get("text"):
                self._turns[tid]["text"] = e["text"]
            for s in self._turns[tid]["steps"]:
                if s["status"] == "running":
                    s["status"] = "ok"
            if self._current == tid:
                self._current = None
        elif t == "task":
            task = dict(e.get("task") or {})
            if task.get("id"):
                prev = self._tasks.get(task["id"], {})
                self._tasks[task["id"]] = {**prev, **task}
                if len(self._tasks) > 12:
                    for k in list(self._tasks)[:-12]:
                        self._tasks.pop(k, None)
        elif t == "panel":
            p = dict(e.get("panel") or {})
            if p.get("id"):
                self._panels.append(p)
        elif t == "panel_remove":   # assistant.screen.clear / dismiss: these panels leave every open page
            gone = set(e.get("ids") or [])
            kept = [p for p in self._panels if p.get("id") not in gone]
            self._panels.clear()
            self._panels.extend(kept)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            turns = [self._turns[t] for t in self._turn_order if t in self._turns]
            return {"seq": self._seq, "current": self._current,
                    "turns": [{k: v for k, v in t.items() if k != "t0"} for t in turns],
                    "tasks": sorted(self._tasks.values(), key=lambda x: x.get("started") or ""),
                    "panels": list(self._panels)}


BUS = Bus()

KV_OUTBOX_SEQ = "events:outbox:seq"
KV_OUTBOX_DONE = "events:outbox:done"
KV_OUTBOX_ITEM = "events:outbox:{n}"
DRAIN_EVERY_SEC = 1.0
_relay = False
_last_drain = 0.0
_drain_lock = threading.Lock()


def set_relay(on: bool) -> None:
    """This process is not the server (assistant/mcp_server.py): publish through the kv outbox."""
    global _relay
    _relay = bool(on)


def publish(type_: str, **data: Any) -> dict[str, Any]:
    if _relay:
        n = db.kv_incr(KV_OUTBOX_SEQ)
        evt = {"type": type_, **data}
        db.kv_set(KV_OUTBOX_ITEM.format(n=n), evt)
        return {"id": 0, **evt}
    return BUS.publish(type_, **data)


def drain_outbox(*, force: bool = False) -> int:
    """Server process: publish events relayed from other processes (oldest first). Returns how many."""
    global _last_drain
    if _relay:
        return 0
    now = time.monotonic()
    if not force and now - _last_drain < DRAIN_EVERY_SEC:
        return 0
    if not _drain_lock.acquire(blocking=False):
        return 0
    try:
        _last_drain = now
        top = int(db.kv_get(KV_OUTBOX_SEQ, 0) or 0)
        done = int(db.kv_get(KV_OUTBOX_DONE, 0) or 0)
        n_out = 0
        for n in range(done + 1, top + 1):
            key = KV_OUTBOX_ITEM.format(n=n)
            evt = db.kv_get(key)
            db.kv_delete(key)
            if isinstance(evt, dict) and evt.get("type"):
                data = {k: v for k, v in evt.items() if k != "type"}
                BUS.publish(str(evt["type"]), **data)
                n_out += 1
        if top > done:
            db.kv_set(KV_OUTBOX_DONE, top)
        return n_out
    except Exception as exc:   # the page's stream must keep flowing
        log.warning("event relay failed: %s", exc)
        return 0
    finally:
        _drain_lock.release()


def snapshot() -> dict[str, Any]:
    drain_outbox()
    return BUS.snapshot()


def reset() -> None:
    """Tests: forget everything."""
    BUS.clear()
