"""Download candidate sources with yt-dlp, behind a strict licence gate.

Pipeline stage ``candidate -> fetched`` (or ``rejected`` / ``failed``).

:func:`fetch_source` always *probes* metadata first (no media download), runs
:func:`check_licence` on it, and only downloads when the licence decision is
positive. The downloaded video lands in ``settings.downloads_dir/<id>/video.mp4``
next to ``info.json`` (the sanitised yt-dlp metadata, kept for audit).

:func:`check_licence` is pure: it looks only at the yt-dlp info dict and the
strategy, so every rule is unit-testable. Licence policy (see CLAUDE.md):

* **US federal government works** (``nasa``, ``congress``, ``dvids``, ``nara``,
  ``us_gov_other``) are public domain only when the government produced them.
  The publisher must pass ``strategy.is_publisher_allowed`` (official channel or
  allowlisted non-YouTube domain); a YouTube URL never passes on its domain
  alone, and a channel matched by *name* whose allowlisted ``channel_id``
  differs is treated as an impostor. The description must not assert
  copyright, "all rights reserved" or licensed/third-party music.
* **CC BY YouTube** (``cc_by_youtube``): yt-dlp's ``license`` must say Creative
  Commons Attribution without NonCommercial / NoDerivs / ShareAlike terms.
* Always rejected: blocklisted rebroadcasters (C-SPAN, news outlets), playlists,
  live / upcoming / still-processing streams, age-restricted or members-only
  videos, and durations that are too short to clip or longer than
  :data:`MAX_DURATION_SEC`.

Direct file links (e.g. NASA Image Library ``.mp4`` URLs) go through yt-dlp's generic
extractor, which returns no description. :func:`fetch_source` therefore also feeds the
description and credits the scout stored on the source row into the licence check
(:func:`licence_info`), so copyright / music notices there still reject the source.

Every decision (approved or not) carries a human-readable ``reason`` and an
``evidence`` dict with the raw fields that justified it; both are stored on the
source row (``status_reason`` / ``licence_evidence``) and in the event log.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import db
import strategy as strategy_mod
from config import get_settings
from strategy import Strategy

log = logging.getLogger(__name__)

MODULE = "fetch"

GOV_SOURCE_TYPES = frozenset({"nasa", "congress", "dvids", "nara", "us_gov_other"})
CC_BY_SOURCE_TYPES = frozenset({"cc_by_youtube"})

LICENCE_US_GOV = "us_gov_public_domain"
LICENCE_CC_BY = "cc_by"

MAX_DURATION_SEC = 3 * 3600          # longer sources are almost always livestream dumps
MAX_HEIGHT = 1080
MAX_FILESIZE_BYTES = 6 * 1024**3     # yt-dlp skips formats/files larger than this (disk safety)

_YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")

# yt-dlp format selector: <=1080p, prefer mp4/m4a so the merge is a cheap remux.
# ``height<=?`` keeps formats with unknown height (direct .mp4 links have none).
FORMAT_SELECTOR = (
    f"bv*[height<=?{MAX_HEIGHT}][ext=mp4]+ba[ext=m4a]"
    f"/b[height<=?{MAX_HEIGHT}][ext=mp4]"
    f"/bv*[height<=?{MAX_HEIGHT}]+ba"
    f"/b[height<=?{MAX_HEIGHT}]"
)

# Rebroadcasters that are never government works, whatever the strategy file says.
_BUILTIN_BLOCKED = ("cspan",)

_DESCRIPTION_EXCERPT_CHARS = 500

# Phrases that *negate* copyright ("not subject to copyright") are removed before scanning.
_COPYRIGHT_NEGATIONS = re.compile(
    r"(not|isn'?t|aren'?t|never|no|without|free of|free from)\s+(be\s+)?(subject\s+to\s+)?"
    r"(any\s+)?(known\s+)?copyright(s|ed)?(\s+(protection|restrictions?))?",
    re.IGNORECASE,
)
# (pattern, label). Gov sources: any of these rejects.
_GOV_RED_FLAGS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"©|\(c\)\s*(19|20)\d\d", re.IGNORECASE), "copyright symbol"),
    (re.compile(r"\bcopyright(ed)?\b", re.IGNORECASE), "copyright notice"),
    (re.compile(r"\ball rights reserved\b", re.IGNORECASE), "all rights reserved"),
    (re.compile(r"\b(licensed|copyrighted|production)\s+music\b", re.IGNORECASE), "licensed music"),
    (re.compile(r"\bmusic\s*(credit|credits|by|courtesy|licensed|provided|:)", re.IGNORECASE), "music credit"),
    (re.compile(r"\b(universal production music|killer tracks|epidemic sound|artlist|audio network|"
                r"musicbed|premiumbeat|audiojungle)\b", re.IGNORECASE), "commercial music library"),
    (re.compile(r"\bused (with|by) permission\b", re.IGNORECASE), "used with permission"),
    (re.compile(r"\bused under licen[cs]e\b", re.IGNORECASE), "used under licence"),
    # C-SPAN footage is C-SPAN's copyright whoever uploads it.
    (re.compile(r"\bc[-\s]?span\b", re.IGNORECASE), "C-SPAN footage"),
)
# CC BY sources are copyrighted-and-licensed, so a bare "©" is fine; these still contradict CC BY reuse.
_CC_RED_FLAGS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (p, label) for p, label in _GOV_RED_FLAGS
    if label not in ("copyright symbol", "copyright notice")
) + (
    # The uploader restricting reuse in the description contradicts a bare CC BY licence field.
    (re.compile(r"\bnon[-\s]?commercial\b|\bcc[-\s]?by[-\s]?(nc|nd|sa)\b|\bno[-\s]?deriv(ative)?s?\b|"
                r"\bshare[-\s]?alike\b", re.IGNORECASE), "restrictive licence terms"),
    (re.compile(r"\b(do not|don'?t|may not)\s+(re-?upload|re-?use|reuse|repost|use)\b", re.IGNORECASE),
     "reuse prohibited"),
)

_BLOCKING_LIVE_STATUSES = {"is_live": "a live stream", "is_upcoming": "an upcoming premiere/stream",
                           "post_live": "a stream that is still processing"}
_BLOCKING_AVAILABILITY = {"premium_only", "subscriber_only", "needs_auth", "private"}

# info.json: drop bulky keys that are useless for audit.
_INFO_DROP_KEYS = ("formats", "thumbnails", "automatic_captions", "subtitles", "requested_formats",
                   "requested_downloads", "http_headers", "heatmap", "fragments")


# ---------------------------------------------------------------------------
# types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LicenceDecision:
    """Outcome of :func:`check_licence`. ``licence`` is set only when ``ok``."""

    ok: bool
    licence: str | None
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


class LicenceRejected(Exception):
    """The source failed the licence gate; it was marked ``rejected`` and never downloaded."""

    def __init__(self, message: str, decision: LicenceDecision | None = None):
        super().__init__(message)
        self.decision = decision


class FetchError(Exception):
    """Probing or downloading failed (the source is ``failed``) or the source is in the wrong state."""


# ---------------------------------------------------------------------------
# yt-dlp wrapper (tests monkeypatch this)
# ---------------------------------------------------------------------------

def _ydl_options(download: bool, outdir: Path | None) -> dict[str, Any]:
    settings = get_settings()
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "format": FORMAT_SELECTOR,
        "merge_output_format": "mp4",
        "max_filesize": MAX_FILESIZE_BYTES,
        "continuedl": True,          # resume .part files after an interrupted run
        "overwrites": False,
        "writeinfojson": False,      # we write our own sanitised info.json
        "writesubtitles": False,
        "writethumbnail": False,
    }
    ffmpeg = settings.ffmpeg_bin
    if os.sep in ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    if download:
        if outdir is None:
            raise ValueError("outdir is required when download=True")
        opts["outtmpl"] = {"default": str(outdir / "video.%(ext)s")}
        opts["paths"] = {"home": str(outdir), "temp": str(outdir)}
        # Guarantee an .mp4 container even if the selected format was webm/mkv.
        opts["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]
    return opts


def extract_info(url: str, *, download: bool, outdir: Path | None = None) -> dict[str, Any]:
    """Run yt-dlp on ``url``; return its sanitised info dict.

    ``download=False`` only probes metadata. ``download=True`` writes
    ``outdir/video.mp4`` (<= 1080p, audio merged). yt-dlp exceptions propagate.
    """
    import yt_dlp  # heavy optional import: never at module import time

    with yt_dlp.YoutubeDL(_ydl_options(download, outdir)) as ydl:
        info = ydl.extract_info(url, download=download)
        return ydl.sanitize_info(info)


def probe_duration(path: Path) -> float | None:
    """Media duration in seconds via ``ffprobe``, or None if it cannot be measured."""
    ffmpeg = get_settings().ffmpeg_bin
    ffprobe = ffmpeg[: -len("ffmpeg")] + "ffprobe" if ffmpeg.endswith("ffmpeg") else "ffprobe"
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout
        value = json.loads(out).get("format", {}).get("duration")
        return float(value) if value not in (None, "N/A") else None
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# licence gate (pure)
# ---------------------------------------------------------------------------

def _host(url: str | None) -> str:
    try:
        return (urlparse(url).hostname or "").lower().rstrip(".") if url else ""
    except ValueError:
        return ""


def _is_youtube_host(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in _YOUTUBE_HOSTS)


# Latin look-alikes (Cyrillic / Greek) that NFKC does not fold, so "С-SPAN" (Cyrillic С) still
# normalises to "cspan".
_HOMOGLYPHS = str.maketrans({
    "а": "a", "в": "b", "с": "c", "е": "e", "н": "h", "і": "i", "ј": "j", "к": "k", "м": "m", "о": "o",
    "р": "p", "ѕ": "s", "т": "t", "у": "y", "х": "x", "ԁ": "d", "ԛ": "q", "ԝ": "w",
    "α": "a", "β": "b", "ε": "e", "η": "n", "ι": "i", "κ": "k", "μ": "m", "ν": "v", "ο": "o", "ρ": "p",
    "τ": "t", "υ": "u", "χ": "x", "ζ": "z",
})


def _norm_name(name: str | None) -> str:
    """Compare-key for channel names: NFKC, casefold, homoglyphs folded, only [a-z0-9] kept."""
    text = unicodedata.normalize("NFKC", name or "").casefold().translate(_HOMOGLYPHS)
    return re.sub(r"[^a-z0-9]", "", text)


def _handle(info: Mapping[str, Any]) -> str | None:
    for key in ("uploader_id", "channel_handle"):
        v = info.get(key)
        if isinstance(v, str) and v.startswith("@"):
            return v
    url = info.get("uploader_url") or info.get("channel_url") or ""
    m = re.search(r"youtube\.com/(@[\w.\-]+)", str(url))
    return m.group(1) if m else None


def _red_flags(description: str, patterns: tuple[tuple[re.Pattern[str], str], ...]) -> list[dict[str, str]]:
    text = _COPYRIGHT_NEGATIONS.sub(" ", description)
    hits: list[dict[str, str]] = []
    for pattern, label in patterns:
        m = pattern.search(text)
        if m:
            lo, hi = max(0, m.start() - 60), min(len(text), m.end() + 60)
            hits.append({"flag": label, "match": m.group(0), "context": " ".join(text[lo:hi].split())})
    return hits


def _description_excerpt(description: str, flags: list[dict[str, str]]) -> str:
    if flags:
        return flags[0]["context"]
    return description[:_DESCRIPTION_EXCERPT_CHARS]


def _licence_terms(licence_text: str) -> set[str]:
    """Lower-case word tokens of a licence string ("CC BY-NC-SA 4.0" -> {cc, by, nc, sa, 4, 0})."""
    return set(re.findall(r"[a-z0-9]+", licence_text.casefold()))


def _check_cc_by_licence(licence_text: str | None) -> tuple[bool, str]:
    if not licence_text or not str(licence_text).strip():
        return False, "no licence reported (YouTube default is the Standard YouTube License)"
    text = str(licence_text).casefold()
    if "standard youtube" in text:
        return False, f"Standard YouTube License ({licence_text!r}), not Creative Commons"
    terms = _licence_terms(text)
    compact = re.sub(r"[^a-z]", "", text)
    if any(t in compact for t in ("noncommercial", "noderiv", "sharealike")) or terms & {"nc", "nd", "sa"}:
        return False, f"licence {licence_text!r} has NonCommercial/NoDerivs/ShareAlike terms (CC BY only)"
    if "creativecommonsattribution" in compact or ({"cc", "by"} <= terms):
        return True, f"licence {licence_text!r} is CC BY"
    return False, f"licence {licence_text!r} is not Creative Commons Attribution"


def _allowlist_impostor(strategy: Strategy, source_type: str, info: Mapping[str, Any]) -> str | None:
    """A YouTube channel that matches an allowlisted *name/handle* but not its pinned ``channel_id``."""
    cid = info.get("channel_id")
    name = _norm_name(info.get("channel") or info.get("uploader"))
    handle = (_handle(info) or "").casefold()
    for ch in strategy.allowlist_for(source_type).get("channels", []):
        pinned = ch.get("channel_id")
        if not pinned:
            continue
        name_hit = name and _norm_name(ch.get("name")) == name
        handle_hit = handle and (ch.get("handle") or "").casefold() == handle
        if (name_hit or handle_hit) and cid != pinned:
            return (f"channel {info.get('channel') or info.get('uploader')!r} matches allowlisted "
                    f"{ch.get('name')!r} by name/handle but its channel_id {cid!r} != {pinned!r}")
    return None


def check_licence(info: Mapping[str, Any], strategy: Strategy, source_type: str) -> LicenceDecision:
    """Decide whether the video described by a yt-dlp ``info`` dict may be clipped.

    Pure function: no I/O. See the module docstring for the policy.
    """
    url = info.get("webpage_url") or info.get("original_url") or info.get("url") or ""
    host = _host(url)
    on_youtube = _is_youtube_host(host)
    extractor = str(info.get("extractor_key") or info.get("extractor") or "")
    channel = info.get("channel") or info.get("uploader")
    description = str(info.get("description") or "")
    duration = info.get("duration")
    handle = _handle(info)

    evidence: dict[str, Any] = {
        "extractor": extractor or None,
        "channel": info.get("channel"),
        "channel_id": info.get("channel_id"),
        "uploader": info.get("uploader"),
        "uploader_id": info.get("uploader_id"),
        "handle": handle,
        "license": info.get("license"),
        "webpage_url": url or None,
        "host": host or None,
        "video_id": info.get("id"),
        "source_type": source_type,
        "duration": duration,
        "live_status": info.get("live_status"),
        "is_live": info.get("is_live"),
        "age_limit": info.get("age_limit"),
        "availability": info.get("availability"),
        "matched_rule": None,
        "red_flags": [],
        "description_excerpt": description[:_DESCRIPTION_EXCERPT_CHARS],
    }

    def reject(reason: str) -> LicenceDecision:
        return LicenceDecision(False, None, reason, evidence)

    # --- shape / availability ------------------------------------------------
    if info.get("_type") in ("playlist", "multi_video") or info.get("entries") is not None:
        return reject("URL is a playlist, not a single video")
    if not url or not host:
        return reject("no webpage URL in metadata; cannot verify the publisher")
    if source_type not in GOV_SOURCE_TYPES | CC_BY_SOURCE_TYPES:
        return reject(f"unknown source type {source_type!r}")
    if info.get("is_live") or info.get("live_status") in _BLOCKING_LIVE_STATUSES:
        what = _BLOCKING_LIVE_STATUSES.get(info.get("live_status") or "", "a live stream")
        return reject(f"video is {what}; only finished uploads are eligible")
    age_limit = info.get("age_limit") or 0
    if isinstance(age_limit, (int, float)) and age_limit >= 18:
        return reject(f"age-restricted video (age_limit={age_limit})")
    if info.get("availability") in _BLOCKING_AVAILABILITY:
        return reject(f"video availability is {info.get('availability')!r}")

    # --- duration --------------------------------------------------------------
    min_dur = float(strategy.clip_length["min"])
    if duration is None:
        if on_youtube:
            return reject("YouTube metadata has no duration")
        # Direct file links often lack a duration; fetch_source re-checks after download.
    else:
        try:
            dur = float(duration)
        except (TypeError, ValueError):
            return reject(f"invalid duration {duration!r}")
        if dur < min_dur:
            return reject(f"duration {dur:.0f}s is shorter than the minimum clip length {min_dur:.0f}s")
        if dur > MAX_DURATION_SEC:
            return reject(f"duration {dur:.0f}s exceeds the {MAX_DURATION_SEC}s maximum")

    # --- blocklist (strategy + built-in rebroadcasters), checked on every name field -----
    blocked = {_norm_name(b) for b in strategy.get("blocklist_channels", [])}
    for name in {info.get("channel"), info.get("uploader")} - {None}:
        n = _norm_name(name)
        if n and (n in blocked or any(b in n for b in _BUILTIN_BLOCKED)):
            return reject(f"channel {name!r} is a blocklisted rebroadcaster, not the original publisher")

    # --- publisher allowlist -----------------------------------------------------------
    if on_youtube and source_type in GOV_SOURCE_TYPES:
        impostor = _allowlist_impostor(strategy, source_type, info)
        if impostor:
            return reject(impostor)
    ok, rule = strategy_mod.is_publisher_allowed(
        strategy, source_type, channel=channel, channel_id=info.get("channel_id"), handle=handle, url=url,
    )
    if not ok and info.get("uploader") and info.get("uploader") != channel:
        ok, rule = strategy_mod.is_publisher_allowed(
            strategy, source_type, channel=info.get("uploader"), channel_id=info.get("channel_id"),
            handle=handle, url=url,
        )
    evidence["matched_rule"] = rule
    if not ok:
        return reject(f"publisher not allowed: {rule}")
    if on_youtube and not extractor.casefold().startswith("youtube"):
        return reject(f"YouTube URL but extractor {extractor!r}; cannot trust channel metadata")

    # --- per-family licence rules -------------------------------------------------------
    if source_type in CC_BY_SOURCE_TYPES:
        if not on_youtube:
            return reject(f"cc_by_youtube source is not on YouTube (host {host!r})")
        cc_ok, cc_reason = _check_cc_by_licence(info.get("license"))
        if not cc_ok:
            return reject(cc_reason)
        flags = _red_flags(description, _CC_RED_FLAGS)
        evidence["red_flags"] = flags
        evidence["description_excerpt"] = _description_excerpt(description, flags)
        if flags:
            return reject("description contradicts CC BY reuse: " + ", ".join(f["flag"] for f in flags))
        return LicenceDecision(True, LICENCE_CC_BY, f"{cc_reason}; {rule}", evidence)

    # US federal government work
    if on_youtube:
        if not info.get("channel_id"):
            return reject("YouTube metadata has no channel_id; cannot verify the official channel")
        impostor = _allowlist_impostor(strategy, source_type, info)
        if impostor:
            return reject(impostor)
    flags = _red_flags(description, _GOV_RED_FLAGS)
    evidence["red_flags"] = flags
    evidence["description_excerpt"] = _description_excerpt(description, flags)
    if flags:
        return reject("description indicates third-party or copyrighted material: "
                      + ", ".join(f["flag"] for f in flags))
    return LicenceDecision(True, LICENCE_US_GOV,
                           f"US federal government work (17 U.S.C. 105); {rule}", evidence)


# ---------------------------------------------------------------------------
# fetch stage
# ---------------------------------------------------------------------------

# Scout metadata fields that hold publisher-supplied credit text worth scanning for notices.
_STORED_CREDIT_KEYS = ("secondary_creator", "photographer", "credit")


def licence_info(info: Mapping[str, Any], source: db.Source) -> dict[str, Any]:
    """``info`` with the source row's stored description / credits appended to ``description``.

    Pure. yt-dlp's own description stays first; text it already contains is not repeated.
    """
    own = str(info.get("description") or "")
    extra: list[str] = []
    if source.description and source.description.strip() and source.description.strip() not in own:
        extra.append(source.description.strip())
    meta = source.metadata if isinstance(source.metadata, dict) else {}
    for key in _STORED_CREDIT_KEYS:
        value = meta.get(key)
        if isinstance(value, str) and value.strip() and value.strip() not in own:
            extra.append(f"{key.replace('_', ' ')}: {value.strip()}")
    if not extra:
        return dict(info)
    return {**info, "description": "\n\n".join(([own] if own else []) + extra)}


def source_dir(source_id: int) -> Path:
    return get_settings().downloads_dir / str(source_id)


def _published_at(info: Mapping[str, Any]) -> str | None:
    ts = info.get("timestamp") or info.get("release_timestamp")
    if isinstance(ts, (int, float)):
        return db.to_iso(datetime.fromtimestamp(ts, tz=UTC))
    d = info.get("upload_date") or info.get("release_date")
    if isinstance(d, str) and re.fullmatch(r"\d{8}", d):
        return f"{d[:4]}-{d[4:6]}-{d[6:]}T00:00:00Z"
    return None


def _platform(info: Mapping[str, Any], current: str | None) -> str | None:
    if str(info.get("extractor_key") or "").casefold().startswith("youtube"):
        return "youtube"
    return current or (_host(info.get("webpage_url")) or None)


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False, sort_keys=True, default=str)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _fail(source_id: int, reason: str, *, data: dict[str, Any] | None = None) -> FetchError:
    with db.transaction() as conn:
        db.set_source_status(source_id, "failed", reason, conn=conn)
        db.log_event(MODULE, f"source {source_id} failed: {reason}", level="error",
                     data={"source_id": source_id, **(data or {})}, conn=conn)
    return FetchError(f"source {source_id}: {reason}")


def _reject(source_id: int, decision: LicenceDecision, *, stage: str) -> LicenceRejected:
    with db.transaction() as conn:
        db.update_source(source_id, licence=None, licence_evidence=decision.evidence, conn=conn)
        db.set_source_status(source_id, "rejected", decision.reason, conn=conn)
        db.log_event(MODULE, f"source {source_id} rejected ({stage}): {decision.reason}", level="warning",
                     data={"source_id": source_id, "stage": stage, "evidence": decision.evidence}, conn=conn)
    return LicenceRejected(f"source {source_id}: {decision.reason}", decision)


def _locate_video(outdir: Path, info: Mapping[str, Any]) -> Path | None:
    target = outdir / "video.mp4"
    if target.is_file() and target.stat().st_size > 0:
        return target
    candidates: list[str] = [d.get("filepath") for d in info.get("requested_downloads") or [] if d.get("filepath")]
    candidates += [info.get("filepath"), info.get("_filename")]  # type: ignore[list-item]
    for c in candidates:
        if c and Path(c).suffix.lower() == ".mp4" and Path(c).is_file() and Path(c).stat().st_size > 0:
            shutil.move(str(c), target)
            return target
    return None


def _is_age_gate_error(message: str) -> bool:
    m = message.casefold()
    return "confirm your age" in m or "age-restricted" in m or "age restricted" in m


def fetch_source(source_id: int, strategy: Strategy | None = None) -> Path:
    """Licence-check and download a ``candidate`` source; return the absolute video path.

    * Already ``fetched`` (or later) with the video on disk: returns it, no work.
    * ``rejected``: raises :class:`LicenceRejected`. Any other non-candidate
      status: raises :class:`FetchError` without touching the row.
    * Licence not ok (at probe *or* re-checked on the download metadata): status
      ``rejected`` with the reason + evidence, raises :class:`LicenceRejected`;
      nothing is downloaded on a probe rejection.
    * yt-dlp / filesystem errors: status ``failed`` with the reason, raises :class:`FetchError`.

    A partially downloaded file from an interrupted run is resumed by yt-dlp; a
    complete ``video.mp4`` already in the source directory is reused.
    """
    src = db.require_source(source_id)
    if src.status in ("fetched", "transcribed", "picked", "done"):
        if src.video_path and Path(src.video_path).is_file():
            log.debug("source %s already fetched: %s", source_id, src.video_path)
            return Path(src.video_path)
        raise FetchError(f"source {source_id} is {src.status!r} but its video {src.video_path!r} is missing")
    if src.status == "rejected":
        raise LicenceRejected(f"source {source_id} was rejected: {src.status_reason}")
    if src.status != "candidate":
        raise FetchError(f"source {source_id} is {src.status!r}; only candidates can be fetched "
                         "(set it back to 'candidate' to retry)")

    strat = strategy if strategy is not None else strategy_mod.load_strategy()
    settings = get_settings()

    # 1. probe metadata (no download) -------------------------------------------------
    try:
        probe = extract_info(src.url, download=False)
    except Exception as exc:  # yt-dlp raises many types (DownloadError, ExtractorError, network...)
        msg = f"metadata probe failed: {exc}"
        if _is_age_gate_error(str(exc)):
            raise _reject(source_id, LicenceDecision(False, None, f"age-restricted video ({exc})",
                                                     {"webpage_url": src.url, "error": str(exc)}),
                          stage="probe")
        raise _fail(source_id, msg, data={"url": src.url, "error_type": type(exc).__name__})
    probe = dict(probe or {})
    probe.setdefault("webpage_url", src.url)

    decision = check_licence(licence_info(probe, src), strat, src.source_type)
    if not decision.ok:
        raise _reject(source_id, decision, stage="probe")

    # 2. download ------------------------------------------------------------------------
    outdir = source_dir(source_id)
    video = outdir / "video.mp4"
    try:
        outdir.mkdir(parents=True, exist_ok=True)
        if video.is_file() and video.stat().st_size > 0:
            log.info("source %s: reusing existing %s", source_id, video)
            info = probe
        else:
            info = dict(extract_info(src.url, download=True, outdir=outdir) or {})
            info.setdefault("webpage_url", src.url)
    except Exception as exc:
        raise _fail(source_id, f"download failed: {exc}", data={"url": src.url, "error_type": type(exc).__name__})

    # Re-check against the metadata of what was actually downloaded (guards against
    # the video changing between probe and download).
    final = check_licence(licence_info(info, src), strat, src.source_type) if info is not probe else decision
    if not final.ok:
        video.unlink(missing_ok=True)
        raise _reject(source_id, final, stage="download")

    located = _locate_video(outdir, info)
    if located is None:
        raise _fail(source_id, f"download produced no non-empty mp4 in {outdir}")
    video = located.resolve()

    # 3. verify duration when the metadata had none (direct file links) -------------------
    duration = info.get("duration") or probe.get("duration")
    if duration is None:
        duration = probe_duration(video)
        if duration is not None:
            checked = check_licence(licence_info({**info, "duration": duration}, src), strat, src.source_type)
            if not checked.ok:
                video.unlink(missing_ok=True)
                raise _reject(source_id, checked, stage="post-download duration")

    # 4. persist --------------------------------------------------------------------------
    info_path = outdir / "info.json"
    try:
        _write_json_atomic(info_path, {k: v for k, v in info.items() if k not in _INFO_DROP_KEYS})
    except OSError as exc:
        raise _fail(source_id, f"could not write info.json: {exc}")

    metadata = dict(src.metadata or {}) if isinstance(src.metadata, dict) else {}
    metadata["fetch"] = {
        "info_path": str(info_path.resolve()),
        "webpage_url": info.get("webpage_url"),
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "format_id": info.get("format_id"),
        "width": info.get("width"),
        "height": info.get("height"),
        "fps": info.get("fps"),
        "vcodec": info.get("vcodec"),
        "acodec": info.get("acodec"),
        "filesize": video.stat().st_size,
        "fetched_at": db.now_iso(),
    }
    updates: dict[str, Any] = {
        "licence": final.licence,
        "licence_evidence": final.evidence,
        "video_path": str(video),
        "duration_sec": float(duration) if duration is not None else src.duration_sec,
        "title": info.get("title") or src.title,
        "description": info.get("description") if info.get("description") is not None else src.description,
        "channel": info.get("channel") or info.get("uploader") or src.channel,
        "channel_id": info.get("channel_id") or src.channel_id,
        "external_id": info.get("id") or src.external_id,
        "published_at": _published_at(info) or src.published_at,
        "platform": _platform(info, src.platform),
        "metadata": metadata,
    }
    with db.transaction() as conn:
        db.update_source(source_id, conn=conn, **updates)
        db.set_source_status(source_id, "fetched", final.reason, conn=conn)
        db.log_event(MODULE, f"fetched source {source_id}: {updates['title']!r} ({final.licence})",
                     data={"source_id": source_id, "video_path": str(video), "licence": final.licence,
                           "reason": final.reason, "duration_sec": updates["duration_sec"]},
                     conn=conn)
    return video
