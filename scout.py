"""Find candidate source videos for every strategy topic and record them as ``candidate`` sources.

No LLM. The scout only *nominates*: ``fetch.check_licence`` is the real licence gate, so
nothing here is ever marked licence-verified (``sources.licence`` stays NULL and
``metadata.licence_verified`` is False).

Searchers (``SEARCHERS``, one per source type; tests replace entries):

* ``nasa``: the NASA Image and Video Library API (``images-api.nasa.gov/search`` then the
  ``/asset/{nasa_id}`` manifest for the mp4 URL and ``metadata.json`` for the duration),
  plus YouTube search filtered to the NASA channels on the strategy allowlist.
* ``congress`` (and other government types): YouTube search (yt-dlp ``ytsearchN:``) plus the
  recent uploads of allowlisted channels that have a handle or channel id, filtered to
  allowlisted publishers.
* ``cc_by_youtube``: YouTube search restricted to Creative Commons videos.

  **Choice**: by default this uses yt-dlp on the YouTube results page with the Creative
  Commons filter (``sp=EgIwAQ%3D%3D``). It costs **zero** Data API quota, so it never competes
  with uploads (10,000 units/day, 1,600 per upload). The alternative is the Data API
  ``search.list(videoLicense=creativeCommon, type=video)`` at 100 units per call (+1 per
  ``videos.list`` for durations); it is implemented in :func:`search_cc_youtube_data_api` with
  an explicit per-day search budget (``SCOUT_CC_API_MAX_SEARCHES_PER_DAY``, default 2) and is
  selected with ``SCOUT_CC_BACKEND=data_api`` (needs ``YOUTUBE_API_KEY``) or by assigning
  ``scout.SEARCHERS["cc_by_youtube"] = scout.search_cc_youtube_data_api``.

All network access goes through :func:`http_get_json` and :func:`ytdlp_flat`, which tests
replace. Each searcher call is isolated: an exception is logged and the run continues.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, quote, quote_plus, urlparse

import db
import strategy as strategy_mod
from config import get_settings
from strategy import Strategy

log = logging.getLogger(__name__)

MODULE = "scout"
DEFAULT_MAX_CANDIDATES = 10
MAX_SOURCE_SEC = 3 * 3600
TOPIC_SHARE = 0.4                 # one topic may take at most this share of a run's cap
SEARCH_LIMIT = 8                  # results requested per (topic, source type) search
MAX_SEARCHER_FAILURES = 2         # consecutive failures before a source type is skipped for the run
CHANNEL_REJECT_LIMIT = 3          # skip channels with this many licence/allowlist rejections
HTTP_TIMEOUT = 20.0

NASA_API = "https://images-api.nasa.gov"
YT_CC_FILTER = "EgIwAQ%3D%3D"     # YouTube results filter "Creative Commons" (url-encoded protobuf)
YT_DATA_API = "https://www.googleapis.com/youtube/v3"
CC_API_SEARCH_UNITS = 100

_COPYRIGHT_RE = re.compile(r"(©|\(c\)\s*\d{4}|copyright|all rights reserved|licensed music|used with permission)", re.I)


@dataclass
class Candidate:
    url: str
    source_type: str
    platform: str
    external_id: str | None
    channel: str | None
    channel_id: str | None
    title: str | None
    duration_sec: float | None
    published_at: str | None          # UTC ISO or None
    topic: str | None
    metadata: dict = field(default_factory=dict)
    # extensions (defaults keep the contract signature valid)
    description: str | None = None
    handle: str | None = None


Searcher = Callable[[str, int, Strategy], list[Candidate]]


# ---------------------------------------------------------------------------
# transports (tests replace these)
# ---------------------------------------------------------------------------

def http_get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    """GET ``url`` and decode JSON. The only HTTP entry point of this module."""
    import httpx

    with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True,
                      headers={"User-Agent": "clipagent-scout/0.1"}) as client:
        r = client.get(url, params=params)
        r.raise_for_status()
        return r.json()


def ytdlp_flat(url: str, limit: int) -> dict[str, Any]:
    """yt-dlp flat extraction of a search / results / channel URL. Returns the playlist info dict."""
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": "in_playlist",
            "playlistend": limit, "noplaylist": False, "socket_timeout": HTTP_TIMEOUT}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return ydl.sanitize_info(info) or {}


# ---------------------------------------------------------------------------
# per-run context (dedupe hints for searchers; cache for channel listings)
# ---------------------------------------------------------------------------

@dataclass
class _RunContext:
    known_urls: set[str] = field(default_factory=set)
    known_ids: set[tuple[str, str]] = field(default_factory=set)
    channel_cache: dict[str, dict[str, Any]] = field(default_factory=dict)


_ctx: _RunContext | None = None


def _is_known(platform: str, external_id: str | None) -> bool:
    return bool(_ctx and external_id and (platform, external_id) in _ctx.known_ids)


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------

def _yt_id(url: str | None) -> str | None:
    if not url:
        return None
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if host.endswith("youtu.be"):
        return p.path.strip("/").split("/")[0] or None
    if "youtube" in host:
        if p.path == "/watch":
            return (parse_qs(p.query).get("v") or [None])[0]
        m = re.match(r"^/(shorts|live|embed|v)/([\w-]{6,})", p.path)
        if m:
            return m.group(2)
    if re.fullmatch(r"[\w-]{11}", url):
        return url
    return None


def canonical_youtube_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _iso_from_entry(e: dict[str, Any]) -> str | None:
    for k in ("release_timestamp", "timestamp"):
        if isinstance(e.get(k), (int, float)):
            return db.to_iso(datetime.fromtimestamp(e[k], UTC))
    ud = e.get("upload_date") or e.get("release_date")
    if isinstance(ud, str) and re.fullmatch(r"\d{8}", ud):
        return f"{ud[:4]}-{ud[4:6]}-{ud[6:]}T00:00:00Z"
    return None


def _norm_iso(s: str | None) -> str | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return db.to_iso(dt)


def parse_duration(value: Any) -> float | None:
    """Parse '0:03:24', '1:02:03.5', '204.0 s', '204', 'PT3M24S' -> seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if value > 0 else None
    s = str(value).strip()
    m = re.fullmatch(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", s)
    if m and any(m.groups()):
        h, mi, se = m.groups()
        return int(h or 0) * 3600 + int(mi or 0) * 60 + float(se or 0)
    m = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)", s)
    if m:
        return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:s|sec|seconds)?(?:\s*\(approx\))?", s)
    if m:
        return float(m.group(1))
    return None


def _entry_to_candidate(e: dict[str, Any], *, source_type: str, query: str,
                        playlist: dict[str, Any] | None = None, how: str) -> Candidate | None:
    if not isinstance(e, dict):
        return None
    vid = e.get("id") if e.get("ie_key", "Youtube") in ("Youtube", None) else None
    vid = vid or _yt_id(e.get("url") or e.get("webpage_url"))
    if not vid or e.get("_type") == "playlist" or len(str(vid)) != 11:
        return None  # channels / playlists in search results
    pl = playlist or {}
    channel = e.get("channel") or e.get("uploader") or pl.get("channel") or pl.get("uploader")
    channel_id = e.get("channel_id") or pl.get("channel_id")
    handle = e.get("uploader_id") or pl.get("uploader_id")
    if handle and not str(handle).startswith("@"):
        handle = None
    return Candidate(
        url=canonical_youtube_url(vid), source_type=source_type, platform="youtube", external_id=vid,
        channel=channel, channel_id=channel_id, title=e.get("title"),
        duration_sec=parse_duration(e.get("duration")), published_at=_iso_from_entry(e), topic=query,
        metadata={"found_via": how, "view_count": e.get("view_count"), "live_status": e.get("live_status"),
                  "original_url": e.get("url"), "licence_verified": False},
        description=e.get("description"), handle=handle,
    )


# ---------------------------------------------------------------------------
# NASA Image and Video Library
# ---------------------------------------------------------------------------

_NASA_PREF = ("~large.mp4", "~orig.mp4", "~medium.mp4", "~mobile.mp4", "~small.mp4", "~preview.mp4")


def _https(url: str) -> str:
    url = url.replace(" ", "%20")
    return "https://" + url[len("http://"):] if url.startswith("http://") else url


def nasa_pick_mp4(asset: dict[str, Any]) -> tuple[str | None, list[str], str | None]:
    """From an ``/asset/{id}`` manifest: (best mp4 url, all mp4 urls, metadata.json url)."""
    hrefs = [_https(i.get("href", "")) for i in asset.get("collection", {}).get("items", []) if i.get("href")]
    mp4s = [h for h in hrefs if h.lower().endswith(".mp4")]
    meta = next((h for h in hrefs if h.endswith("/metadata.json")), None)
    for suffix in _NASA_PREF:
        for h in mp4s:
            if h.endswith(suffix):
                return h, mp4s, meta
    return (mp4s[0] if mp4s else None), mp4s, meta


def nasa_duration(meta: dict[str, Any]) -> float | None:
    for k in ("Video:Duration", "QuickTime:Duration", "Composite:Duration", "QuickTime:TrackDuration",
              "Format:Duration", "Duration"):
        if k in meta:
            d = parse_duration(meta[k])
            if d:
                return d
    return None


def search_nasa_images(query: str, limit: int, strategy: Strategy) -> list[Candidate]:
    res = http_get_json(f"{NASA_API}/search", {"q": query, "media_type": "video", "page_size": max(limit * 3, 10)})
    items = res.get("collection", {}).get("items", []) or []
    out: list[Candidate] = []
    for it in items:
        if len(out) >= limit:
            break
        data = (it.get("data") or [{}])[0]
        nasa_id = data.get("nasa_id")
        if not nasa_id or data.get("media_type", "video") != "video" or _is_known("nasa_images", nasa_id):
            continue
        desc = data.get("description") or data.get("description_508") or ""
        if _COPYRIGHT_RE.search(desc):
            continue  # fetch would reject it anyway; don't spend requests on it
        asset = http_get_json(f"{NASA_API}/asset/{quote(nasa_id)}")
        mp4, mp4s, meta_url = nasa_pick_mp4(asset)
        if not mp4:
            continue
        duration = None
        if meta_url:
            try:
                duration = nasa_duration(http_get_json(meta_url) or {})
            except Exception as exc:  # duration is optional; fetch learns it anyway
                log.debug("nasa metadata.json failed for %s: %s", nasa_id, exc)
        out.append(Candidate(
            url=mp4, source_type="nasa", platform="nasa_images", external_id=nasa_id, channel="NASA",
            channel_id=None, title=data.get("title"), duration_sec=duration,
            published_at=_norm_iso(data.get("date_created")), topic=query, description=desc[:2000],
            metadata={"found_via": "nasa_images_api", "nasa_id": nasa_id, "center": data.get("center"),
                      "details_url": f"https://images.nasa.gov/details/{quote(nasa_id)}", "mp4_urls": mp4s,
                      "keywords": data.get("keywords"), "secondary_creator": data.get("secondary_creator"),
                      "licence_verified": False},
        ))
    return out


# ---------------------------------------------------------------------------
# official YouTube channels (allowlisted publishers)
# ---------------------------------------------------------------------------

def _channel_tab_url(ch: dict[str, Any]) -> str | None:
    if ch.get("channel_id"):
        return f"https://www.youtube.com/channel/{ch['channel_id']}/videos"
    if ch.get("handle"):
        return f"https://www.youtube.com/{ch['handle']}/videos"
    return None


def _query_matches(query: str, title: str | None) -> bool:
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2 and w not in {"the", "and"}]
    t = (title or "").lower()
    return bool(words) and sum(w in t for w in words) >= max(1, math.ceil(len(words) / 2))


def search_official_youtube(source_type: str) -> Searcher:
    """Searcher for a government source type: YouTube search + allowlisted channels' recent uploads."""

    def searcher(query: str, limit: int, strategy: Strategy) -> list[Candidate]:
        out: list[Candidate] = []
        n = min(50, max(limit * 4, 10))
        info = ytdlp_flat(f"ytsearch{n}:{query}", n)
        for e in info.get("entries") or []:
            c = _entry_to_candidate(e, source_type=source_type, query=query, how="ytsearch")
            if c and strategy_mod.is_publisher_allowed(strategy, source_type, channel=c.channel,
                                                       channel_id=c.channel_id, handle=c.handle, url=c.url)[0]:
                out.append(c)
        for ch in strategy.allowlist_for(source_type).get("channels", []):
            tab = _channel_tab_url(ch)
            if not tab:
                continue
            if _ctx is not None and tab in _ctx.channel_cache:
                pl = _ctx.channel_cache[tab]
            else:
                pl = ytdlp_flat(tab, 30)
                if _ctx is not None:
                    _ctx.channel_cache[tab] = pl
            for e in pl.get("entries") or []:
                if not _query_matches(query, e.get("title")):
                    continue
                c = _entry_to_candidate(e, source_type=source_type, query=query, playlist=pl, how="channel_uploads")
                if c:
                    c.channel = c.channel or ch.get("name")
                    c.channel_id = c.channel_id or ch.get("channel_id")
                    c.handle = c.handle or ch.get("handle")
                    out.append(c)
        return out[: limit * 2]

    return searcher


def search_nasa(query: str, limit: int, strategy: Strategy) -> list[Candidate]:
    """NASA: image library first; NASA YouTube channels second. Either half may fail alone."""
    out: list[Candidate] = []
    errors: list[str] = []
    for fn in (search_nasa_images, search_official_youtube("nasa")):
        try:
            out += fn(query, limit, strategy)
        except Exception as exc:
            errors.append(f"{getattr(fn, '__name__', 'nasa')}: {exc}")
            log.warning("nasa sub-search failed: %s", exc)
    if errors and not out:
        raise RuntimeError("; ".join(errors))
    return out


# ---------------------------------------------------------------------------
# Creative Commons YouTube
# ---------------------------------------------------------------------------

def cc_search_url(query: str) -> str:
    # sp must reach yt-dlp still url-encoded once (it is forwarded verbatim as innertube params)
    return f"https://www.youtube.com/results?search_query={quote_plus(query)}&sp={quote(YT_CC_FILTER, safe='')}"


def search_cc_youtube(query: str, limit: int, strategy: Strategy) -> list[Candidate]:
    """yt-dlp on the YouTube results page with the Creative Commons filter (0 Data API quota)."""
    n = min(50, max(limit * 3, 10))
    info = ytdlp_flat(cc_search_url(query), n)
    out = []
    for e in info.get("entries") or []:
        c = _entry_to_candidate(e, source_type="cc_by_youtube", query=query, how="youtube_cc_filter")
        if c:
            c.metadata["licence_hint"] = "youtube_search_creative_commons_filter"
            out.append(c)
    return out


def search_cc_youtube_data_api(query: str, limit: int, strategy: Strategy) -> list[Candidate]:
    """Alternative: Data API search.list(videoLicense=creativeCommon). 100 quota units per call,
    budgeted per local day via kv ``scout:cc_api_searches:<day>``."""
    settings = get_settings()
    key = settings.youtube_api_key
    if not key:
        raise RuntimeError("YOUTUBE_API_KEY not set for the Data API CC search backend")
    kv_key = f"scout:cc_api_searches:{db.local_today().isoformat()}"
    if db.kv_incr(kv_key) > settings.scout_cc_api_max_searches_per_day:
        log.info("cc data-api search budget used (%s/day)", settings.scout_cc_api_max_searches_per_day)
        return []
    res = http_get_json(f"{YT_DATA_API}/search", {
        "part": "snippet", "q": query, "type": "video", "videoLicense": "creativeCommon",
        "maxResults": min(50, max(limit * 2, 5)), "order": "relevance", "key": key})
    items = [i for i in res.get("items", []) if i.get("id", {}).get("videoId")]
    durations: dict[str, float | None] = {}
    if items:
        vids = ",".join(i["id"]["videoId"] for i in items)
        det = http_get_json(f"{YT_DATA_API}/videos", {"part": "contentDetails,status", "id": vids, "key": key})
        for v in det.get("items", []):
            if v.get("status", {}).get("license") == "creativeCommon":
                durations[v["id"]] = parse_duration(v.get("contentDetails", {}).get("duration"))
    out = []
    for i in items:
        vid, sn = i["id"]["videoId"], i.get("snippet", {})
        if vid not in durations:
            continue
        out.append(Candidate(
            url=canonical_youtube_url(vid), source_type="cc_by_youtube", platform="youtube", external_id=vid,
            channel=sn.get("channelTitle"), channel_id=sn.get("channelId"), title=sn.get("title"),
            duration_sec=durations[vid], published_at=_norm_iso(sn.get("publishedAt")), topic=query,
            description=sn.get("description"),
            metadata={"found_via": "youtube_data_api_cc", "licence_hint": "data_api_creativeCommon",
                      "quota_units": CC_API_SEARCH_UNITS + 1, "licence_verified": False},
        ))
    return out


def search_cc_by_youtube(query: str, limit: int, strategy: Strategy) -> list[Candidate]:
    if get_settings().scout_cc_backend == "data_api":
        return search_cc_youtube_data_api(query, limit, strategy)
    return search_cc_youtube(query, limit, strategy)


SEARCHERS: dict[str, Searcher] = {
    "nasa": search_nasa,
    "congress": search_official_youtube("congress"),
    "dvids": search_official_youtube("dvids"),
    "nara": search_official_youtube("nara"),
    "us_gov_other": search_official_youtube("us_gov_other"),
    "cc_by_youtube": search_cc_by_youtube,
}


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------

def _load_known() -> tuple[_RunContext, Counter]:
    ctx = _RunContext()
    rejected_channels: Counter = Counter()
    for s in db.list_sources():
        ctx.known_urls.add(s.url)
        if s.external_id:
            ctx.known_ids.add((s.platform or "", s.external_id))
            if s.platform == "youtube" or _yt_id(s.url):
                ctx.known_ids.add(("youtube", s.external_id))
        yid = _yt_id(s.url)
        if yid:
            ctx.known_ids.add(("youtube", yid))
        if s.status == "rejected" and s.channel_id:
            rejected_channels[s.channel_id] += 1
    return ctx, rejected_channels


def _prefilter(c: Candidate, strategy: Strategy, min_len: float) -> str | None:
    """Reason to drop the candidate, or None to keep it."""
    if c.metadata.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        return "live"
    if "/shorts/" in (c.metadata.get("original_url") or ""):
        return "short"
    ok, _ = strategy_mod.is_publisher_allowed(strategy, c.source_type, channel=c.channel, channel_id=c.channel_id,
                                              handle=c.handle, url=c.url)
    if not ok:
        return "publisher"
    if c.duration_sec is not None and not (min_len <= c.duration_sec <= MAX_SOURCE_SEC):
        return "duration"
    return None


def scout(max_candidates: int | None = None, strategy: Strategy | None = None) -> list[int]:
    """Search every strategy topic x source type and insert new ``candidate`` sources.

    Returns only the new source ids (at most ``max_candidates``, default 10), spread across topics.
    """
    global _ctx
    strat = strategy or strategy_mod.load_strategy()
    cap = DEFAULT_MAX_CANDIDATES if max_candidates is None else max(0, int(max_candidates))
    min_len = float(strat.clip_length["max"])
    ctx, rejected_channels = _load_known()
    _ctx = ctx
    dropped: Counter = Counter()
    errors: list[dict[str, str]] = []
    failures: Counter = Counter()
    found = 0
    pools: dict[str, list[Candidate]] = defaultdict(list)
    seen_urls: set[str] = set()
    seen_ids: set[tuple[str, str]] = set()
    enabled = set(strat.source_types)
    try:
        if cap == 0:
            return []
        for topic in strat.topics:
            query = topic["query"]
            for st in topic.get("source_types", []):
                if st not in enabled:
                    continue
                searcher = SEARCHERS.get(st)
                if searcher is None:
                    dropped["no_searcher"] += 1
                    continue
                if failures[st] >= MAX_SEARCHER_FAILURES:
                    dropped["searcher_disabled"] += 1
                    continue
                try:
                    results = searcher(query, max(SEARCH_LIMIT, 1), strat)
                    failures[st] = 0
                except Exception as exc:  # isolate: one searcher failing must not stop the others
                    failures[st] += 1
                    errors.append({"source_type": st, "query": query, "error": f"{type(exc).__name__}: {exc}"[:300]})
                    log.warning("scout searcher %s failed for %r: %s", st, query, exc)
                    continue
                for c in results:
                    found += 1
                    c.topic = c.topic or query
                    if c.source_type != st:
                        c.source_type = st
                    key = (c.platform, c.external_id or "")
                    yid = _yt_id(c.url)
                    if (c.url in ctx.known_urls or (c.external_id and key in ctx.known_ids)
                            or (yid and ("youtube", yid) in ctx.known_ids)):
                        dropped["known"] += 1
                        continue
                    if c.url in seen_urls or (c.external_id and key in seen_ids):
                        dropped["duplicate"] += 1
                        continue
                    if c.channel_id and rejected_channels[c.channel_id] >= CHANNEL_REJECT_LIMIT:
                        dropped["rejected_channel"] += 1
                        continue
                    reason = _prefilter(c, strat, min_len)
                    if reason:
                        dropped[reason] += 1
                        continue
                    seen_urls.add(c.url)
                    if c.external_id:
                        seen_ids.add(key)
                    pools[query].append(c)

        # newest first inside each topic
        for q in pools:
            pools[q].sort(key=lambda c: c.published_at or "", reverse=True)
            pools[q].sort(key=lambda c: c.published_at is None)

        # round-robin across topics, rotating the starting topic each run, with a per-topic share cap
        order = [t["query"] for t in strat.topics if pools.get(t["query"])]
        if order:
            start = int(db.kv_get("scout:topic_cursor", 0) or 0) % len(order)
            order = order[start:] + order[:start]
            db.kv_set("scout:topic_cursor", start + 1)
        per_topic = max(1, math.ceil(cap * TOPIC_SHARE))
        taken: Counter = Counter()
        new_ids: list[int] = []
        while len(new_ids) < cap and order:
            progressed = False
            for q in list(order):
                if len(new_ids) >= cap:
                    break
                if taken[q] >= per_topic or not pools[q]:
                    order.remove(q)
                    continue
                c = pools[q].pop(0)
                sid = db.add_source(
                    c.url, c.source_type, platform=c.platform, external_id=c.external_id, channel=c.channel,
                    channel_id=c.channel_id, title=c.title, description=c.description,
                    duration_sec=c.duration_sec, published_at=c.published_at, topic=q,
                    metadata={**c.metadata, "handle": c.handle, "licence_verified": False,
                              "duration_unknown": c.duration_sec is None},
                )
                progressed = True
                if sid is None:
                    dropped["known"] += 1
                    continue
                taken[q] += 1
                new_ids.append(sid)
            if not progressed:
                break

        by_type = Counter(db.require_source(i).source_type for i in new_ids)
        db.log_event(MODULE, f"scout added {len(new_ids)} candidate(s)",
                     level="warning" if errors and not new_ids else "info",
                     data={"new": len(new_ids), "cap": cap, "found": found, "by_type": dict(by_type),
                           "by_topic": dict(taken), "dropped": dict(dropped), "errors": errors[:10],
                           "error_count": len(errors)})
        return new_ids
    finally:
        _ctx = None
