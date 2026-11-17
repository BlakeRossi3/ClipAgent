"""Live progress of the Shorts pipeline, for the dashboard's "Reels" panel.

The pipeline runs as plain code (often in a separate ``main.py process`` process), so the only
shared channel is the database: each lane (``process``, ``upload``) keeps one kv row
``pipeline:live:<lane>`` that says what it is doing right now and when it last said so.

* :func:`start_run` / :func:`end_run` bracket one run of a lane.
* :func:`step` records the current stage, source/clip and ``done``/``total`` (e.g. windows scored).
* :func:`lanes` reads them back for the dashboard, marking a run ``quiet`` when it has not reported
  for :data:`QUIET_AFTER` (a long transcription, or a process that died without saying so).

Progress is best-effort: every writer swallows its own errors so it can never break the pipeline.
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import Any

import db

log = logging.getLogger(__name__)

KEY_PREFIX = "pipeline:live:"
LANES = ("process", "upload")
QUIET_AFTER = timedelta(minutes=10)
STAGE_LABELS = {
    "starting": "Starting",
    "fetch": "Downloading",
    "transcribe": "Transcribing",
    "score": "Scoring moments",
    "finalize": "Choosing cuts",
    "render": "Rendering",
    "upload": "Uploading",
    "finishing": "Finishing up",
}


def _key(lane: str) -> str:
    return f"{KEY_PREFIX}{lane}"


def _safe_write(lane: str, value: dict[str, Any]) -> None:
    try:
        db.kv_set(_key(lane), value)
    except Exception:  # progress must never break the pipeline
        log.debug("progress write failed for %s", lane, exc_info=True)


def _get(lane: str) -> dict[str, Any] | None:
    try:
        v = db.kv_get(_key(lane))
    except Exception:
        log.debug("progress read failed for %s", lane, exc_info=True)
        return None
    return v if isinstance(v, dict) else None


def start_run(lane: str) -> None:
    now = db.now_iso()
    _safe_write(lane, {"lane": lane, "running": True, "pid": os.getpid(), "started": now, "ts": now,
                       "stage": "starting", "source_id": None, "clip_id": None, "title": "", "detail": "",
                       "done": None, "total": None})


def step(lane: str, stage: str, *, source_id: int | None = None, clip_id: int | None = None,
         title: str = "", detail: str = "", done: int | None = None, total: int | None = None) -> None:
    """Record what ``lane`` is doing now (starts a run implicitly when none is open)."""
    cur = _get(lane) or {}
    if not cur.get("running"):
        cur = {"lane": lane, "running": True, "pid": os.getpid(), "started": db.now_iso()}
    cur.update({"ts": db.now_iso(), "stage": stage, "source_id": source_id, "clip_id": clip_id,
                "title": (title or "")[:120], "detail": (detail or "")[:160], "done": done, "total": total})
    _safe_write(lane, cur)


def end_run(lane: str, summary: dict[str, Any] | None = None, *, error: str = "") -> None:
    cur = _get(lane) or {"lane": lane, "started": db.now_iso()}
    cur.update({"running": False, "ts": db.now_iso(), "finished": db.now_iso(),
                "summary": {k: v for k, v in (summary or {}).items() if isinstance(v, (int, float, str))},
                "error": error[:300]})
    _safe_write(lane, cur)


def lanes() -> list[dict[str, Any]]:
    """[{lane, state (running|quiet|finished|idle), label, title, detail, done, total, pct, started, ts,
    finished, summary, error}] for every lane, in :data:`LANES` order."""
    now = db.utcnow()
    out: list[dict[str, Any]] = []
    for lane in LANES:
        cur = _get(lane)
        if not cur:
            out.append({"lane": lane, "state": "idle", "label": "Idle"})
            continue
        try:
            age = now - db.parse_iso(str(cur.get("ts")))
        except Exception:
            age = timedelta(0)
        if cur.get("running"):
            state = "quiet" if age > QUIET_AFTER else "running"
        else:
            state = "finished"
        done, total = cur.get("done"), cur.get("total")
        pct = int(100 * done / total) if isinstance(done, int) and isinstance(total, int) and total > 0 else None
        out.append({**cur, "state": state, "label": STAGE_LABELS.get(str(cur.get("stage")), str(cur.get("stage") or "")),
                    "pct": pct, "quiet_min": int(age.total_seconds() // 60)})
    return out
