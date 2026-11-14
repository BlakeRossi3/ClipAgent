"""Orchestrator: pipeline stages, scheduled jobs, disk hygiene and the command line.

Pipeline (plain code; Claude is only called inside ``pick`` and ``strategist``)::

    scout -> sources(candidate) -> fetch -> transcribe -> pick -> render -> upload
                                                                     \\-> analytics -> weekly review

* :func:`process_source` resumes one source from its current status and runs every remaining
  stage, then renders its clips and marks it ``done``.
* Stage errors are per item: ``LicenceRejected`` is permanent; ``FetchError`` /
  ``TranscribeError`` / ``PickError`` leave the source ``failed`` and
  :func:`retry_failed_sources` retries it at most :data:`MAX_SOURCE_RETRIES` times;
  ``RenderError`` fails one clip (re-rendered at most :data:`MAX_CLIP_RETRIES` times).
* ``budget.BudgetExceeded`` stops LLM work for the day (the source stays ``transcribed``);
  fetching, transcribing, rendering and uploading continue.
* :func:`upload_ready` stops the batch on ``UploadCapReached`` / ``QuotaExhausted`` /
  ``UploadAuthError`` and continues past other ``UploadError``\\ s.
* Scheduled jobs (:func:`build_scheduler`) are wrapped by :func:`run_job`: an exception
  becomes an ``error`` event (plus one ``job_error`` SMS per job per day) and never stops
  the scheduler. A lock file in the data dir keeps a second scheduler off the same data,
  and per-stage locks keep a CLI run from rendering/uploading the same clips concurrently.

Only this module configures logging handlers.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import IO, Any
from urllib.parse import urlparse

import budget
import db
import strategy as strategy_mod
from config import Settings, get_settings

log = logging.getLogger(__name__)

MODULE = "main"
MAX_SOURCE_RETRIES = 2                 # automatic retries of a failed source, then it stays failed
MAX_CLIP_RETRIES = 2                   # re-renders of a failed clip
RETRY_AFTER = timedelta(minutes=50)    # a failed source waits this long before its retry
DEFAULT_MAX_SOURCES = 3

# Pipeline order for picking work: finish what is closest to a Short first.
_WORK_ORDER = ("picked", "transcribed", "fetched", "candidate")


# ---------------------------------------------------------------------------
# locks
# ---------------------------------------------------------------------------

class Busy(RuntimeError):
    """Another process (or job) holds the lock for this stage."""


def _lock_path(name: str) -> Path:
    return get_settings().data_dir / f"{name}.lock"


_WIN_LOCK_OFFSET = 1 << 20


def _try_lock(fh: IO[str]) -> bool:
    """Non-blocking exclusive OS lock on an open file (released by the OS if the process dies).

    POSIX: ``fcntl.flock``. Windows: ``msvcrt.locking`` on one byte far past the pid text (a
    mandatory byte-range lock there would make the pid unreadable to the next process).
    Platform modules are imported here, never at module level, so importing ``main`` works anywhere.
    """
    if sys.platform == "win32":
        import msvcrt

        try:
            fh.seek(_WIN_LOCK_OFFSET)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(fh: IO[str]) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(_WIN_LOCK_OFFSET)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass  # closing the handle releases it anyway
    fh.close()


def _holder_pid(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()[:20] or "unknown"
    except OSError:
        return "unknown"


def acquire_lock(name: str) -> IO[str]:
    """Take an exclusive, non-blocking lock on ``<data_dir>/<name>.lock``; keep the handle open.

    Raises :class:`Busy` (with the holder's pid when known). The lock dies with the process.
    """
    path = _lock_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+", encoding="utf-8")
    if not _try_lock(fh):
        fh.close()
        holder = _holder_pid(path)
        raise Busy(f"{name} is locked by another process (pid {holder}); lock file {path}") from None
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


@contextmanager
def stage_lock(name: str) -> Iterator[None]:
    fh = acquire_lock(name)
    try:
        yield
    finally:
        _unlock(fh)


def lock_holder(name: str) -> str | None:
    """Pid text of the process holding ``name`` (None when free). Never blocks or steals it."""
    try:
        fh = acquire_lock(name)
    except Busy as exc:
        return str(exc).split("(pid ", 1)[-1].split(")", 1)[0]
    _unlock(fh)
    return None


# ---------------------------------------------------------------------------
# pipeline stages
# ---------------------------------------------------------------------------

def _clip_retries_left(clip_id: int) -> bool:
    return int(db.kv_get(f"main:clip_retries:{clip_id}", 0) or 0) < MAX_CLIP_RETRIES


def render_clips(source_id: int | None = None, strategy: strategy_mod.Strategy | None = None) -> list[int]:
    """Render ``picked`` clips (and ``failed`` ones with retries left). Returns rendered clip ids.

    A ``RenderError`` fails that clip only; the others still render.
    """
    import edit

    strat = strategy or strategy_mod.load_strategy()
    done: list[int] = []
    for clip in db.list_clips(["picked", "failed"], source_id=source_id):
        if clip.status == "failed":
            if not _clip_retries_left(clip.id):
                continue
            db.kv_incr(f"main:clip_retries:{clip.id}")
        try:
            edit.render_clip(clip.id, strat)
        except edit.RenderError as exc:
            log.warning("render of clip %s failed: %s", clip.id, exc)
            continue
        done.append(clip.id)
    return done


def _finish_source(source_id: int) -> None:
    """``picked`` -> ``done`` once none of its clips still needs rendering."""
    if db.require_source(source_id).status != "picked":
        return
    pending = [c for c in db.list_clips(["picked", "failed"], source_id=source_id)
               if c.status == "picked" or _clip_retries_left(c.id)]
    if not pending:
        db.set_source_status(source_id, "done", "all clips rendered")
        db.log_event(MODULE, f"source {source_id} done", data={"source_id": source_id})


def process_source(source_id: int, *, allow_llm: bool = True,
                   strategy: strategy_mod.Strategy | None = None) -> list[int]:
    """Fetch -> transcribe -> pick -> render one source, resuming from its current status.

    Returns the ids of clips rendered by this call. Raises ``fetch.LicenceRejected``,
    ``fetch.FetchError``, ``transcribe.TranscribeError``, ``pick.PickError`` (the source is
    ``rejected`` / ``failed``) and ``budget.BudgetExceeded`` (the source stays
    ``transcribed``). With ``allow_llm=False`` a source stops at ``transcribed``.
    """
    import fetch
    import pick
    import transcribe

    strat = strategy or strategy_mod.load_strategy()
    src = db.require_source(source_id)
    if src.status == "candidate":
        fetch.fetch_source(source_id, strat)
        src = db.require_source(source_id)
    if src.status == "fetched":
        transcribe.transcribe_source(source_id)
        src = db.require_source(source_id)
    if src.status == "transcribed":
        if not allow_llm:
            return []
        pick.pick_clips(source_id, strat)
        src = db.require_source(source_id)
    if src.status != "picked":
        return []
    rendered = render_clips(source_id, strat)
    _finish_source(source_id)
    return rendered


def _resume_status(src: db.Source) -> str:
    """The furthest stage a failed source can restart from, judged by what is on disk."""
    if src.transcript_path and Path(src.transcript_path).is_file():
        return "transcribed"
    if src.video_path and Path(src.video_path).is_file():
        return "fetched"
    return "candidate"


def retry_failed_sources() -> list[int]:
    """Give ``failed`` sources another go (at most :data:`MAX_SOURCE_RETRIES` each, spaced out)."""
    retried: list[int] = []
    now = db.utcnow()
    for src in db.list_sources("failed"):
        key = f"main:retries:{src.id}"
        n = int(db.kv_get(key, 0) or 0)
        if n >= MAX_SOURCE_RETRIES or now - db.parse_iso(src.updated_at) < RETRY_AFTER:
            continue
        target = _resume_status(src)
        db.kv_incr(key)
        db.set_source_status(src.id, target, f"retry {n + 1}/{MAX_SOURCE_RETRIES} after: {src.status_reason}")
        db.log_event(MODULE, f"retrying source {src.id} from {target} ({n + 1}/{MAX_SOURCE_RETRIES})",
                     data={"source_id": src.id, "previous_error": src.status_reason})
        retried.append(src.id)
    return retried


def _mark_failed(source_id: int, reason: str) -> None:
    """Fail a source left in a working status by an unexpected error, so the bounded
    :func:`retry_failed_sources` policy applies instead of an hourly (possibly paid) re-run."""
    try:
        src = db.require_source(source_id)
        # not ``picked``: a retry would resume at ``transcribed`` and pay for picking again
        if src.status in ("candidate", "fetched", "transcribed"):
            db.set_source_status(source_id, "failed", reason[:500])
    except Exception:  # never let the error handler mask the original problem
        log.exception("could not mark source %s failed", source_id)


def process_pending(max_sources: int = DEFAULT_MAX_SOURCES) -> dict[str, Any]:
    """Advance up to ``max_sources`` sources (closest to done first), then render stray clips."""
    import fetch
    import pick
    import transcribe

    with stage_lock("process"):
        strat = strategy_mod.load_strategy()
        counts: Counter[str] = Counter()
        counts["retried"] = len(retry_failed_sources())
        llm_ok = not budget.is_paused()
        by_status = {st: db.list_sources(st) for st in _WORK_ORDER}
        work = [s for st in _WORK_ORDER for s in by_status[st]]
        for src in work:
            if counts["processed"] >= max_sources:
                break
            if src.status == "transcribed" and not llm_ok:
                counts["waiting_for_budget"] += 1
                continue
            counts["processed"] += 1
            try:
                counts["clips_rendered"] += len(process_source(src.id, allow_llm=llm_ok, strategy=strat))
            except budget.BudgetExceeded as exc:
                llm_ok = False
                counts["budget_stopped"] = 1
                db.log_event(MODULE, f"LLM budget exhausted at source {src.id}; judgment paused until tomorrow",
                             level="warning", data={"source_id": src.id, "error": str(exc)})
            except pick.LLMUnavailable as exc:   # API outage/auth: not the source's fault
                llm_ok = False
                counts["llm_unavailable"] = 1
                db.log_event(MODULE, f"LLM unavailable at source {src.id}; judgment skipped this run",
                             level="warning", data={"source_id": src.id, "error": str(exc)[:300]})
            except db.InvalidTransition as exc:  # a concurrent run moved it on: skip this item
                counts["skipped_concurrent"] += 1
                log.info("source %s changed status concurrently: %s", src.id, exc)
            except fetch.LicenceRejected:
                counts["rejected"] += 1
            except (fetch.FetchError, transcribe.TranscribeError, pick.PickError) as exc:
                counts["failed"] += 1
                log.warning("source %s failed: %s", src.id, exc)
            except Exception as exc:  # an unexpected bug must not stop the other sources
                counts["failed"] += 1
                db.log_event(MODULE, f"source {src.id}: unexpected {type(exc).__name__}: {exc}", level="error",
                             data={"source_id": src.id, "traceback": traceback.format_exc()[-2000:]})
                _mark_failed(src.id, f"unexpected {type(exc).__name__}: {exc}")
        # clips picked outside this run (requeued, earlier render failures with retries left)
        counts["clips_rendered"] += len(render_clips(strategy=strat))
        for src in db.list_sources("picked"):
            _finish_source(src.id)
    return dict(counts)


def upload_ready(limit: int | None = None) -> list[int]:
    """Upload rendered clips (oldest first) into free publish slots. Returns new upload ids.

    Stops the batch on ``UploadCapReached`` / ``QuotaExhausted`` / ``UploadAuthError``;
    any other ``UploadError`` skips that clip.
    """
    import upload

    ids: list[int] = []
    with stage_lock("upload"):
        for clip in db.list_clips(["rendered", "scheduled"]):
            if limit is not None and len(ids) >= limit:
                break
            try:
                ids.append(upload.upload_clip(clip.id))
            except (upload.UploadCapReached, upload.UploadAuthError) as exc:   # QuotaExhausted included
                routine = type(exc) is upload.UploadCapReached                  # the daily cap, not a problem
                db.log_event(MODULE, f"upload batch stopped: {exc}", level="info" if routine else "warning",
                             data={"clip_id": clip.id, "reason": type(exc).__name__})
                break
            except upload.UploadError as exc:
                log.warning("upload of clip %s failed: %s", clip.id, exc)
            except db.InvalidTransition as exc:  # a concurrent run moved it on: skip this clip
                log.info("clip %s changed status concurrently: %s", clip.id, exc)
    return ids


def run_once(max_sources: int = DEFAULT_MAX_SOURCES) -> dict[str, Any]:
    """One full pass: scout (when the queue is short), process, upload. Returns counters."""
    import scout

    counters: dict[str, Any] = {"scouted": 0}
    if len(db.list_sources("candidate")) < max_sources:
        try:
            counters["scouted"] = len(scout.scout())
        except Exception as exc:
            db.log_event(MODULE, f"scout failed: {type(exc).__name__}: {exc}", level="error",
                         data={"traceback": traceback.format_exc()[-2000:]})
    counters.update(process_pending(max_sources))
    counters["uploaded"] = len(upload_ready())
    db.log_event(MODULE, "run_once finished", data=counters)
    return counters


# ---------------------------------------------------------------------------
# manual: one URL in, one Short out
# ---------------------------------------------------------------------------

_HOST_TYPES = (("nasa.gov", "nasa"), ("house.gov", "congress"), ("senate.gov", "congress"),
               ("congress.gov", "congress"), ("dvidshub.net", "dvids"), ("archives.gov", "nara"))


def _host_guess(url: str, enabled: list[str]) -> str:
    host = (urlparse(url).hostname or "").lower()
    for dom, st in _HOST_TYPES:
        if (host == dom or host.endswith("." + dom)) and st in enabled:
            return st
    return enabled[0]


def infer_source_type(url: str, strat: strategy_mod.Strategy) -> str:
    """The first enabled source type whose licence rules accept ``url`` (one metadata probe).

    Government types are tried before CC BY. When none passes, a host-based guess is
    returned so the licence gate records the rejection with its evidence.
    """
    import fetch

    order = sorted(strat.source_types, key=lambda st: st in strategy_mod.ANY_CHANNEL_SOURCE_TYPES)
    try:
        info = dict(fetch.extract_info(url, download=False) or {})
    except Exception as exc:
        log.info("probe of %s failed (%s); guessing the source type from the host", url, exc)
        return _host_guess(url, order)
    info.setdefault("webpage_url", url)
    for st in order:
        if fetch.check_licence(info, strat, st).ok:
            return st
    return _host_guess(url, order)


def manual(url: str, source_type: str | None = None, *, upload: bool = True) -> list[int]:
    """Clip one URL end to end: fetch, transcribe, pick, render and (optionally) upload.

    Re-running with the same URL resumes the existing source. Returns the source's clip ids.
    Raises the stage exceptions of :func:`process_source` (e.g. ``LicenceRejected``) and
    ``budget.BudgetExceeded``.
    """
    import upload as upload_mod

    strat = strategy_mod.load_strategy()
    existing = db.get_source_by_url(url)
    if existing is not None:
        sid = existing.id
        if existing.status == "rejected":
            import fetch
            raise fetch.LicenceRejected(f"source {sid} was rejected: {existing.status_reason}")
    else:
        st = source_type or infer_source_type(url, strat)
        if st not in strat.source_types:
            raise ValueError(f"source type {st!r} is not enabled in the strategy ({strat.source_types})")
        sid = db.add_source(url, st, topic="manual", metadata={"added_via": "manual"})
        assert sid is not None
        db.log_event(MODULE, f"manual source {sid} added", data={"source_id": sid, "url": url, "source_type": st})
    with stage_lock("process"):
        process_source(sid, strategy=strat)
        render_clips(sid, strat)
        _finish_source(sid)
    if upload:
        with stage_lock("upload"):
            for clip in db.list_clips(["rendered", "scheduled"], source_id=sid):
                try:
                    upload_mod.upload_clip(clip.id)
                except (upload_mod.UploadCapReached, upload_mod.UploadAuthError) as exc:
                    db.log_event(MODULE, f"manual upload stopped: {exc}", level="warning",
                                 data={"clip_id": clip.id})
                    break
                except upload_mod.UploadError as exc:
                    log.warning("upload of clip %s failed: %s", clip.id, exc)
    return [c.id for c in db.list_clips(source_id=sid)]


# ---------------------------------------------------------------------------
# DRY_RUN -> live
# ---------------------------------------------------------------------------

def requeue_dry_run() -> dict[str, list[int]]:
    """Make clips that were only "uploaded" in DRY_RUN eligible for a real upload.

    Their dry-run upload rows become ``superseded`` (freeing the publish slots and fake video
    ids they held) and
    the clip goes back to ``rendered`` - or to ``picked`` when the render file is gone, so it is
    re-rendered first. Clips with any real upload are never touched.
    """
    out: dict[str, list[int]] = {"rendered": [], "picked": []}
    with stage_lock("upload"):
        for clip in db.list_clips("uploaded"):
            ups = db.list_uploads(clip_id=clip.id)
            if any(not u.dry_run and u.status != "failed" for u in ups):
                continue
            dry = [u for u in ups if u.dry_run and u.status == "uploaded"]
            if not dry:
                continue
            target = "rendered" if clip.render_path and Path(clip.render_path).is_file() else "picked"
            with db.transaction() as conn:
                for u in dry:   # free the fake id: a later dry run of the same clip reuses dryrun-<clip_id>
                    db.update_upload(u.id, status="superseded", youtube_video_id=None, conn=conn,
                                     error=f"dry run {u.youtube_video_id} superseded by requeue-dry-run")
                if target == "picked":
                    db.update_clip(clip.id, render_path=None, conn=conn)
                # uploaded -> rendered/picked is not a normal transition: this is the deliberate repair path
                db.set_clip_status(clip.id, target, "requeued for a live upload", force=True, conn=conn)
            out[target].append(clip.id)
    db.log_event(MODULE, f"requeued {len(out['rendered']) + len(out['picked'])} dry-run clip(s)", data=out)
    return out


# ---------------------------------------------------------------------------
# disk hygiene
# ---------------------------------------------------------------------------

def _delete(path: Path) -> int:
    try:
        size = path.stat().st_size
        path.unlink()
        return size
    except FileNotFoundError:
        return 0


def _dry_run_only(clip_id: int) -> bool:
    ups = [u for u in db.list_uploads(clip_id=clip_id) if u.status in ("uploaded", "queued")]
    return bool(ups) and all(u.dry_run for u in ups)


def cleanup() -> dict[str, int]:
    """Delete media that is no longer needed. DB rows, info.json and transcripts are kept.

    * Source video/audio (and partial downloads) of ``done`` / ``rejected`` / ``failed`` sources
      untouched for ``SOURCE_MEDIA_RETENTION_DAYS``, unless a clip still needs them (not yet
      rendered, re-renderable, or only dry-run uploaded) or the source has retries left.
    * Renders whose live upload is older than ``RENDER_RETENTION_DAYS``.
    * Stale ``*.tmp.mp4`` files from interrupted renders.
    """
    s = get_settings()
    now = db.utcnow()
    freed = Counter[str]()
    src_cutoff = now - timedelta(days=s.source_media_retention_days)
    for src in db.list_sources(["done", "rejected", "failed"]):
        if db.parse_iso(src.updated_at) > src_cutoff:
            continue
        if src.status == "failed" and int(db.kv_get(f"main:retries:{src.id}", 0) or 0) < MAX_SOURCE_RETRIES:
            continue
        clips = db.list_clips(source_id=src.id)
        if any(c.status in ("picked", "rendered", "scheduled") or (c.status == "failed" and _clip_retries_left(c.id))
               or (c.status == "uploaded" and _dry_run_only(c.id)) for c in clips):
            continue
        folder = s.downloads_dir / str(src.id)
        files = [p for p in folder.iterdir() if p.is_file() and p.name != "info.json"] if folder.is_dir() else []
        if not files:
            continue
        freed["bytes"] += sum(_delete(p) for p in files)
        freed["sources"] += 1
        meta = dict(src.metadata) if isinstance(src.metadata, dict) else {}
        meta["media_deleted_at"] = db.now_iso()
        db.update_source(src.id, video_path=None, audio_path=None, metadata=meta)

    render_cutoff = db.to_iso(now - timedelta(days=s.render_retention_days))
    for clip in db.list_clips("uploaded"):
        if not clip.render_path:
            continue
        live = [u for u in db.list_uploads(clip_id=clip.id) if not u.dry_run and u.status in ("uploaded", "removed")]
        if not live or any((u.uploaded_at or "9999") > render_cutoff for u in live):
            continue
        render = Path(clip.render_path)
        freed["bytes"] += _delete(render) + _delete(render.with_suffix(".ass"))
        freed["renders"] += 1
        db.update_clip(clip.id, render_path=None)

    if s.renders_dir.is_dir():
        for tmp in s.renders_dir.glob("*.tmp.mp4"):
            if now.timestamp() - tmp.stat().st_mtime > 6 * 3600:
                freed["bytes"] += _delete(tmp)
    result = {"sources": freed["sources"], "renders": freed["renders"], "bytes": freed["bytes"]}
    db.log_event(MODULE, f"cleanup freed {freed['bytes'] / 1e6:.1f} MB", data=result)
    return result


# ---------------------------------------------------------------------------
# scheduler
# ---------------------------------------------------------------------------

def run_job(name: str, fn: Callable[[], Any]) -> Any:
    """Run a scheduled job: record its last run; an exception becomes an error event, never a crash."""
    t0 = time.monotonic()
    state: dict[str, Any] = {"started_at": db.now_iso()}
    try:
        result = fn()
    except Busy as exc:
        state.update(ok=True, skipped=str(exc))
        db.log_event(MODULE, f"job {name} skipped: stage busy", level="info", data={"reason": str(exc)})
        result = None
    except Exception as exc:
        state.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:500])
        db.log_event(MODULE, f"job {name} failed: {type(exc).__name__}: {exc}", level="error",
                     data={"job": name, "traceback": traceback.format_exc()[-4000:]})
        if db.kv_set_if_absent(f"main:job_alerted:{name}:{db.local_today().isoformat()}", True):
            db.add_notification("job_error", f"Shorts agent: the {name} job failed ({type(exc).__name__}). "
                                "Details are on the dashboard.", "sms")
        result = None
    else:
        state["ok"] = True
    state["seconds"] = round(time.monotonic() - t0, 1)
    db.kv_set(f"main:job:{name}", state)
    return result


def analytics_job() -> dict[str, Any]:
    """Daily analytics: per-video stats, channel stats and the takedown/claim check (isolated)."""
    import analytics

    out: dict[str, Any] = {}
    for step, fn in (("collect_stats", analytics.collect_stats), ("refresh_channel_stats",
                                                                   analytics.refresh_channel_stats),
                     ("check_claims", analytics.check_claims)):
        try:
            out[step] = fn()
        except Exception as exc:
            out[step] = f"failed: {exc}"
            db.log_event(MODULE, f"analytics step {step} failed: {type(exc).__name__}: {exc}", level="error",
                         data={"traceback": traceback.format_exc()[-4000:]})
    return out


@dataclass(frozen=True)
class JobSpec:
    id: str
    func: Callable[[], Any]
    trigger: str              # "cron" | "interval"
    args: dict[str, Any]
    description: str


def _weekly_improvement() -> Any:
    """Non-urgent: waits for the Claude plan's window to reset when usage is high."""
    from assistant import improve, usage

    if usage.should_defer("improve", "Run the weekly self-improvement review (assistant.improve).") is not None:
        return None
    return improve.weekly_improvement()


def job_specs() -> list[JobSpec]:
    """Jobs and their local times (``settings.timezone``), off the hour on purpose."""
    import analytics  # noqa: F401  (import errors surface at startup, not at 07:25)
    import notify
    import scout
    import strategist

    return [
        JobSpec("scout", lambda: scout.scout(), "cron", {"hour": 6, "minute": 10}, "find candidate sources"),
        JobSpec("process", process_pending, "cron", {"minute": 20}, "fetch/transcribe/pick/render"),
        JobSpec("upload", upload_ready, "cron", {"minute": 40}, "upload rendered clips into slots"),
        JobSpec("send_queued", notify.send_queued, "interval", {"minutes": 5}, "deliver queued calls/SMS"),
        JobSpec("expire_decisions", db.expire_decisions, "cron", {"minute": 50}, "expire unanswered approvals"),
        JobSpec("analytics", analytics_job, "cron", {"hour": 7, "minute": 25}, "stats, channel stats, claims"),
        JobSpec("cleanup", cleanup, "cron", {"hour": 3, "minute": 35}, "delete old media"),
        JobSpec("daily_summary", notify.daily_summary, "cron", {"hour": 20, "minute": 55}, "evening call"),
        JobSpec("weekly_review", strategist.weekly_review, "cron", {"day_of_week": "sun", "hour": 18, "minute": 5},
                "Sonnet strategy review"),
    ] + assistant_job_specs()


def assistant_job_specs() -> list[JobSpec]:
    """The personal assistant's jobs - only when ASSISTANT_ENABLED."""
    from assistant.settings import get_assistant_settings

    if not get_assistant_settings().enabled:
        return []
    from assistant import checkins, runtime

    def tidy() -> Any:
        return runtime.run_task("archivist", "Weekly tidy of the memory vault: follow the memory-curation skill. "
                                "Merge duplicates, file inbox items, flag contradictions. Report briefly.").status

    return [
        JobSpec("assistant_plan_startup", checkins.plan_day, "date", {}, "plan today's check-ins at startup"),
        JobSpec("assistant_plan", checkins.plan_day, "cron", {"hour": 0, "minute": 7}, "plan the day's check-ins"),
        JobSpec("assistant_checkins", checkins.send_due, "interval", {"minutes": 5}, "send due check-ins"),
        JobSpec("assistant_tidy", tidy, "cron", {"day_of_week": "sun", "hour": 4, "minute": 15},
                "archivist memory tidy"),
        JobSpec("assistant_improve", _weekly_improvement,
                "cron", {"day_of_week": "sun", "hour": 19, "minute": 25}, "weekly self-improvement proposals"),
        JobSpec("assistant_deferred", lambda: __import__("assistant.usage", fromlist=["x"]).run_due(),
                "interval", {"minutes": 10}, "run prompts queued for after the Claude plan resets"),
        JobSpec("assistant_evals", lambda: __import__("assistant.evals", fromlist=["x"]).run_evals_isolated(),
                "cron", {"hour": 2, "minute": 45}, "nightly assistant evals"),
        JobSpec("assistant_history", lambda: __import__("assistant.history_import", fromlist=["x"]).nightly(),
                "cron", {"hour": 1, "minute": 37}, "distil the next batch of imported Claude chats"),
        JobSpec("assistant_reels_cleanup", lambda: __import__("assistant.reels", fromlist=["x"]).cleanup(),
                "cron", {"hour": 3, "minute": 50}, "delete old downloaded reels"),
    ] + ([JobSpec("assistant_heartbeat", lambda: __import__("assistant.heartbeat", fromlist=["x"]).tick()["status"],
                  "interval", {"minutes": get_assistant_settings().heartbeat_minutes},
                  "heartbeat: check Agent/Heartbeat.md against the live status")]
         if get_assistant_settings().heartbeat_minutes > 0 else [])


def build_scheduler() -> Any:
    """APScheduler ``BackgroundScheduler`` with every job (not started)."""
    from apscheduler.schedulers.background import BackgroundScheduler

    sched = BackgroundScheduler(timezone=get_settings().timezone,
                                job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 30 * 60})
    for spec in job_specs():
        sched.add_job(run_job, spec.trigger, id=spec.id, name=spec.description, args=[spec.id, spec.func],
                      replace_existing=True, **spec.args)
    return sched


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def status_text() -> str:
    """One screen: pipeline, caps, spend, decisions, jobs, upcoming publishes."""
    import youtube_auth

    s = get_settings()
    tz = db.local_tz()
    now = db.utcnow()
    lines = [f"Auto Shorts Agent - {db.local_now():%Y-%m-%d %H:%M %Z}"
             f"   DRY_RUN={'on' if s.dry_run else 'OFF (live)'}   FAKE_LLM={'on' if s.fake_llm else 'off'}"
             + ("   LLM PAUSED" if db.kv_get("budget:paused") else "")]
    src = db.count_sources_by_status()
    lines.append("Sources: " + "  ".join(f"{st} {src.get(st, 0)}" for st in db.SOURCE_STATUSES))
    lines.append("Clips:   " + "  ".join(f"{st} {db.count_clips(status=st)}" for st in db.CLIP_STATUSES))
    lines.append(f"Uploads today: {db.count_uploads_on()}/{strategy_mod.upload_cap()}"
                 f"   YouTube quota (PT day): {youtube_auth.quota_used()}/{youtube_auth.daily_quota_limit()}")
    lines.append(f"LLM spend today: ${budget.spent_today():.4f} of ${budget.daily_cap_usd():.2f}")
    pending = db.list_decisions("pending")
    lines.append(f"Pending approvals: {len(pending)}")
    lines += [f"  #{d.id} {d.kind}: {d.summary[:90]}" for d in pending[:5]]
    lines.append("Jobs (last run):")
    for spec in job_specs():
        st = db.kv_get(f"main:job:{spec.id}")
        if not st:
            lines.append(f"  {spec.id:<17} never")
            continue
        when = db.parse_iso(st["started_at"]).astimezone(tz).strftime("%m-%d %H:%M")
        outcome = "ok" if st.get("ok") else f"FAILED {st.get('error', '')[:60]}"
        if st.get("skipped"):
            outcome = "skipped (busy)"
        lines.append(f"  {spec.id:<17} {when}  {outcome}  ({st.get('seconds', 0)}s)")
    holder = lock_holder("scheduler")
    lines.append(f"Scheduler: {'running (pid ' + holder + ')' if holder else 'not running'}")
    upcoming = db.publish_times_between(db.now_iso(), db.to_iso(now + timedelta(days=7)))
    lines.append("Next publishes: " + (", ".join(db.parse_iso(t).astimezone(tz).strftime("%a %H:%M")
                                                for t in upcoming[:6]) or "none scheduled"))
    errors = db.list_events(level="error", since=db.local_day_bounds()[0], limit=200)
    lines.append(f"Errors today: {len(errors)}" + (f" (latest: {errors[0].module}: {errors[0].message[:70]})"
                                                    if errors else ""))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Check:
    name: str
    status: str        # pass | warn | fail
    detail: str
    fix: str = ""


NETWORK_HOSTS = ("www.youtube.com", "images-api.nasa.gov", "huggingface.co", "api.anthropic.com",
                 "oauth2.googleapis.com", "www.googleapis.com", "youtubeanalytics.googleapis.com", "api.twilio.com")


def _ffmpeg_fix() -> str:
    if sys.platform == "win32":
        return "re-run ClipAgent-Setup.bat (installs the full Gyan.FFmpeg build with winget)"
    return "sudo apt install ffmpeg (or set FFMPEG_BIN)"


def _ffmpeg_checks(s: Settings) -> list[Check]:
    import edit

    ff = shutil.which(s.ffmpeg_bin) or (s.ffmpeg_bin if Path(s.ffmpeg_bin).is_file() else None)
    if not ff:
        return [Check("ffmpeg", "fail", f"{s.ffmpeg_bin!r} not found", _ffmpeg_fix())]
    out = [Check("ffmpeg", "pass", ff)]
    probe = edit.ffprobe_bin(s)
    out.append(Check("ffprobe", "pass", shutil.which(probe) or probe) if shutil.which(probe) or Path(probe).is_file()
               else Check("ffprobe", "fail", "not found", _ffmpeg_fix()))

    def listing(flag: str) -> str:
        try:
            return subprocess.run([ff, "-hide_banner", flag], capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            return ""

    filters, encoders = listing("-filters"), listing("-encoders")
    out.append(Check("ffmpeg libass (captions)", "pass" if " ass " in filters else "fail",
                     "ass filter available" if " ass " in filters else "ffmpeg built without libass",
                     "install a full ffmpeg build (Ubuntu's ffmpeg package and Gyan.FFmpeg have libass)"))
    out.append(Check("ffmpeg libx264 + aac", "pass" if "libx264" in encoders and " aac " in encoders else "fail",
                     "encoders available" if "libx264" in encoders else "libx264 encoder missing",
                     "install a full ffmpeg build (Ubuntu's ffmpeg package has libx264)"))
    return out


def _font_check(strat: strategy_mod.Strategy | None) -> Check:
    import edit

    wanted = (strat or {}).get("caption_style", {}).get("font") or edit.DEFAULT_CAPTION_STYLE["font"]
    if shutil.which("fc-list") is None and sys.platform != "win32":  # Windows: edit scans the Fonts folders
        return Check("caption font", "warn", f"fontconfig not installed; cannot verify {wanted!r}",
                     "sudo apt install fontconfig fonts-dejavu-core")
    got = edit.resolve_font(wanted)
    if got.lower() == wanted.lower():
        return Check("caption font", "pass", got)
    if got.lower() in {f.lower() for f in edit.FONT_FALLBACKS}:
        return Check("caption font", "warn", f"{wanted!r} not installed; captions use {got!r}",
                     f"put {wanted.replace(' ', '-')}.ttf in assets/fonts/ (or install the font family)")
    return Check("caption font", "fail", f"neither {wanted!r} nor any fallback font is installed",
                 "add a TTF to assets/fonts/" + ("" if sys.platform == "win32" else " (or sudo apt install fonts-dejavu-core)"))


def _youtube_checks(s: Settings) -> list[Check]:
    import youtube_auth

    sev = "warn" if s.dry_run else "fail"
    out = [Check("YouTube client secret", "pass", str(s.youtube_client_secrets))
           if Path(s.youtube_client_secrets).is_file() else
           Check("YouTube client secret", sev, f"{s.youtube_client_secrets} missing",
                 "download the Desktop-app OAuth client JSON (docs/YOUTUBE_SETUP.md)")]
    tp = Path(s.youtube_token_path)
    if not tp.is_file():
        out.append(Check("YouTube token", sev, f"{tp} missing", "run: .venv/bin/python youtube_auth.py"))
        return out
    try:
        info = json.loads(tp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        out.append(Check("YouTube token", "fail", f"{tp} unreadable", "re-run: .venv/bin/python youtube_auth.py"))
        return out
    granted = set(info.get("scopes") or [])
    missing = [x.rsplit("/", 1)[-1] for x in youtube_auth.SCOPES if x not in granted]
    out.append(Check("YouTube token scopes", "fail" if missing else "pass",
                     f"missing {missing}" if missing else ", ".join(sorted(x.rsplit('/', 1)[-1] for x in granted)),
                     "re-run: .venv/bin/python youtube_auth.py"))
    if s.youtube_monetary_analytics and youtube_auth.SCOPE_MONETARY not in granted:
        out.append(Check("YouTube revenue scope", "warn", "YOUTUBE_MONETARY_ANALYTICS=true but token lacks it",
                         "re-run: .venv/bin/python youtube_auth.py --monetary"))
    if not info.get("refresh_token"):
        out.append(Check("YouTube refresh token", "fail", "token has no refresh_token",
                         "revoke the app at myaccount.google.com/permissions, re-run youtube_auth.py"))
    return out


def _whisper_check(s: Settings) -> Check:
    try:
        from faster_whisper.utils import download_model
    except ImportError:
        return Check("whisper model", "fail", "faster-whisper not installed", ".venv/bin/pip install -r requirements.txt")
    try:
        path = download_model(s.whisper_model, local_files_only=True)
    except Exception:
        return Check("whisper model", "warn", f"{s.whisper_model} not cached yet",
                     "the first transcription downloads it from huggingface.co (~500 MB for small.en)")
    return Check("whisper model", "pass", f"{s.whisper_model} cached at {path}")


def _network_checks() -> list[Check]:
    import httpx

    out = []
    for host in NETWORK_HOSTS:
        try:
            r = httpx.get(f"https://{host}/", timeout=10, follow_redirects=False)
            out.append(Check(f"reach {host}", "pass", f"HTTP {r.status_code}"))
        except httpx.HTTPError as exc:
            out.append(Check(f"reach {host}", "fail", f"{type(exc).__name__}: {exc}"[:120],
                             "check DNS / firewall / outbound HTTPS from this server"))
    return out


def doctor_checks(*, network: bool = False) -> list[Check]:
    s = get_settings()
    checks: list[Check] = []
    py = sys.version_info
    checks.append(Check("python", "pass" if py >= (3, 12) else "fail", platform.python_version(),
                        "install Python 3.12+ and recreate .venv"))
    checks += _ffmpeg_checks(s)

    strat: strategy_mod.Strategy | None = None
    try:
        strat = strategy_mod.load_strategy()
        checks.append(Check("strategy.json", "pass", f"version {strat.version}, {len(strat.topics)} topics"))
    except strategy_mod.StrategyError as exc:
        checks.append(Check("strategy.json", "fail", "; ".join(exc.errors)[:300], f"fix {s.strategy_path}"))
    except (OSError, ValueError) as exc:
        checks.append(Check("strategy.json", "fail", str(exc)[:200], f"restore {s.strategy_path} from git"))
    if strat is not None:
        inert = [f"{st}: {ch.get('name')}" for st, entry in strat["allowlist"].items() if not st.startswith("_")
                 for ch in entry.get("channels", []) if not ch.get("channel_id") and not ch.get("handle")]
        checks.append(Check("allowlist channel ids", "warn" if inert else "pass",
                            ("no channel_id/handle, so never matched: " + "; ".join(inert))[:300] if inert
                            else "every allowlisted channel has a channel_id or handle",
                            "verify each official channel (linked from its .gov site) and add its channel_id "
                            "to strategy.json - display names are never trusted"))
    checks.append(_font_check(strat))

    try:
        s.ensure_dirs()
        with tempfile.NamedTemporaryFile(dir=s.data_dir):
            pass
        free_gb = shutil.disk_usage(s.data_dir).free / 1e9
        checks.append(Check("data dir", "pass" if free_gb >= 5 else "warn", f"{s.data_dir} ({free_gb:.1f} GB free)",
                            "free disk space: sources are large (lower SOURCE_MEDIA_RETENTION_DAYS)"))
    except OSError as exc:
        checks.append(Check("data dir", "fail", f"{s.data_dir}: {exc}", "fix permissions or CLIPAGENT_DATA_DIR"))

    version = db.schema_version()
    checks.append(Check("database schema", "pass" if version == db.SCHEMA_VERSION else "warn",
                        f"version {version} of {db.SCHEMA_VERSION} at {s.db_path}",
                        "run: .venv/bin/python main.py init"))

    if s.fake_llm:
        checks.append(Check("Anthropic", "warn", "no API key (FAKE_LLM=true): the Shorts pipeline cannot pick "
                            "clips by itself (optional; the assistant runs on the Claude account)",
                            "set ANTHROPIC_API_KEY and FAKE_LLM=false in config.env"))
    else:
        checks.append(Check("Anthropic", "pass" if s.anthropic_api_key else "fail",
                            "API key configured" if s.anthropic_api_key else "ANTHROPIC_API_KEY missing",
                            "set ANTHROPIC_API_KEY in config.env"))
    checks += _youtube_checks(s)

    sev = "warn" if s.dry_run else "fail"
    twilio_parts = {"TWILIO_ACCOUNT_SID": s.twilio_account_sid, "TWILIO_AUTH_TOKEN": s.twilio_auth_token,
                    "TWILIO_FROM_NUMBER": s.twilio_from_number}
    missing = [k for k, v in twilio_parts.items() if not v]
    checks.append(Check("Twilio", "pass" if not missing else sev,
                        "configured" if not missing else f"missing {missing}: calls/SMS are only logged",
                        "see docs/TWILIO_SETUP.md"))
    checks.append(Check("owner phone", "pass" if s.owner_phone else sev, s.owner_phone or "OWNER_PHONE missing",
                        "set OWNER_PHONE (E.164, e.g. +15551234567)"))
    base = urlparse(s.public_base_url)
    public_ok = base.scheme == "https" and base.hostname not in ("localhost", "127.0.0.1")
    checks.append(Check("PUBLIC_BASE_URL", "pass" if public_ok else sev, s.public_base_url,
                        "set PUBLIC_BASE_URL to the HTTPS address of this server (deploy/README.md)"))
    tok = s.dashboard_token or ""
    if not tok:
        tok_status, tok_detail = ("fail" if public_ok else "warn"), "dashboard has no password"
    elif len(tok) < 20:
        tok_status, tok_detail = "warn", f"only {len(tok)} characters"
    else:
        tok_status, tok_detail = "pass", "set"
    checks.append(Check("DASHBOARD_TOKEN", tok_status, tok_detail,
                        "set DASHBOARD_TOKEN in config.env to a random string of 20+ characters "
                        "(python -c 'import secrets; print(secrets.token_urlsafe(32))')"))
    if s.dashboard_allow_unauthenticated:
        checks.append(Check("DASHBOARD_ALLOW_UNAUTHENTICATED", "fail" if public_ok else "warn",
                            "true: anyone who reaches the dashboard can approve changes",
                            "set DASHBOARD_ALLOW_UNAUTHENTICATED=false"))
    checks.append(Check("DRY_RUN", "warn" if s.dry_run else "pass",
                        "on: nothing is uploaded, called or texted" if s.dry_run else "off: live",
                        "set DRY_RUN=false when the go-live checklist is done (README)"))
    checks.append(_whisper_check(s))
    checks += assistant_checks(s, network=network)
    if network:
        checks += _network_checks()
    return checks


def claude_code_checks(*, network: bool = False) -> list[Check]:
    """The assistant on the owner's Claude subscription (assistant/claude_code.py)."""
    from assistant import claude_code

    st = claude_code.status(probe=network)
    fix = "open Settings > The brain and press Sign in with Claude"
    out = [Check("Claude Code", "pass" if st["installed"] else "fail",
                 (st["path"] or "not installed") + (f" ({st['version']})" if st.get("version") else ""), fix)]
    if network and st["installed"]:
        out.append(Check("Claude account", "pass" if st.get("logged_in") else "fail",
                         "signed in" if st.get("logged_in") else (st.get("error") or "not signed in"), fix))
    last = st.get("last_error") or {}
    if isinstance(last, dict) and last.get("kind") == "auth":
        out.append(Check("Claude account", "fail", "the last run was refused: sign-in needs renewing", fix))
    out.append(Check("assistant models", "pass", f"Claude plan: main {st['model_in_use']}, sub-agents "
                     f"{st['sub_model_in_use']}"))
    used, cap = st["turns_today"], st["turns_cap"]
    out.append(Check("Claude runs today", "warn" if used >= cap else "pass", f"{used} of {cap}",
                     "raise ASSISTANT_MAX_TURNS_PER_DAY (shares your Claude plan's limits)"))
    if st.get("backoff_until"):
        out.append(Check("Claude plan limit", "warn", f"reached; assistant back {st['backoff_text']}",
                         "wait for the reset, or use the API key backend (ASSISTANT_BACKEND=api)"))
    return out


def assistant_checks(s: Settings, *, network: bool = False) -> list[Check]:
    """Doctor section for the personal assistant."""
    from assistant import registry, store, tools
    from assistant.settings import get_assistant_settings

    a = get_assistant_settings()
    out = [Check("assistant", "pass", "enabled" if a.enabled else "disabled (ASSISTANT_ENABLED=false)")]
    if not a.enabled:
        return out
    if a.backend == "claude_code" and not a.fake:
        out += claude_code_checks(network=network)
    else:
        out.append(Check("assistant models", "pass", f"main {a.model} ({a.effort}), sub-agents {a.subagent_model} "
                         f"({a.subagent_effort}){', FAKE' if a.fake else ''}"))
    out.append(Check("assistant budget", "pass" if a.daily_budget_usd > 0 else "warn",
                     f"${a.daily_budget_usd:.2f}/day, ${a.max_turn_usd:.2f}/turn, {a.max_sms_per_day} texts/day "
                     f"(separate from the pipeline's ${budget.daily_cap_usd():.2f})",
                     "set ASSISTANT_DAILY_BUDGET_USD"))
    vault_ok = a.vault_dir.is_dir()
    ignored = True
    try:
        rel = a.vault_dir.resolve().relative_to(Path(__file__).resolve().parent)
        ignored = subprocess.run(["git", "check-ignore", "-q", str(rel)], cwd=Path(__file__).resolve().parent,
                                 capture_output=True).returncode == 0
    except ValueError:
        pass  # outside the repository: cannot be committed
    except OSError:
        pass
    out.append(Check("vault", "pass" if vault_ok and ignored else ("fail" if not ignored else "warn"),
                     f"{a.vault_dir}" + ("" if vault_ok else " (not created yet)")
                     + ("" if ignored else " is NOT git-ignored"),
                     "move VAULT_DIR outside the repo or into an ignored folder; it is created on first use"))
    errs = registry.validate_all(tools.known_tool_names())
    out.append(Check("agents/skills", "fail" if errs else "pass", "; ".join(errs)[:300] or "registry validates",
                     "run: python main.py assistant validate"))
    rated = [j for j in store.list_jobs() if j.hourly_rate > 0]
    out.append(Check("pay rate", "pass" if rated else "warn",
                     ", ".join(f"{j.name} ${j.hourly_rate:.2f}/h" for j in rated) or "not set: earnings unknown",
                     "python main.py assistant job --rate <usd> (or answer the assistant's question)"))
    out.append(Check("owner phone (assistant)", "pass" if s.owner_phone else "fail",
                     s.owner_phone or "OWNER_PHONE missing", "set OWNER_PHONE"))
    return out


def assistant_cli(args: argparse.Namespace) -> int:
    from assistant import checkins, ledger, registry, runtime, store, tools, vault
    from assistant.settings import get_assistant_settings

    a = get_assistant_settings()
    store.init_store()
    if args.acmd in ("evals", "improve"):
        import importlib

        return int(importlib.import_module(f"assistant.{args.acmd}").main(args.rest) or 0)
    if args.acmd == "heartbeat":
        from assistant import heartbeat

        return heartbeat.main_cli(now=args.now)
    if args.acmd == "validate":
        errs = registry.validate_all(tools.known_tool_names())
        skills = registry.load_skills(strict=False)
        agents = registry.load_agents(skills=skills, strict=False)
        print("\n".join(errs) if errs else f"ok: {len(agents)} agents, {len(skills)} skills, "
              f"{len(tools.available(sorted(tools.known_tool_names())))} tools installed")
        return 1 if errs else 0
    if args.acmd == "reindex":
        vault.ensure_vault()
        print(f"reindexed: {vault.reindex()}")
    elif args.acmd == "status":
        print(f"assistant {'ENABLED' if a.enabled else 'disabled'}  name {a.name}  fake {a.fake}  vault {a.vault_dir}")
        print(f"spend today ${store.assistant_spent_today():.4f} of ${a.daily_budget_usd:.2f}   "
              f"texts today {tools.outbound_sms_today()}/{a.max_sms_per_day}")
        s = ledger.summary(weeks=4)
        print("money: " + (ledger.brief_line(s) or "no job set"))
        if s["missing"]:
            print("missing: " + "; ".join(s["missing"]))
        open_c = store.open_checkin()
        print(f"open check-in: #{open_c.id} {open_c.kind}: {open_c.question}" if open_c else "open check-in: none")
    elif args.acmd == "chat":
        res = runtime.run_turn(" ".join(args.message), channel="dashboard")
        print(res.text)
        print(f"[{res.status}, ${res.cost_usd:.4f}, tools: {[c.name for c in res.tool_calls]}]")
    elif args.acmd == "hours":
        res = ledger.log_hours(args.hours, week=args.week, job=args.job, source="cli")
        print(ledger.confirmation(res, a.owner_honorific))
    elif args.acmd == "job":
        job = ledger.set_job(args.name, hourly_rate=args.rate, pay_schedule=args.schedule, est_tax_rate=args.tax)
        print(f"job {job.name}: ${job.hourly_rate:.2f}/h, paid {job.pay_schedule}, tax set-aside "
              f"{job.est_tax_rate:.0%}")
    elif args.acmd == "plan":
        new = checkins.plan_day()
        print(f"{db.local_today()}: {len(new)} newly planned (routine: {checkins.ROUTINE_NOTE} in the vault)")
        print("\n".join(checkins.status_lines()))
    return 0


def format_checks(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    lines = []
    for c in checks:
        lines.append(f"[{c.status.upper():<4}] {c.name:<{width}}  {c.detail}")
        if c.status != "pass" and c.fix:
            lines.append(f"       {'':<{width}}  fix: {c.fix}")
    tally = Counter(c.status for c in checks)
    lines.append(f"\n{tally['pass']} passed, {tally['warn']} warnings, {tally['fail']} failed")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# servers
# ---------------------------------------------------------------------------

def serve(host: str, port: int) -> None:
    import uvicorn

    import server

    uvicorn.run(server.create_app(), host=host, port=port, log_level=get_settings().log_level.lower(),
                proxy_headers=True, forwarded_allow_ips="127.0.0.1")


# In-process control for the web Settings page (Restart / Stop / after an update). Only set
# while ``schedule`` runs; ``serve`` has no scheduler to restart.
_control: dict[str, Any] = {"server": None, "restart": False}


def control_available() -> bool:
    return _control["server"] is not None


def request_shutdown(*, restart: bool = False) -> bool:
    """Ask the running ``schedule`` process to stop gracefully (and start again when ``restart``)."""
    srv = _control["server"]
    if srv is None:
        return False
    _control["restart"] = restart
    srv.should_exit = True
    return True


def _install_stop_signals() -> list[int]:
    """Make SIGTERM and, on Windows, SIGBREAK (Ctrl+Break, console window closed) behave like Ctrl+C.

    uvicorn handles SIGINT/SIGTERM (and SIGBREAK) itself, then re-raises the signal once it has
    shut down; the default handler for SIGTERM/SIGBREAK would kill the process abruptly, so they are
    mapped to KeyboardInterrupt, which :func:`schedule` turns into a clean exit.
    """
    done = []
    for name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, signal.default_int_handler)
            done.append(int(sig))
    return done


def _start_selfedit_check(port: int) -> None:
    """After a restart caused by the assistant editing its own code: verify /healthz, else roll back."""
    try:
        from assistant import selfedit

        if db.kv_get(selfedit.KV_PENDING) is None:
            return
        threading.Thread(target=selfedit.verify_after_restart, args=(port,), name="selfedit-verify",
                         daemon=True).start()
    except Exception as exc:  # the guard must never stop the app from starting
        log.warning("self-edit check not started: %s", exc)


def _relaunch() -> None:
    """Start this same command again (after Restart / an update). Never returns on POSIX."""
    argv = [sys.executable, *sys.argv]
    if sys.platform == "win32":
        # os.execv on Windows does not quote arguments with spaces; start a fresh process instead.
        subprocess.Popen(argv, close_fds=True)
        return
    os.execv(sys.executable, argv)


def schedule(host: str, port: int) -> int:
    """Scheduler + web server in one process until Ctrl+C / SIGTERM / window close / Stop button."""
    import uvicorn

    import server

    try:
        lock = acquire_lock("scheduler")
    except Busy as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 1
    db.init_db()
    sched = build_scheduler()
    sched.start()
    db.log_event(MODULE, "scheduler started", data={"jobs": [j.id for j in sched.get_jobs()], "pid": os.getpid()})
    srv = uvicorn.Server(uvicorn.Config(server.create_app(), host=host, port=port, proxy_headers=True,
                                        forwarded_allow_ips="127.0.0.1",
                                        log_level=get_settings().log_level.lower()))
    _install_stop_signals()
    _control.update(server=srv, restart=False)
    _start_selfedit_check(port)
    try:  # the always-on voice loop (VOICE_ENABLED) runs as its own process; see voice/web.py
        from voice import web as voice_web

        voice_web.start_child()
    except Exception as exc:
        log.warning("voice not started: %s", exc)
    if __name__ == "__main__":  # `python main.py`: the web pages `import main`, a second module copy
        import main as importable

        importable._control = _control
    try:
        srv.run()
    except KeyboardInterrupt:
        pass
    finally:
        _control["server"] = None
        try:
            from voice import web as voice_web

            voice_web.stop_child()
        except Exception:
            pass
        try:
            sched.shutdown(wait=False)
            db.log_event(MODULE, "scheduler stopped", data={"pid": os.getpid(), "restart": _control["restart"]})
        except KeyboardInterrupt:  # a second Ctrl+C while stopping: just finish quietly
            pass
        _unlock(lock)
    if _control["restart"]:
        print("restarting ...", flush=True)
        _relaunch()
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def configure_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    for noisy in ("googleapiclient.discovery_cache", "httpx", "httpcore", "faster_whisper", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="main.py", description="Auto Shorts Agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create data dirs + database, record the initial strategy version")
    p = sub.add_parser("manual", help="clip one URL end to end (fetch, transcribe, pick, render, upload)")
    p.add_argument("url")
    p.add_argument("--source-type", choices=sorted(strategy_mod.KNOWN_SOURCE_TYPES))
    p.add_argument("--no-upload", action="store_true")
    sub.add_parser("scout", help="search for new candidate sources")
    p = sub.add_parser("process", help="advance pending sources and render their clips")
    p.add_argument("--limit", type=int, default=DEFAULT_MAX_SOURCES)
    p = sub.add_parser("run-once", help="scout + process + upload once")
    p.add_argument("--max-sources", type=int, default=DEFAULT_MAX_SOURCES)
    p = sub.add_parser("upload", help="upload rendered clips into free publish slots")
    p.add_argument("--limit", type=int)
    p = sub.add_parser("analytics", help="collect YouTube stats (+ channel stats, claim check)")
    p.add_argument("--day", type=date.fromisoformat, help="YYYY-MM-DD (default: today - 2)")
    sub.add_parser("review", help="run the weekly strategy review now")
    sub.add_parser("summary", help="compose and send the daily summary now")
    for name in ("serve", "schedule"):
        p = sub.add_parser(name, help="web server only" if name == "serve" else "scheduler + web server")
        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--port", type=int, default=8000)
    sub.add_parser("status", help="one-screen summary")
    p = sub.add_parser("doctor", help="preflight checks for this machine")
    p.add_argument("--network", action="store_true", help="also check outbound HTTPS to the services used")
    p = sub.add_parser("requeue-dry-run", help="make dry-run 'uploads' eligible for a real upload (go-live)")
    p.add_argument("--force", action="store_true", help="run even though DRY_RUN is still true")
    sub.add_parser("cleanup", help="delete old downloaded media and uploaded renders now")
    for name in ("approve", "reject"):
        p = sub.add_parser(name, help=f"{name} a pending decision")
        p.add_argument("decision_id", type=int)
    pa = sub.add_parser("assistant", help="the personal assistant (chat, hours, job, plan, status...)")
    asub = pa.add_subparsers(dest="acmd", required=True)
    p = asub.add_parser("chat", help="send the assistant a message (dashboard channel)")
    p.add_argument("message", nargs="+")
    asub.add_parser("status", help="assistant spend, texts, money, open check-in")
    asub.add_parser("validate", help="validate agents/*.md and skills/*/SKILL.md")
    asub.add_parser("reindex", help="rebuild the vault search index")
    p = asub.add_parser("hours", help="log hours worked this week (or --week)")
    p.add_argument("hours", type=float)
    p.add_argument("--week", type=date.fromisoformat, help="any date in that week (YYYY-MM-DD)")
    p.add_argument("--job")
    p = asub.add_parser("job", help="set a job's hourly rate / pay schedule / tax set-aside")
    p.add_argument("--rate", type=float, required=True, help="USD per hour before tax")
    p.add_argument("--name", default="Job")
    p.add_argument("--schedule", choices=["weekly", "biweekly", "semimonthly", "monthly"])
    p.add_argument("--tax", type=float, help="tax set-aside, e.g. 0.2 or 20")
    asub.add_parser("plan", help="plan and show today's check-ins")
    p = asub.add_parser("heartbeat", help="heartbeat status (--now: run one tick now)")
    p.add_argument("--now", action="store_true", help="run one heartbeat now, even if nothing changed")
    for name in ("evals", "improve"):
        p = asub.add_parser(name, help=f"pass-through to assistant.{name} (see its --help)", add_help=False)
        p.add_argument("rest", nargs=argparse.REMAINDER)
    return ap


def main(argv: list[str] | None = None) -> int:
    import fetch
    import pick
    import transcribe

    args = _parser().parse_args(argv)
    s = get_settings()
    configure_logging(s.log_level)

    if args.cmd == "doctor":
        checks = doctor_checks(network=args.network)
        print(format_checks(checks))
        return 1 if any(c.status == "fail" for c in checks) else 0
    if args.cmd == "serve":
        serve(args.host, args.port)
        return 0
    if args.cmd == "schedule":
        return schedule(args.host, args.port)

    s.ensure_dirs()
    db.init_db()
    try:
        if args.cmd == "init":
            strat = strategy_mod.load_strategy()
            strategy_mod.record_initial_version(strat)
            print(f"data dir:  {s.data_dir}\ndatabase:  {s.db_path} (schema v{db.schema_version()})\n"
                  f"strategy:  {s.strategy_path} (version {strat.version})\n"
                  f"DRY_RUN={s.dry_run} FAKE_LLM={s.fake_llm}\nnext: python main.py doctor")
        elif args.cmd == "manual":
            clip_ids = manual(args.url, args.source_type, upload=not args.no_upload)
            src = db.get_source_by_url(args.url)
            print(f"source {src.id}: {src.status} ({src.licence or 'no licence'}) {src.title or ''}")
            if not clip_ids:
                print(f"no clips: {src.status_reason or 'nothing scored high enough'}")
            for cid in clip_ids:
                c = db.require_clip(cid)
                ups = db.list_uploads("uploaded", clip_id=cid)
                where = (f" -> {ups[0].youtube_video_id}"
                         + (f" (publish {ups[0].publish_at})" if ups[0].publish_at else "")) if ups else ""
                print(f"clip {cid}: {c.status} {c.start_sec:.1f}-{c.end_sec:.1f}s {c.title!r}\n"
                      f"    render: {c.render_path}{where}")
        elif args.cmd == "scout":
            import scout
            print(f"new candidates: {scout.scout()}")
        elif args.cmd == "process":
            print(json.dumps(process_pending(args.limit), indent=1))
        elif args.cmd == "run-once":
            print(json.dumps(run_once(args.max_sources), indent=1))
        elif args.cmd == "upload":
            print(f"uploads: {upload_ready(args.limit)}")
        elif args.cmd == "analytics":
            import analytics
            if args.day:
                print(f"stats rows: {analytics.collect_stats(args.day)}")
            else:
                print(json.dumps(analytics_job(), indent=1, default=str))
        elif args.cmd == "review":
            import strategist
            r = strategist.weekly_review()
            print(f"status: {r.status}  decision: {r.decision_id}  applied version: {r.applied_version}\n{r.report}")
        elif args.cmd == "summary":
            import notify
            print(notify.daily_summary())
        elif args.cmd == "status":
            print(status_text())
        elif args.cmd == "requeue-dry-run":
            if s.dry_run and not args.force:
                print("DRY_RUN is still true: set DRY_RUN=false in config.env first (or pass --force).",
                      file=sys.stderr)
                return 2
            out = requeue_dry_run()
            print(f"ready to upload: {out['rendered']}\nto re-render first: {out['picked']}")
        elif args.cmd == "cleanup":
            out = cleanup()
            print(f"freed {out['bytes'] / 1e6:.1f} MB ({out['sources']} sources, {out['renders']} renders)")
        elif args.cmd == "assistant":
            return assistant_cli(args)
        elif args.cmd in ("approve", "reject"):
            import notify
            ok = notify.handle_decision(args.decision_id, args.cmd == "approve", via="cli")
            d = db.get_decision(args.decision_id)
            print(f"decision {args.decision_id}: {d.status if d else 'not found'}" + ("" if ok else " (unchanged)"))
            return 0 if ok else 1
    except Busy as exc:
        print(f"busy: {exc}", file=sys.stderr)
        return 3
    except budget.BudgetExceeded as exc:
        print(f"stopped: {exc}", file=sys.stderr)
        return 1
    except fetch.LicenceRejected as exc:
        print(f"rejected by the licence gate: {exc}", file=sys.stderr)
        return 1
    except (fetch.FetchError, transcribe.TranscribeError, pick.PickError, ValueError) as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:  # Ctrl+C in a CLI command: no traceback
        sys.exit(130)
