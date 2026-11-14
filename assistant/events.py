"""Live events from Sterling's brain to the Jarvis page (``GET /voice/api/stream``, server-sent events).

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
    settings    {voice: {...}}                                    voice/engine changed: reload the speaker
    ignored     {text, reason}                                    an utterance was treated as noise

The bus also folds the events into a small :func:`snapshot` (current turn with its steps, recent
turns, tasks, panels) so a page that opens mid-turn, or reconnects, shows the same rail.
Nothing here touches the database: it is memory only and lives as long as the server process.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

import db

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

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            turns = [self._turns[t] for t in self._turn_order if t in self._turns]
            return {"seq": self._seq, "current": self._current,
                    "turns": [{k: v for k, v in t.items() if k != "t0"} for t in turns],
                    "tasks": sorted(self._tasks.values(), key=lambda x: x.get("started") or ""),
                    "panels": list(self._panels)}


BUS = Bus()


def publish(type_: str, **data: Any) -> dict[str, Any]:
    return BUS.publish(type_, **data)


def snapshot() -> dict[str, Any]:
    return BUS.snapshot()


def reset() -> None:
    """Tests: forget everything."""
    BUS.clear()
