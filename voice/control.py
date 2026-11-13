"""The voice loop's switches and status, shared through the database ``kv`` table.

The dashboard (web process), the tray icon, the hotkey and the loop itself all read and write
the same two keys, so a mute set anywhere holds everywhere:

* ``voice:muted``  - present = hard mute (microphone closed, no wake word either).
* ``voice:status`` - what the loop is doing, published by the voice process (with a heartbeat).
"""

from __future__ import annotations

import os
import time
from typing import Any

import db

KV_MUTED = "voice:muted"
KV_STATUS = "voice:status"
OFFLINE_AFTER_SEC = 20.0
STATES = ("starting", "idle", "listening", "thinking", "speaking", "followup", "muted", "no_mic", "stopped")


class Control:
    """kv-backed (the real one). Tests use :class:`MemoryControl`."""

    def muted(self) -> bool:
        return db.kv_get(KV_MUTED) is not None

    def set_muted(self, on: bool, *, via: str) -> None:
        if on:
            db.kv_set(KV_MUTED, {"at": db.now_iso(), "via": via})
        else:
            db.kv_delete(KV_MUTED)
        db.log_event("voice", f"microphone {'muted' if on else 'unmuted'}", data={"via": via})

    def publish(self, state: str, **info: Any) -> None:
        db.kv_set(KV_STATUS, {"state": state, "ts": db.now_iso(), "epoch": time.time(), "pid": os.getpid(), **info})


class MemoryControl(Control):
    def __init__(self) -> None:
        self._muted = False
        self.published: list[dict[str, Any]] = []
        self.mute_log: list[tuple[bool, str]] = []

    def muted(self) -> bool:
        return self._muted

    def set_muted(self, on: bool, *, via: str) -> None:
        self._muted = on
        self.mute_log.append((on, via))

    def publish(self, state: str, **info: Any) -> None:
        self.published.append({"state": state, **info})


def status() -> dict[str, Any]:
    """For the dashboard pill: the last published status + whether the loop is alive + mute."""
    raw = db.kv_get(KV_STATUS) or {}
    alive = bool(raw) and time.time() - float(raw.get("epoch") or 0) < OFFLINE_AFTER_SEC
    muted = db.kv_get(KV_MUTED) is not None
    state = raw.get("state", "stopped") if alive else "offline"
    if muted and state not in ("offline",):
        state = "muted"
    return {"state": state, "online": alive, "muted": muted, "device": raw.get("device"),
            "voice": raw.get("voice"), "wake": raw.get("wake"), "error": raw.get("error"),
            "heard": raw.get("heard"), "ts": raw.get("ts")}
