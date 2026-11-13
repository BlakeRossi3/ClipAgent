"""YouTube Data API v3 upload + scheduling.

Public API (CLAUDE.md contract, extended)::

    uploads_today() -> int
    next_publish_slots(n, after=None) -> list[datetime]        # aware UTC
    upload_clip(clip_id, publish_at=None, *, immediate=False) -> upload_id
    get_youtube_service()
    set_service_factory(fn | None)                              # tests / scripts
    build_metadata(clip, source=None, *, settings=None, strategy=None) -> dict   # videos.insert body (pure)
    UploadError, UploadCapReached, QuotaExhausted(UploadCapReached), UploadAuthError(UploadError)

Behaviour summary
-----------------
* **Cap**: ``uploads_today() >= strategy.upload_cap()`` (local day) -> :class:`UploadCapReached`.
* **Quota**: a local tally of Data API units per *Pacific* day (``youtube_auth.record_quota``).
  An insert is refused (:class:`QuotaExhausted`) if it would leave less than
  ``youtube_auth.QUOTA_RESERVE`` units. A ``quotaExceeded`` / ``uploadLimitExceeded``
  response from YouTube stops uploads until the next Pacific midnight and queues a
  ``youtube_quota`` notification (once per day). The clip stays ``rendered`` / ``scheduled``.
* **Scheduling**: ``publish_at`` in the future -> ``privacyStatus=private`` + ``publishAt``.
  ``publish_at=None`` -> first free slot from :func:`next_publish_slots` (at least
  :data:`MIN_LEAD` from now). ``immediate=True`` -> no ``publishAt``; privacy =
  ``settings.youtube_default_privacy``.
* **Licence field**: always ``status.license = "youtube"`` (standard). We never mark a
  Short ``creativeCommon``: for public-domain sources a CC licence would assert rights
  nobody holds, and for CC BY sources the obligation is *attribution* (title, author,
  source link, licence link, note of changes), which the description always carries.
  CC BY does not require the derivative to be CC-licensed.
* **Retries**: resumable upload (:data:`CHUNK_SIZE`), exponential backoff with jitter on
  5xx/429/rate-limit reasons and transport errors (:data:`MAX_RETRIES`).
* **Errors**: permanent (bad metadata, bad file) -> upload ``failed``, clip ``failed``,
  error event, :class:`UploadError`. Transient-exhausted / quota / auth -> upload ``failed``,
  clip left as is (retry later), :class:`UploadError` / :class:`QuotaExhausted` /
  :class:`UploadAuthError` (the orchestrator should stop the batch on the last two).
* **Idempotency**: a clip with a successful upload row is never uploaded again (the existing
  upload id is returned). Every video carries the tag ``clipagent-clip-<id>``; a ``queued``
  live upload row left behind by a crash is reconciled by looking for that tag among the
  channel's latest uploads before anything is re-sent.
* **DRY_RUN**: no API call; video id ``dryrun-<clip_id>``, quota 0, slot still recorded.
"""

from __future__ import annotations

import json
import logging
import random
import re
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import db
import strategy as strategy_mod
import youtube_auth
from config import Settings, get_settings

log = logging.getLogger(__name__)

MODULE = "upload"

# --- tunables -----------------------------------------------------------------
CHUNK_SIZE = 8 * 1024 * 1024            # resumable chunk (multiple of 256 KiB)
MAX_RETRIES = 8
BACKOFF_BASE_SEC = 2.0
BACKOFF_MAX_SEC = 64.0
MIN_LEAD = timedelta(minutes=20)        # a default slot must be at least this far ahead
SLOT_SEARCH_DAYS = 60
ORPHAN_AGE = timedelta(hours=2)         # older queued live rows are presumed crashed
MAX_SHORT_SEC = 180.0                   # YouTube Shorts: up to 3 minutes, vertical/square

TITLE_MAX_CHARS = 100
DESCRIPTION_MAX_BYTES = 5000
TAGS_MAX_CHARS = 500
SHORTS_TAG = "#Shorts"
MARKER_TAG = "clipagent-clip-{clip_id}"

LICENCE_TEXT = {
    "us_gov_public_domain": "Public domain - work of the US federal government (17 U.S.C. 105).",
    "cc_by": "Creative Commons Attribution (CC BY 3.0) - https://creativecommons.org/licenses/by/3.0/",
}

# Reasons from the Data API error payload (error.errors[].reason).
QUOTA_REASONS = frozenset({"quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded"})
RETRY_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "backendError", "internalError"})
AUTH_REASONS = frozenset({"authError", "unauthorized", "forbidden", "insufficientPermissions",
                          "youtubeSignupRequired", "accountClosed", "accountSuspended"})
RETRY_STATUSES = frozenset({500, 502, 503, 504, 429})

_QUOTA_STOP_KEY = "youtube:quota_exhausted:{day}"     # Pacific date
_QUOTA_ALERT_KEY = "youtube:quota_alerted:{day}"

_sleep: Callable[[float], None] = time.sleep          # tests patch
_service_factory: Callable[[], Any] | None = None


class UploadError(Exception):
    """An upload could not be completed."""


class UploadCapReached(UploadError):
    """No more uploads today (strategy cap). Stop the batch."""


class QuotaExhausted(UploadCapReached):
    """YouTube quota (local tally or API quotaExceeded/uploadLimitExceeded). Stop until PT midnight."""


class UploadAuthError(UploadError):
    """No usable OAuth token. Stop the batch; the owner has been notified."""


class _Permanent(Exception):
    pass


# ---------------------------------------------------------------------------
# services
# ---------------------------------------------------------------------------

def set_service_factory(fn: Callable[[], Any] | None) -> None:
    """Inject a callable returning a youtube v3 service (tests); ``None`` restores OAuth."""
    global _service_factory
    _service_factory = fn


def get_youtube_service() -> Any:
    """Authorised YouTube Data API v3 client (token from ``python youtube_auth.py``)."""
    return youtube_auth.youtube_service()


def _service() -> Any:
    return _service_factory() if _service_factory is not None else get_youtube_service()


# ---------------------------------------------------------------------------
# cap & slots
# ---------------------------------------------------------------------------

def uploads_today() -> int:
    return db.count_uploads_on()


def _load_strategy() -> strategy_mod.Strategy | None:
    try:
        return strategy_mod.load_strategy()
    except (OSError, ValueError):
        return None


def next_publish_slots(n: int, after: datetime | None = None) -> list[datetime]:
    """The next ``n`` free publish slots (aware UTC), strictly after ``after`` (default now).

    Slots are the strategy ``posting_times`` in the strategy ``timezone``. Times already in
    ``db.publish_times_between`` are skipped and a strategy-local day never gets more than
    ``upload_cap()`` publishes (already-taken times of that day count, whatever they are).
    Wall times are resolved with zoneinfo: on a DST gap the slot shifts forward by the gap,
    on a repeated hour the first occurrence is used.
    """
    if n <= 0:
        return []
    strat = _load_strategy()
    cap = strategy_mod.upload_cap(strat)
    if strat is None or cap <= 0 or not strat.get("posting_times"):
        return []
    tz = ZoneInfo(strat["timezone"])
    times = sorted(datetime.strptime(t, "%H:%M").time() for t in strat["posting_times"])
    after = (after or db.utcnow()).astimezone(UTC)
    day: date = after.astimezone(tz).date()
    out: list[datetime] = []
    for _ in range(SLOT_SEARCH_DAYS):
        start = datetime.combine(day, datetime.min.time(), tzinfo=tz).astimezone(UTC)
        end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=tz).astimezone(UTC)
        taken = set(db.publish_times_between(db.to_iso(start), db.to_iso(end)))
        used = len(taken)
        for t in times:
            if used >= cap:
                break
            slot = datetime.combine(day, t, tzinfo=tz).astimezone(UTC)
            if slot <= after or db.to_iso(slot) in taken:
                continue
            out.append(slot)
            used += 1
            if len(out) == n:
                return out
        day += timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# metadata (pure)
# ---------------------------------------------------------------------------

_ANGLE = re.compile(r"[<>]")
_WS = re.compile(r"[ \t]+")


def _clean(text: str | None) -> str:
    return _ANGLE.sub("", text or "").replace("\r\n", "\n").strip()


def _truncate_words(text: str, limit: int) -> str:
    """Cut ``text`` to <= ``limit`` chars on a word boundary (hard cut if one word is too long)."""
    text = _WS.sub(" ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[: limit + 1]
    sp = cut.rfind(" ")
    out = cut[:sp] if sp > limit // 3 else text[:limit]
    return out.rstrip(" ,;:-|")


def _truncate_bytes(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    cut = raw[:limit].decode("utf-8", errors="ignore")
    nl = cut.rfind("\n")
    sp = cut.rfind(" ")
    pos = max(nl, sp)
    return (cut[:pos] if pos > len(cut) // 2 else cut).rstrip() + "..."


def build_title(title: str, fallback: str = "Clip") -> str:
    """<= 100 chars, no angle brackets, contains ``#Shorts`` (kept on truncation)."""
    t = _WS.sub(" ", _clean(title).replace("\n", " ")) or _clean(fallback) or "Clip"
    if "#shorts" in t.lower():
        if len(t) <= TITLE_MAX_CHARS:
            return t
        t = re.sub(r"\s*#shorts\b", "", t, flags=re.IGNORECASE).strip()
    base = _truncate_words(t, TITLE_MAX_CHARS - len(SHORTS_TAG) - 1)
    return f"{base} {SHORTS_TAG}".strip()


def _norm_hashtag(tag: str) -> str:
    tag = re.sub(r"[^\w]", "", _clean(tag).lstrip("#"))
    return f"#{tag}" if tag else ""


def credit_block(clip: db.Clip, source: db.Source | None = None) -> str:
    """Credit + source URL + licence (+ CC BY attribution details). Always in the description."""
    credit = _clean(clip.credit_text)
    channel = _clean(source.channel) if source and source.channel else ""
    if not credit:
        credit = f"Video: {channel}" if channel else "Video: original source (see link)"
    lines = [credit, f"Source: {clip.source_url}",
             f"Licence: {LICENCE_TEXT.get(clip.licence, clip.licence)}"]
    if clip.licence == "cc_by":
        orig = _clean(source.title) if source and source.title else ""
        author = channel or "the original creator"
        lines.append(f'Original work: "{orig}" by {author}' if orig else f"Original work by {author}")
        lines.append("Changes: excerpted, reframed to vertical, captions and titles added. "
                     "The original creator does not endorse this channel.")
    return "\n".join(lines)


def build_description(clip: db.Clip, source: db.Source | None = None,
                      extra_hashtags: list[str] | None = None) -> str:
    """Creative text + hashtags + credit block; <= 5000 UTF-8 bytes; the credit block is never cut."""
    tags: list[str] = []
    for h in [SHORTS_TAG, *(clip.hashtags or []), *(extra_hashtags or [])]:
        h = _norm_hashtag(h)
        if h and h.lower() not in {x.lower() for x in tags}:
            tags.append(h)
    hashtag_line = " ".join(tags[:15])  # YouTube ignores all hashtags if a video has more than 15
    tail = "\n\n" + hashtag_line + "\n\n" + credit_block(clip, source)
    body = _clean(clip.description)
    room = DESCRIPTION_MAX_BYTES - len(tail.encode("utf-8"))
    body = _truncate_bytes(body, max(room, 0)) if body else ""
    return (body + tail).strip() if body else tail.strip()


def build_tags(clip: db.Clip, extra: list[str] | None = None) -> list[str]:
    """Plain keyword tags (no '#'); marker tag first; total <= 500 chars as YouTube counts them."""
    seen: set[str] = set()
    out: list[str] = []
    total = 0
    for raw in [MARKER_TAG.format(clip_id=clip.id), "Shorts", *(clip.hashtags or []), *(extra or [])]:
        t = _clean(raw).lstrip("#").replace(",", " ").strip()
        if not t or t.lower() in seen:
            continue
        cost = len(t) + (2 if " " in t else 0) + (1 if out else 0)
        if total + cost > TAGS_MAX_CHARS:
            continue
        seen.add(t.lower())
        out.append(t)
        total += cost
    return out


def build_metadata(clip: db.Clip, source: db.Source | None = None, *, settings: Settings | None = None,
                   strategy: dict | None = None, publish_at: datetime | None = None,
                   privacy: str | None = None) -> dict[str, Any]:
    """The ``videos.insert`` request body (pure apart from reading settings)."""
    settings = settings or get_settings()
    extra = list((strategy or {}).get("default_hashtags") or [])
    fallback = (source.title if source and source.title else "") or clip.hook_text or "Clip"
    status: dict[str, Any] = {
        "selfDeclaredMadeForKids": False,
        "license": "youtube",
        "embeddable": True,
        "containsSyntheticMedia": False,
    }
    if publish_at is not None:
        status["privacyStatus"] = "private"
        status["publishAt"] = publish_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    else:
        status["privacyStatus"] = privacy or settings.youtube_default_privacy
    return {
        "snippet": {
            "title": build_title(clip.title, fallback),
            "description": build_description(clip, source, extra),
            "tags": build_tags(clip, extra),
            "categoryId": str(settings.youtube_category_id),
        },
        "status": status,
    }


# ---------------------------------------------------------------------------
# file checks
# ---------------------------------------------------------------------------

def probe_video(path: Path, settings: Settings | None = None) -> tuple[int, int, float] | None:
    """(width, height, duration) via ffprobe; ``None`` if ffprobe is unavailable. Tests patch this."""
    settings = settings or get_settings()
    ffprobe = str(Path(settings.ffmpeg_bin).with_name("ffprobe")) if "/" in settings.ffmpeg_bin else "ffprobe"
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration",
             "-of", "json", str(path)], capture_output=True, text=True, timeout=60, check=True).stdout
    except FileNotFoundError:
        return None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise _Permanent(f"ffprobe failed on {path}: {exc}") from exc
    data = json.loads(out or "{}")
    st = (data.get("streams") or [{}])[0]
    return int(st.get("width") or 0), int(st.get("height") or 0), float((data.get("format") or {}).get("duration") or 0)


def _check_render(clip: db.Clip, *, strict: bool) -> Path | None:
    if clip.duration_sec > MAX_SHORT_SEC:
        raise _Permanent(f"clip is {clip.duration_sec:.0f}s; Shorts must be <= {MAX_SHORT_SEC:.0f}s")
    path = Path(clip.render_path) if clip.render_path else None
    if path is None or not path.is_file():
        if strict:
            raise _Permanent(f"render file missing: {clip.render_path}")
        return None
    info = probe_video(path)
    if info is not None:
        w, h, dur = info
        if w and h and w > h:
            raise _Permanent(f"render is landscape ({w}x{h}); Shorts must be vertical or square")
        if dur > MAX_SHORT_SEC + 0.5:
            raise _Permanent(f"render is {dur:.1f}s; Shorts must be <= {MAX_SHORT_SEC:.0f}s")
    return path


# ---------------------------------------------------------------------------
# API error handling
# ---------------------------------------------------------------------------

def _error_reasons(exc: Exception) -> tuple[int, set[str], str]:
    status = int(getattr(getattr(exc, "resp", None), "status", 0) or 0)
    reasons: set[str] = set()
    message = str(exc)
    content = getattr(exc, "content", b"") or b""
    try:
        payload = json.loads(content.decode("utf-8") if isinstance(content, bytes) else content)
        err = payload.get("error", {}) if isinstance(payload, dict) else {}
        if isinstance(err, dict):
            message = err.get("message") or message
            for e in err.get("errors") or []:
                if e.get("reason"):
                    reasons.add(e["reason"])
            for d in err.get("details") or []:
                if isinstance(d, dict) and d.get("reason"):
                    reasons.add(d["reason"])
        elif isinstance(err, str):
            reasons.add(err)
    except (ValueError, AttributeError):
        pass
    return status, reasons, message


def _classify(exc: Exception) -> str:
    """'quota' | 'retry' | 'auth' | 'permanent' for an HttpError."""
    status, reasons, _ = _error_reasons(exc)
    if reasons & QUOTA_REASONS:
        return "quota"
    if status in RETRY_STATUSES or reasons & RETRY_REASONS:
        return "retry"
    if status == 401 or reasons & AUTH_REASONS:
        return "auth"
    return "permanent"


def _transient_types() -> tuple[type[BaseException], ...]:
    import http.client
    import socket
    import ssl

    import httplib2

    types: list[type[BaseException]] = [httplib2.HttpLib2Error, http.client.HTTPException, ConnectionError,
                                        TimeoutError, socket.timeout, ssl.SSLError]
    try:
        from google.auth.exceptions import TransportError
        types.append(TransportError)
    except ImportError:  # pragma: no cover
        pass
    return tuple(types)


def _backoff(attempt: int) -> float:
    return min(BACKOFF_MAX_SEC, BACKOFF_BASE_SEC * (2 ** attempt)) * (0.5 + random.random() / 2)


def _execute_insert(request: Any) -> dict[str, Any]:
    """Drive a resumable ``videos.insert`` to completion with retry/backoff."""
    from googleapiclient.errors import HttpError

    transient = _transient_types()
    attempt = 0
    response = None
    while response is None:
        try:
            _status, response = request.next_chunk()
            attempt = 0 if _status is not None else attempt  # progress resets the budget
        except HttpError as exc:
            if _classify(exc) != "retry":
                raise
            err: BaseException = exc
        except transient as exc:  # type: ignore[misc]
            err = exc
        else:
            continue
        attempt += 1
        if attempt > MAX_RETRIES:
            raise _RetriesExhausted(f"gave up after {MAX_RETRIES} retries: {err}") from err
        delay = _backoff(attempt - 1)
        log.warning("upload chunk failed (%s); retry %d/%d in %.1fs", err, attempt, MAX_RETRIES, delay)
        _sleep(delay)
    if "id" not in response:
        raise _Permanent(f"unexpected videos.insert response: {response!r}")
    return response


class _RetriesExhausted(Exception):
    pass


# ---------------------------------------------------------------------------
# quota stop
# ---------------------------------------------------------------------------

def quota_exhausted_today() -> bool:
    return db.kv_get(_QUOTA_STOP_KEY.format(day=youtube_auth.quota_day().isoformat())) is not None


def _stop_for_quota(reason: str, detail: str) -> None:
    day = youtube_auth.quota_day().isoformat()
    db.kv_set(_QUOTA_STOP_KEY.format(day=day), {"reason": reason, "detail": detail, "at": db.now_iso()})
    db.log_event(MODULE, f"YouTube quota stop: {reason}", level="warning",
                 data={"pacific_day": day, "detail": detail, "tally": youtube_auth.quota_used()})
    if db.kv_set_if_absent(_QUOTA_ALERT_KEY.format(day=day), True):
        msg = ("YouTube upload quota reached for today; uploads resume after midnight Pacific time."
               if reason != "uploadLimitExceeded" else
               "YouTube says this channel hit its upload limit. Uploads are paused until tomorrow.")
        db.add_notification("youtube_quota", msg, "sms")


# ---------------------------------------------------------------------------
# idempotency / crash recovery
# ---------------------------------------------------------------------------

def _find_video_by_marker(youtube: Any, clip_id: int, max_items: int = 50) -> str | None:
    """Look for our marker tag among the channel's most recent uploads (3 units)."""
    marker = MARKER_TAG.format(clip_id=clip_id)
    ch = youtube.channels().list(part="contentDetails", mine=True).execute()
    youtube_auth.record_quota("channels.list")
    items = ch.get("items") or []
    if not items:
        return None
    playlist = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    pl = youtube.playlistItems().list(part="contentDetails", playlistId=playlist, maxResults=max_items).execute()
    youtube_auth.record_quota("playlistItems.list")
    ids = [it["contentDetails"]["videoId"] for it in pl.get("items") or []]
    if not ids:
        return None
    vids = youtube.videos().list(part="snippet", id=",".join(ids)).execute()
    youtube_auth.record_quota("videos.list")
    for v in vids.get("items") or []:
        if marker in ((v.get("snippet") or {}).get("tags") or []):
            return v["id"]
    return None


def _reconcile_orphans(clip_id: int, youtube_factory: Callable[[], Any]) -> int | None:
    """Resolve ``queued`` live rows for this clip. Returns an upload id if the video already exists."""
    orphans = [u for u in db.list_uploads("queued", clip_id=clip_id) if not u.dry_run]
    if not orphans:
        return None
    now = db.utcnow()
    for u in orphans:
        if now - db.parse_iso(u.created_at) < ORPHAN_AGE:
            raise UploadError(f"clip {clip_id} has an upload in progress (upload {u.id})")
    youtube = youtube_factory()
    found = _find_video_by_marker(youtube, clip_id)
    newest, rest = orphans[0], orphans[1:]
    for u in rest:
        db.mark_upload_failed(u.id, "interrupted upload (superseded)")
    if found:
        db.mark_upload_succeeded(newest.id, found, quota_units=youtube_auth.QUOTA_COSTS["videos.insert"])
        db.log_event(MODULE, "recovered interrupted upload", level="warning",
                     data={"clip_id": clip_id, "upload_id": newest.id, "video_id": found})
        return newest.id
    db.mark_upload_failed(newest.id, "interrupted upload; video not found on channel",
                          quota_units=youtube_auth.QUOTA_COSTS["videos.insert"])
    return None


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------

def _resolve_slot(publish_at: datetime | None, immediate: bool) -> datetime | None:
    if immediate:
        return None
    now = db.utcnow()
    if publish_at is not None:
        if publish_at.tzinfo is None:
            publish_at = publish_at.replace(tzinfo=UTC)
        if publish_at > now + timedelta(minutes=5):
            return publish_at.astimezone(UTC)
        log.info("requested publish_at %s is too soon; picking the next free slot", publish_at)
    slots = next_publish_slots(1, after=now + MIN_LEAD)
    if not slots:
        raise UploadCapReached("no free publish slot (upload cap is 0 or no posting_times)")
    return slots[0]


def _set_clip_uploaded(clip_id: int) -> None:
    clip = db.require_clip(clip_id)
    if clip.status != "uploaded":
        db.set_clip_status(clip_id, "uploaded", force=clip.status not in ("rendered", "scheduled"))


def upload_clip(clip_id: int, publish_at: datetime | None = None, *, immediate: bool = False) -> int:
    """Upload a rendered clip as a Short. Returns the ``uploads`` row id.

    See the module docstring for scheduling, error and idempotency semantics.
    """
    settings = get_settings()
    clip = db.require_clip(clip_id)

    # 1. idempotency: an existing successful upload wins (live only counts live ones)
    for u in db.list_uploads("uploaded", clip_id=clip_id):
        if settings.dry_run or not u.dry_run:
            log.info("clip %s already uploaded (upload %s)", clip_id, u.id)
            _set_clip_uploaded(clip_id)
            return u.id

    if clip.status not in ("rendered", "scheduled"):
        raise UploadError(f"clip {clip_id} is {clip.status!r}; only rendered/scheduled clips can be uploaded")

    # 2. guards
    strat = _load_strategy()
    cap = strategy_mod.upload_cap(strat)
    if uploads_today() >= cap:
        raise UploadCapReached(f"upload cap reached ({cap} today)")
    if not settings.dry_run:
        if quota_exhausted_today():
            raise QuotaExhausted("YouTube quota exhausted for today (Pacific)")
        if not youtube_auth.quota_allows("videos.insert"):
            _stop_for_quota("local_tally", f"tally {youtube_auth.quota_used()} of {youtube_auth.daily_quota_limit()}")
            raise QuotaExhausted("local quota tally would exceed the daily limit")

    source = db.get_source(clip.source_id)
    try:
        path = _check_render(clip, strict=not settings.dry_run)
    except _Permanent as exc:
        db.set_clip_status(clip_id, "failed", str(exc), force=clip.status not in ("rendered", "scheduled"))
        db.log_event(MODULE, f"clip {clip_id} not uploadable: {exc}", level="error", data={"clip_id": clip_id})
        raise UploadError(str(exc)) from exc

    # 3. crash recovery for live rows left 'queued'
    youtube = None
    if not settings.dry_run:
        holder: dict[str, Any] = {}

        def lazy() -> Any:
            if "s" not in holder:
                holder["s"] = _service()
            return holder["s"]

        from googleapiclient.errors import HttpError

        try:
            recovered = _reconcile_orphans(clip_id, lazy)
        except youtube_auth.YouTubeAuthError as exc:
            raise UploadAuthError(str(exc)) from exc
        except HttpError as exc:
            raise UploadError(f"could not verify an interrupted upload of clip {clip_id}: {exc}") from exc
        if recovered is not None:
            _set_clip_uploaded(clip_id)
            return recovered
        youtube = holder.get("s")

    slot = _resolve_slot(publish_at, immediate)
    body = build_metadata(clip, source, settings=settings, strategy=strat, publish_at=slot)
    privacy = body["status"]["privacyStatus"]
    # atomic check-and-insert: a concurrent process (scheduler + CLI) cannot start a second insert
    upload_id = db.claim_upload(clip_id, privacy=privacy, publish_at=db.to_iso(slot) if slot else None,
                                dry_run=settings.dry_run)
    if upload_id is None:
        raise UploadError(f"clip {clip_id} is already being uploaded (or was) by another process")

    if settings.dry_run:
        video_id = f"dryrun-{clip_id}"
        if path is None:
            db.log_event(MODULE, f"dry run: render file missing for clip {clip_id}", level="warning",
                         data={"clip_id": clip_id, "render_path": clip.render_path})
        db.mark_upload_succeeded(upload_id, video_id, quota_units=0)
        _set_clip_uploaded(clip_id)
        db.log_event(MODULE, f"dry run: would upload clip {clip_id}", data={
            "clip_id": clip_id, "upload_id": upload_id, "video_id": video_id,
            "publish_at": db.to_iso(slot) if slot else None, "privacy": privacy, "body": body})
        return upload_id

    return _upload_live(clip, upload_id, path, body, slot, youtube)


def _upload_live(clip: db.Clip, upload_id: int, path: Path | None, body: dict, slot: datetime | None,
                 youtube: Any) -> int:
    from google.auth.exceptions import RefreshError
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    cost = youtube_auth.QUOTA_COSTS["videos.insert"]
    clip_id = clip.id
    try:
        youtube = youtube or _service()
    except youtube_auth.YouTubeAuthError as exc:
        db.mark_upload_failed(upload_id, f"auth: {exc}")
        raise UploadAuthError(str(exc)) from exc

    media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=CHUNK_SIZE, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media,
                                      notifySubscribers=True)
    try:
        response = _execute_insert(request)
    except HttpError as exc:
        kind = _classify(exc)
        status, reasons, message = _error_reasons(exc)
        err = f"HTTP {status} {sorted(reasons)}: {message}"
        # Calls that reach the API are billed even when rejected; count conservatively.
        youtube_auth.record_quota("videos.insert", units=cost)
        db.mark_upload_failed(upload_id, err, quota_units=cost)
        if kind == "quota":
            reason = "uploadLimitExceeded" if "uploadLimitExceeded" in reasons else "quotaExceeded"
            _stop_for_quota(reason, err)
            raise QuotaExhausted(err) from exc
        if kind == "auth":
            youtube_auth.report_auth_problem(f"YouTube rejected our credentials during upload ({message}).",
                                             error=err)
            raise UploadAuthError(err) from exc
        db.set_clip_status(clip_id, "failed", err[:500])
        db.log_event(MODULE, f"upload of clip {clip_id} rejected", level="error",
                     data={"clip_id": clip_id, "upload_id": upload_id, "error": err})
        raise UploadError(err) from exc
    except _RetriesExhausted as exc:
        youtube_auth.record_quota("videos.insert", units=cost)
        if getattr(request, "resumable_uri", None):
            # The resumable session existed: YouTube may have finished the video while our reads
            # failed. Leave the row 'queued' so the next attempt (after ORPHAN_AGE) looks for the
            # marker tag on the channel instead of uploading a duplicate.
            db.update_upload(upload_id, error=f"outcome unknown, will verify on channel: {exc}"[:500],
                             quota_units=cost)
            db.log_event(MODULE, f"upload of clip {clip_id} interrupted mid-session; will verify before retrying",
                         level="error", data={"clip_id": clip_id, "upload_id": upload_id, "error": str(exc)})
            raise UploadError(str(exc)) from exc
        db.mark_upload_failed(upload_id, str(exc), quota_units=cost)
        db.log_event(MODULE, f"upload of clip {clip_id} failed after retries; will retry later",
                     level="error", data={"clip_id": clip_id, "upload_id": upload_id, "error": str(exc)})
        raise UploadError(str(exc)) from exc
    except youtube_auth.YouTubeAuthError as exc:
        db.mark_upload_failed(upload_id, f"auth: {exc}")
        raise UploadAuthError(str(exc)) from exc
    except RefreshError as exc:
        db.mark_upload_failed(upload_id, f"auth: {exc}")
        youtube_auth.report_auth_problem("YouTube refresh token was revoked or expired during an upload.",
                                         error=str(exc))
        raise UploadAuthError(str(exc)) from exc
    except _Permanent as exc:
        youtube_auth.record_quota("videos.insert", units=cost)
        db.mark_upload_failed(upload_id, str(exc), quota_units=cost)
        db.log_event(MODULE, f"upload of clip {clip_id} failed", level="error",
                     data={"clip_id": clip_id, "upload_id": upload_id, "error": str(exc)})
        raise UploadError(str(exc)) from exc

    video_id = response["id"]
    youtube_auth.record_quota("videos.insert", units=cost)
    # The video exists now: make the DB write as robust as we can (crash here -> marker-tag recovery).
    for attempt in range(3):
        try:
            db.mark_upload_succeeded(upload_id, video_id, quota_units=cost)
            break
        except Exception:
            if attempt == 2:
                log.critical("video %s uploaded for clip %s but DB write failed", video_id, clip_id)
                raise
            _sleep(1.0)
    _set_clip_uploaded(clip_id)
    returned = response.get("status") or {}
    data = {"clip_id": clip_id, "upload_id": upload_id, "video_id": video_id,
            "publish_at": db.to_iso(slot) if slot else None, "privacy": body["status"]["privacyStatus"],
            "returned_privacy": returned.get("privacyStatus"), "quota_tally": youtube_auth.quota_used()}
    db.log_event(MODULE, f"uploaded clip {clip_id} as {video_id}", data=data)
    return upload_id
