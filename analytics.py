"""Daily YouTube Analytics pull -> ``stats`` / ``channel_stats``, plus takedown/claim checks.

Public API (CLAUDE.md contract)::

    set_service_factory(fn | None)        # fn() -> (youtube_data_v3, youtube_analytics_v2)
    collect_stats(day=None) -> int        # rows upserted into stats
    refresh_channel_stats() -> None
    check_claims() -> int                 # videos newly flagged
    lifetime_stats(video_id) -> dict | None   # extension: Data API counters cached in kv

Design notes
------------
* **Backfill**: Analytics data lags ~2 days and keeps being revised, so every run re-upserts
  the ``BACKFILL_DAYS`` days ending at ``day`` (default local today - 2).
* **Isolated reports**: each report (core watch metrics, engagement, retention, the Data API
  counters) is its own request; an HttpError on one is logged and the others still land.
  Values a failed report would have provided are kept from the existing ``stats`` row
  instead of being overwritten with zeros.
* **Per-video daily rows**: primary query is ``dimensions=video`` for one day with
  ``filters=video==id1,id2,...`` (``BATCH``) sorted by ``-views``; if YouTube rejects that
  shape (400) we fall back to the always-supported single-video ``dimensions=day`` query.
* **Day semantics**: YouTube Analytics days are Pacific-time days; ``stats.day`` stores the
  report day as-is (it is labelled "local" in the schema, the difference is <= 3 h).
* **Retention**: ``dimensions=elapsedVideoTimeRatio`` / ``metrics=audienceWatchRatio`` for a
  single video, only for videos at least ``RETENTION_MIN_AGE_DAYS`` old and at most every
  ``RETENTION_REFRESH_DAYS``; stored as ``[[ratio, watch_ratio], ...]`` in the ``retention``
  JSON column of that video's row for the newest collected day (and carried forward).
* **Same-day signal**: ``videos.list(part=statistics)`` lifetime counters (1 unit per 50 ids)
  are cached in ``kv['yt:lifetime:<video_id>']`` (no column exists for them).
* **Claims**: the Data API does not expose Content ID claim details to ordinary channels.
  ``check_claims`` only sees indirect signals (rejected/failed/deleted uploads, regional
  blocks, a video that vanished, a scheduled video that never went public).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import date, timedelta
from typing import Any

import db
import youtube_auth
from config import get_settings

log = logging.getLogger(__name__)

MODULE = "analytics"

LAG_DAYS = 2
BACKFILL_DAYS = 3
BATCH = 50                        # video ids per Analytics filter / Data API videos.list call
RETENTION_MIN_AGE_DAYS = 3
RETENTION_REFRESH_DAYS = 7
NOT_PUBLIC_GRACE = timedelta(hours=2)

CORE_METRICS = ("views", "estimatedMinutesWatched", "averageViewDuration", "averageViewPercentage")
ENGAGEMENT_METRICS = ("likes", "comments", "shares", "subscribersGained", "subscribersLost")
_COLUMN = {
    "views": "views", "estimatedMinutesWatched": "watch_minutes", "averageViewDuration": "avg_view_duration_sec",
    "averageViewPercentage": "avg_view_pct", "likes": "likes", "comments": "comments", "shares": "shares",
    "subscribersGained": "subscribers_gained", "subscribersLost": "subscribers_lost",
}
_NULLABLE = ("avg_view_duration_sec", "avg_view_pct")
_STAT_FIELDS = ("views", "watch_minutes", "avg_view_duration_sec", "avg_view_pct", "likes", "comments", "shares",
                "subscribers_gained", "subscribers_lost", "retention")

CLAIM_REJECTIONS = frozenset({"claim", "copyright", "trademark", "legal"})

_service_factory: Callable[[], tuple[Any, Any]] | None = None


def set_service_factory(fn: Callable[[], tuple[Any, Any]] | None) -> None:
    """Inject ``fn() -> (youtube_data, youtube_analytics)`` (tests); ``None`` restores OAuth."""
    global _service_factory
    _service_factory = fn


def _default_services() -> tuple[Any, Any]:
    creds = youtube_auth.load_credentials()
    return youtube_auth.youtube_service(creds), youtube_auth.analytics_service(creds)


def _services(purpose: str) -> tuple[Any, Any] | None:
    """Services, or ``None`` (logged) in DRY_RUN / without a token / on auth failure."""
    if get_settings().dry_run:
        db.log_event(MODULE, f"{purpose}: skipped (DRY_RUN)", level="debug")
        return None
    if _service_factory is not None:
        return _service_factory()
    if not youtube_auth.has_token():
        youtube_auth.report_auth_problem("YouTube token missing; analytics skipped. Run `python youtube_auth.py`.")
        return None
    try:
        return _default_services()
    except youtube_auth.YouTubeAuthError:
        return None  # already logged + owner notified


def _http_error() -> type[Exception]:
    from googleapiclient.errors import HttpError
    return HttpError


def _chunks(items: list[str], n: int) -> Iterable[list[str]]:
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _report_failed(report: str, exc: Exception, **data: Any) -> None:
    status = getattr(getattr(exc, "resp", None), "status", None)
    db.log_event(MODULE, f"report {report} failed (HTTP {status})", level="warning",
                 data={"error": str(exc)[:500], **data})


def _rows(resp: dict[str, Any]) -> list[dict[str, Any]]:
    headers = [h["name"] for h in resp.get("columnHeaders") or []]
    return [dict(zip(headers, r)) for r in resp.get("rows") or []]


def _live_dates() -> dict[str, date]:
    """video id -> first local day it could have views (publish_at or uploaded_at)."""
    out: dict[str, date] = {}
    for u in db.list_uploads("uploaded", include_dry_run=False):
        if not u.youtube_video_id:
            continue
        ts = u.publish_at or u.uploaded_at
        if ts:
            out[u.youtube_video_id] = db.local_date_of(ts)
    return out


# ---------------------------------------------------------------------------
# collect_stats
# ---------------------------------------------------------------------------

def _query(analytics: Any, **params: Any) -> dict[str, Any]:
    return analytics.reports().query(ids="channel==MINE", **params).execute()


def _video_day_report(analytics: Any, metrics: tuple[str, ...], ids: list[str], day: date,
                      report: str) -> dict[str, dict[str, Any]] | None:
    """{video_id: {metric: value}} for one day, or None if the report failed entirely."""
    HttpError = _http_error()
    d = day.isoformat()
    out: dict[str, dict[str, Any]] = {}
    ok = False
    for batch in _chunks(ids, BATCH):
        try:
            resp = _query(analytics, startDate=d, endDate=d, metrics=",".join(metrics), dimensions="video",
                          filters="video==" + ",".join(batch), sort="-views" if "views" in metrics else
                          f"-{metrics[0]}", maxResults=len(batch))
            for r in _rows(resp):
                out[r.pop("video")] = r
            ok = True
        except HttpError as exc:
            if getattr(getattr(exc, "resp", None), "status", None) != 400:
                _report_failed(report, exc, day=d, videos=len(batch))
                continue
            # Fallback: single-video daily report (documented shape), one call per video.
            for vid in batch:
                try:
                    resp = _query(analytics, startDate=d, endDate=d, metrics=",".join(metrics),
                                  dimensions="day", filters=f"video=={vid}")
                    rows = _rows(resp)
                    if rows:
                        rows[0].pop("day", None)
                        out[vid] = rows[0]
                    ok = True
                except HttpError as exc2:
                    _report_failed(report, exc2, day=d, video=vid)
    return out if ok else None


def _retention(analytics: Any, vid: str, start: date, end: date) -> list[list[float]] | None:
    HttpError = _http_error()
    try:
        resp = _query(analytics, startDate=start.isoformat(), endDate=end.isoformat(),
                      metrics="audienceWatchRatio", dimensions="elapsedVideoTimeRatio",
                      filters=f"video=={vid}")
    except HttpError as exc:
        _report_failed("retention", exc, video=vid)
        return None
    curve = [[round(float(r["elapsedVideoTimeRatio"]), 4), round(float(r["audienceWatchRatio"]), 4)]
             for r in _rows(resp)]
    return sorted(curve) or None


def _coerce(col: str, v: Any) -> Any:
    if v is None:
        return None if col in _NULLABLE else 0
    return float(v) if col in _NULLABLE or col == "watch_minutes" else int(v)


def _latest_retention(ids: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for vid in ids:
        for st in reversed(db.get_stats(vid)):
            if st.retention:
                out[vid] = st.retention
                break
    return out


def _retention_due(vid: str, live: date, today: date) -> bool:
    if (today - live).days < RETENTION_MIN_AGE_DAYS:
        return False
    last = db.kv_get(f"analytics:retention:{vid}")
    return last is None or (today - date.fromisoformat(last)).days >= RETENTION_REFRESH_DAYS


def collect_stats(day: date | None = None) -> int:
    """Upsert per-video daily stats for the ``BACKFILL_DAYS`` days ending at ``day``. Returns rows upserted."""
    ids_all = db.live_video_ids()
    if not ids_all:
        db.log_event(MODULE, "collect_stats: no live videos", level="debug")
        return 0
    svcs = _services("collect_stats")
    if svcs is None:
        return 0
    youtube, analytics = svcs
    today = db.local_today()
    end_day = day or (today - timedelta(days=LAG_DAYS))
    live = _live_dates()
    days = [end_day - timedelta(days=i) for i in range(BACKFILL_DAYS - 1, -1, -1)]
    upserted = 0
    failed_reports = 0
    last_curve = _latest_retention(ids_all)

    for d in days:
        ids = [v for v in ids_all if live.get(v, d) <= d]
        if not ids:
            continue
        core = _video_day_report(analytics, CORE_METRICS, ids, d, "core")
        eng = _video_day_report(analytics, ENGAGEMENT_METRICS, ids, d, "engagement")
        failed_reports += (core is None) + (eng is None)
        if core is None and eng is None:
            continue
        existing = {s.youtube_video_id: s for s in db.stats_between(d, d)}
        for vid in ids:
            prev = existing.get(vid)
            row: dict[str, Any] = {f: getattr(prev, f) for f in _STAT_FIELDS} if prev else {}
            if row.get("retention") is None and vid in last_curve:
                row["retention"] = last_curve[vid]
            for report, metrics in ((core, CORE_METRICS), (eng, ENGAGEMENT_METRICS)):
                if report is None:
                    continue  # keep previous values for this report's columns
                vals = report.get(vid, {})
                for m in metrics:
                    row[_COLUMN[m]] = _coerce(_COLUMN[m], vals.get(m))
            db.upsert_stat(vid, d, **row)
            upserted += 1

    # retention curves, sparingly, attached to the newest collected day's row
    newest = {s.youtube_video_id: s for s in db.stats_between(end_day, end_day)}
    for vid in ids_all:
        live_day = live.get(vid)
        prev = newest.get(vid)
        if prev is None or live_day is None or not _retention_due(vid, live_day, today):
            continue
        curve = _retention(analytics, vid, live_day, end_day)
        if curve is None:
            continue
        row = {f: getattr(prev, f) for f in _STAT_FIELDS}
        row["retention"] = curve
        db.upsert_stat(vid, end_day, **row)
        db.kv_set(f"analytics:retention:{vid}", today.isoformat())

    refreshed = _refresh_lifetime(youtube, ids_all)
    db.log_event(MODULE, f"collected stats: {upserted} rows over {len(days)} days",
                 level="warning" if failed_reports else "info",
                 data={"days": [d.isoformat() for d in days], "videos": len(ids_all), "rows": upserted,
                       "failed_reports": failed_reports, "lifetime_refreshed": refreshed})
    return upserted


# ---------------------------------------------------------------------------
# Data API counters
# ---------------------------------------------------------------------------

def _videos_list(youtube: Any, ids: list[str], part: str) -> dict[str, dict[str, Any]] | None:
    """{id: item} for all ids (missing ids absent), or None if any batch failed."""
    HttpError = _http_error()
    out: dict[str, dict[str, Any]] = {}
    for batch in _chunks(ids, 50):
        try:
            resp = youtube.videos().list(part=part, id=",".join(batch)).execute()  # maxResults is not allowed with id
        except HttpError as exc:
            _report_failed(f"videos.list({part})", exc, videos=len(batch))
            return None
        finally:
            youtube_auth.record_quota("videos.list")
        for it in resp.get("items") or []:
            out[it["id"]] = it
    return out


def _refresh_lifetime(youtube: Any, ids: list[str]) -> int:
    items = _videos_list(youtube, ids, "statistics")
    if items is None:
        return 0
    now = db.now_iso()
    for vid, it in items.items():
        s = it.get("statistics") or {}
        db.kv_set(f"yt:lifetime:{vid}", {
            "views": int(s.get("viewCount", 0)), "likes": int(s.get("likeCount", 0)),
            "comments": int(s.get("commentCount", 0)), "fetched_at": now})
    return len(items)


def lifetime_stats(video_id: str) -> dict[str, Any] | None:
    """Latest Data API lifetime counters ``{views, likes, comments, fetched_at}`` (same-day signal)."""
    return db.kv_get(f"yt:lifetime:{video_id}")


def refresh_channel_stats() -> None:
    """Today's ``channel_stats`` row from ``channels.list(mine=True, part=statistics)``."""
    svcs = _services("refresh_channel_stats")
    if svcs is None:
        return
    youtube, _ = svcs
    HttpError = _http_error()
    try:
        resp = youtube.channels().list(part="statistics", mine=True).execute()
    except HttpError as exc:
        _report_failed("channels.list", exc)
        return
    finally:
        youtube_auth.record_quota("channels.list")
    items = resp.get("items") or []
    if not items:
        db.log_event(MODULE, "channels.list returned no channel for this token", level="warning")
        return
    s = items[0].get("statistics") or {}

    def _int(k: str) -> int | None:
        return int(s[k]) if s.get(k) is not None else None

    subs = None if s.get("hiddenSubscriberCount") and s.get("subscriberCount") is None else _int("subscriberCount")
    db.upsert_channel_stat(db.local_today(), subscribers=subs, total_views=_int("viewCount"),
                           video_count=_int("videoCount"))
    db.log_event(MODULE, "channel stats refreshed", level="debug", data=s)


# ---------------------------------------------------------------------------
# check_claims
# ---------------------------------------------------------------------------

def _problems(item: dict[str, Any]) -> tuple[list[str], bool]:
    """(human-readable problems, is_copyright_related) for one videos.list item."""
    st = item.get("status") or {}
    cd = item.get("contentDetails") or {}
    problems: list[str] = []
    claim = False
    up = st.get("uploadStatus")
    if up == "rejected":
        reason = st.get("rejectionReason") or "unknown"
        problems.append(f"rejected by YouTube ({reason})")
        claim = claim or reason in CLAIM_REJECTIONS
    elif up == "failed":
        problems.append(f"processing failed ({st.get('failureReason') or 'unknown'})")
    elif up == "deleted":
        problems.append("deleted")
    blocked = ((cd.get("regionRestriction") or {}).get("blocked")) or []
    if blocked:
        problems.append(f"blocked in {len(blocked)} region(s)" + (" (worldwide-scale block)" if len(blocked) > 100 else ""))
        claim = True
    allowed = (cd.get("regionRestriction") or {}).get("allowed")
    if allowed is not None:
        problems.append(f"only viewable in {len(allowed)} region(s)")
        claim = True
    return problems, claim


def _flag(upload: db.Upload, vid: str, problems: list[str], *, claim: bool, removed: bool) -> None:
    if claim:
        db.set_copyright_claim(vid, True)
    if removed:
        db.update_upload(upload.id, status="removed", error="; ".join(problems)[:500])
    clip = db.get_clip(upload.clip_id)
    title = clip.title if clip else f"clip {upload.clip_id}"
    db.log_event(MODULE, f"video {vid} flagged: {'; '.join(problems)}", level="error",
                 data={"video_id": vid, "upload_id": upload.id, "clip_id": upload.clip_id,
                       "problems": problems, "copyright": claim, "removed": removed})
    db.add_notification("youtube_claim",
                        f"Urgent: the Short '{title}' has a problem on YouTube: {'; '.join(problems)}. "
                        f"Please check YouTube Studio.", "call")


def check_claims() -> int:
    """Flag rejected/blocked/removed uploads. Returns how many videos were newly flagged."""
    ids = db.live_video_ids()
    if not ids:
        return 0
    svcs = _services("check_claims")
    if svcs is None:
        return 0
    youtube, _ = svcs
    items = _videos_list(youtube, ids, "status,contentDetails")
    if items is None:
        return 0
    uploads = {u.youtube_video_id: u for u in db.list_uploads("uploaded", include_dry_run=False)}
    now = db.utcnow()
    newly = 0
    stuck_private: list[str] = []
    for vid in ids:
        up = uploads.get(vid)
        if up is None:
            continue
        item = items.get(vid)
        if item is None:
            _flag(up, vid, ["no longer on YouTube (removed or deleted)"], claim=False, removed=True)
            newly += 1
            continue
        problems, claim = _problems(item)
        removed = (item.get("status") or {}).get("uploadStatus") in ("rejected", "deleted")
        # flag each distinct set of problems once (a later, different problem flags again)
        if problems and db.kv_set_if_absent(f"analytics:flagged:{vid}:{'|'.join(sorted(problems))}", db.now_iso()):
            _flag(up, vid, problems, claim=claim, removed=removed)
            newly += 1
        if (item.get("contentDetails") or {}).get("licensedContent"):
            # Meaning for non-partner channels is unclear; record it, do not page anyone.
            if db.kv_set_if_absent(f"analytics:licensed_content:{vid}", db.now_iso()):
                db.log_event(MODULE, f"video {vid} reports licensedContent=true", level="info",
                             data={"video_id": vid})
        st = item.get("status") or {}
        if (up.publish_at and st.get("privacyStatus") == "private" and not st.get("publishAt")
                and now - db.parse_iso(up.publish_at) > NOT_PUBLIC_GRACE):
            stuck_private.append(vid)

    if stuck_private and db.kv_set_if_absent(f"analytics:stuck_private:{db.local_today().isoformat()}", stuck_private):
        db.log_event(MODULE, f"{len(stuck_private)} scheduled Shorts are still private after their publish time",
                     level="warning", data={"video_ids": stuck_private,
                                            "hint": "API project probably not audited: uploads are locked private"})
        db.add_notification(
            "youtube_private_lock",
            f"{len(stuck_private)} scheduled Shorts stayed private after their publish time. YouTube locks "
            f"videos uploaded by unaudited API projects to private; publish them in YouTube Studio and "
            f"submit the API audit form.", "sms")
    db.log_event(MODULE, f"claims check: {newly} newly flagged of {len(ids)}",
                 level="warning" if newly else "info", data={"checked": len(ids), "flagged": newly})
    return newly
