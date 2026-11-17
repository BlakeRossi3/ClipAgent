"""Back-to-back texts become one request and get one reply.

Blake: "if I send multiple texts sequentially, they'll be split up into multiple messages and each will
get a response. Not really what I want. So let's handle that the same way that we handle voice talking.
If I text multiple things sequentially before it answers, then cancel the previous messages that I sent
and combine the text from all of the messages and just send one."

How (the spoken path's "strict single flight", ``assistant/brain.ask``, for texts):

* Every text is recorded the moment it arrives (History shows it at once) and joins :data:`_pending`.
* The first text's handler becomes the *leader*: it waits until no new text has come for
  ``ASSISTANT_SMS_BATCH_SEC`` (default 4 s), then answers everything pending as ONE request
  (``runtime.run_turn(..., inbound_ids=...)``: no duplicate inbound rows; the reply answers the last text).
* A text that arrives while that answer is being worked on stops it (``claude_code.RunControl.cancel``:
  the ``claude -p`` process is terminated and nothing is sent) and the leader starts again on all the
  texts together. Once a reply has gone out, those texts are answered; a later text is a new request.
* Later texts never start a second run: one conversation, one ``claude --resume`` at a time (two at once
  used to race on the same session).

Check-in answers (``checkins.handle_reply``) are still handled in plain code before a text gets here.
Tests: ``ASSISTANT_SMS_BATCH_SEC=0`` (the shared fixture) answers at once; :func:`set_sleeper`.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import db

log = logging.getLogger(__name__)

SUPERSEDED = "Combined with your next text"
TICK_SEC = 0.25


@dataclass
class _Text:
    text: str
    message_id: int
    attachments: list[str] = field(default_factory=list)


_lock = threading.Lock()
_pending: list[_Text] = []
_last_at = 0.0
_leader = False
_current: Any = None          # claude_code.RunControl of the answer being worked on
_sleep: Callable[[float], None] = time.sleep


def set_sleeper(fn: Callable[[float], None] | None) -> None:
    global _sleep
    _sleep = fn or time.sleep


def reset() -> None:
    """Tests: forget everything pending."""
    global _last_at, _leader, _current
    with _lock:
        _pending.clear()
        _last_at, _leader, _current = 0.0, False, None


def pending() -> list[dict[str, Any]]:
    with _lock:
        return [{"text": t.text, "message_id": t.message_id} for t in _pending]


def combine(texts: list[_Text]) -> str:
    return "\n".join(t.text.strip() for t in texts if t.text.strip()) or "(sent an attachment)"


def receive(text: str, *, attachments: list[str] | None = None) -> dict[str, Any]:
    """Take one text from him (already authenticated and de-duplicated). Blocks while this call leads
    the answer; a text that joins an answer already being prepared returns at once."""
    global _last_at, _leader
    from assistant import store

    meta = {"attachments": [Path(p).name for p in attachments]} if attachments else None
    mid = store.add_message("in", "sms", text, kind="chat", meta=meta)
    with _lock:
        _pending.append(_Text(text, mid, list(attachments or [])))
        _last_at = time.monotonic()
        running = _current
        lead = not _leader
        if lead:
            _leader = True
        waiting = len(_pending)
    if running is not None and not running.cancelled:
        running.reason = SUPERSEDED
        running.cancel()       # he was still being answered: start again on everything together
        db.log_event("assistant", "texts combined: the answer in progress was stopped",
                     data={"message_id": mid, "texts": waiting})
    if not lead:
        return {"message_id": mid, "joined": True}
    try:
        res = _lead()
    except Exception:
        with _lock:
            _leader = False
        raise
    return {"message_id": mid, "joined": False, **res}


def _lead() -> dict[str, Any]:
    """Answer everything pending; run again while newer texts keep replacing the answer."""
    global _leader, _current
    from assistant import claude_code, runtime
    from assistant.settings import get_assistant_settings

    rounds, last_status = 0, ""
    while True:
        while True:   # quiet for the batch window?
            with _lock:
                if not _pending:
                    _leader = False
                    return {"rounds": rounds, "status": last_status}
                wait = _last_at + get_assistant_settings().sms_batch_sec - time.monotonic()
            if wait <= 0:
                break
            _sleep(min(wait, TICK_SEC))
        with _lock:
            batch = list(_pending)
            ctl = claude_code.RunControl()
            _current = ctl
        rounds += 1
        context = None
        if len(batch) > 1:
            context = {"note": f"He sent {len(batch)} texts one after another before you replied; they are joined "
                               "below as one request. Answer all of it in one reply."}
            db.log_event("assistant", f"{len(batch)} texts answered as one request",
                         data={"message_ids": [t.message_id for t in batch]})
        try:
            res = runtime.run_turn(combine(batch), channel="sms", deliver=True, context=context,
                                   attachments=[a for t in batch for a in t.attachments] or None,
                                   inbound_ids=[t.message_id for t in batch], control=ctl)
            last_status = res.status
        except Exception as exc:   # run_turn reports its own failures; never leave texts stuck here
            log.exception("text not answered")
            db.log_event("assistant", "a text could not be answered", level="error", data={"error": repr(exc)[:300]})
            last_status = "error"
        with _lock:
            _current = None
            superseded = last_status == "cancelled" and getattr(ctl, "reason", "") == SUPERSEDED
            if not superseded:   # answered (or he stopped it from the rail): those texts are done
                del _pending[:len(batch)]
