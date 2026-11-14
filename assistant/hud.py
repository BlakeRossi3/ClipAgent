"""Sterling's HUD: what he can put on the Jarvis page, and how the page may restyle itself.

Adapted from adewaskar/jarvis (MIT License, Copyright (c) 2026 Aditya Dewaskar): ``bridge/panels.mjs``
(the ``blade`` kinds: image, video, embed, markup), ``bridge/ui.mjs`` (the ``ui_*`` tools: the
interface is his to retint and reshape, persisted, always reversible) and ``bridge/net.mjs`` +
``server.mjs`` ``/img`` (an SSRF gate in front of a server-side image fetch, so the page only ever
talks to this server and a model-chosen URL can never reach the LAN).

Panel kinds (published on ``assistant.events`` as ``panel`` events; the page renders each kind with
its own code, never as model markup, except ``markup`` which ``assistant.panels`` sanitises)::

    markup   {html}                                   the ``display`` tool (assistant/panels.py)
    image    {src, caption}                           http(s) only; shown through /voice/api/img
    video    {src | clip_id, caption}                 an mp4/webm through /voice/api/media, or a rendered Short
    youtube  {video_id, caption}                      youtube-nocookie.com/embed iframe
    map      {map: {lat, lon, zoom, label, markers}}  OpenStreetMap tiles through /voice/api/img
    data     {rows: [[label, value], ...], note}      key figures
    list     {items: [{label, sub, tag}], note}       a short list

Every panel has ``id, kind, title, accent, hold (turn|sticky), size (card|blade), turn``.

Image proxy (:func:`fetch_remote`): http(s) only, default ports, no credentials in the URL, never a
local/private/link-local/CGNAT/metadata address (every DNS answer is checked; IP literals too),
redirects followed by hand (at most 3) and each hop re-checked, content type must be the kind asked
for (no SVG), size capped. The first URL must be on :data:`PROXY_HOSTS` or one the model put in a
panel (:func:`allow_url`). Geocoding uses Nominatim with a proper User-Agent, one request a second,
answers cached in ``kv``.

Tests replace the network with :func:`set_http` and DNS with :func:`set_resolver`.
"""

from __future__ import annotations

import ipaddress
import itertools
import json
import re
import socket
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import quote, urlencode, urlsplit

import db

USER_AGENT = "ClipAgent/1.0 (personal assistant)"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
TILE_HOST = "tile.openstreetmap.org"
PROXY_HOSTS = frozenset({TILE_HOST, "i.ytimg.com", "upload.wikimedia.org"})
KINDS = ("markup", "image", "video", "youtube", "map", "data", "list")
SIZES = ("card", "blade")
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_MEDIA_BYTES = 40 * 1024 * 1024
TIMEOUT_SEC = 10.0
MAX_REDIRECTS = 3
GEO_MIN_INTERVAL_SEC = 1.0
KV_GEO = "hud:geo:{q}"
KV_UI = "hud:ui"
_BLOCKED_HOST = re.compile(r"(^|\.)(localhost|local|internal|intranet|lan|home|corp|home\.arpa)$", re.I)
_TILE_PATH = re.compile(r"^/(\d{1,2})/(\d{1,7})/(\d{1,7})\.png$")
_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_seq = itertools.count(1)


class ProxyError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# network seams (tests replace both)
# ---------------------------------------------------------------------------

@dataclass
class Fetched:
    status: int
    headers: dict[str, str] = field(default_factory=dict)   # lower-case names
    body: bytes = b""


Http = Callable[[str, dict[str, str], float, int], Fetched]


def _httpx_get(url: str, headers: dict[str, str], timeout: float, max_bytes: int) -> Fetched:
    import httpx

    with httpx.Client(follow_redirects=False, timeout=timeout) as c:
        with c.stream("GET", url, headers=headers) as r:
            hdrs = {k.lower(): v for k, v in r.headers.items()}
            if 300 <= r.status_code < 400:
                return Fetched(r.status_code, hdrs, b"")
            declared = hdrs.get("content-length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                raise ProxyError(413, "too large")
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf += chunk
                if len(buf) > max_bytes:
                    raise ProxyError(413, "too large")
            return Fetched(r.status_code, hdrs, bytes(buf))


def _getaddrinfo(host: str) -> list[str]:
    return sorted({ai[4][0] for ai in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)})


_http: Http = _httpx_get
_resolver: Callable[[str], list[str]] = _getaddrinfo


def set_http(fn: Http | None) -> None:
    """Tests / demos: ``fn(url, headers, timeout, max_bytes) -> Fetched`` (None = real httpx)."""
    global _http
    _http = fn or _httpx_get


def set_resolver(fn: Callable[[str], list[str]] | None) -> None:
    """Tests: ``fn(host) -> [ip, ...]`` (None = the system resolver)."""
    global _resolver
    _resolver = fn or _getaddrinfo


# ---------------------------------------------------------------------------
# the SSRF gate
# ---------------------------------------------------------------------------

def blocked_address(ip: str) -> bool:
    """True for anything that is not a plain public address (loopback, private, link-local and cloud
    metadata, CGNAT, multicast, reserved, unspecified, IPv4-mapped forms of those)."""
    try:
        a = ipaddress.ip_address(str(ip).split("%")[0])
    except ValueError:
        return True
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped is not None:
        a = a.ipv4_mapped
    return not a.is_global or a.is_multicast


_allowed: "OrderedDict[str, float]" = OrderedDict()
_allowed_lock = threading.Lock()
ALLOWED_KEEP = 500
ALLOWED_TTL_SEC = 24 * 3600


def allow_url(url: str) -> bool:
    """The model put this http(s) URL in a panel: the proxy may fetch it (for a day). Returns ok."""
    u = str(url or "").strip()
    try:
        vet_url(u, need_allowed=False, resolve=False)
    except ProxyError:
        return False
    with _allowed_lock:
        _allowed[u] = time.time()
        _allowed.move_to_end(u)
        while len(_allowed) > ALLOWED_KEEP:
            _allowed.popitem(last=False)
    return True


def is_allowed(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if host == TILE_HOST:
        return bool(_TILE_PATH.match(parts.path)) and not parts.query
    if host in PROXY_HOSTS:
        return True
    with _allowed_lock:
        t = _allowed.get(url)
    return t is not None and time.time() - t < ALLOWED_TTL_SEC


def vet_url(url: str, *, need_allowed: bool = True, resolve: bool = True) -> str:
    """Return ``url`` if the proxy may fetch it, else raise :class:`ProxyError`."""
    raw = str(url or "").strip()
    if len(raw) > 2000 or any(c in raw for c in "\r\n\t "):
        raise ProxyError(400, "absolute http(s) url required")
    try:
        p = urlsplit(raw)
        port = p.port
    except ValueError:
        raise ProxyError(400, "absolute http(s) url required") from None
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ProxyError(400, "absolute http(s) url required")
    if p.username or p.password or "@" in p.netloc:
        raise ProxyError(400, "credentials in the url")
    if port not in (None, 80, 443):
        raise ProxyError(403, "blocked port")
    host = p.hostname.lower().rstrip(".")
    if _BLOCKED_HOST.search(host) or "." not in host and ":" not in host:
        raise ProxyError(403, "blocked host")
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        if blocked_address(str(literal)):
            raise ProxyError(403, "blocked host")
    elif re.fullmatch(r"(?:0x[0-9a-f]*|\d+)", host.rsplit(".", 1)[-1]):   # 2130706433, 0x7f.1, 127.1: no TLD is numeric
        raise ProxyError(403, "blocked host")
    if need_allowed and not is_allowed(raw):
        raise ProxyError(403, "not an allowed image")
    if resolve and literal is None:
        try:
            addrs = _resolver(host)
        except OSError:
            raise ProxyError(502, "upstream unreachable") from None
        if not addrs or any(blocked_address(a) for a in addrs):
            raise ProxyError(403, "blocked host")
    return raw


_cache: "OrderedDict[str, tuple[float, str, bytes]]" = OrderedDict()
_cache_lock = threading.Lock()
CACHE_KEEP = 400
CACHE_TTL_SEC = 24 * 3600


def fetch_remote(url: str, *, kind: str = "image") -> tuple[str, bytes]:
    """The proxy: -> (content type, bytes). Raises :class:`ProxyError` (status for the page)."""
    kinds = ("image/",) if kind == "image" else ("video/", "audio/")
    cap = MAX_IMAGE_BYTES if kind == "image" else MAX_MEDIA_BYTES
    target = vet_url(url)
    key = f"{kind}:{target}"
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL_SEC:
            _cache.move_to_end(key)
            return hit[1], hit[2]
    headers = {"User-Agent": USER_AGENT, "Accept": "image/*,*/*;q=0.8" if kind == "image" else "*/*",
               "Accept-Encoding": "identity"}
    cur = target
    for hop in range(MAX_REDIRECTS + 1):
        try:
            res = _http(cur, headers, TIMEOUT_SEC, cap)
        except ProxyError:
            raise
        except Exception:
            raise ProxyError(502, "upstream unreachable") from None
        if 300 <= res.status < 400:
            loc = res.headers.get("location", "")
            if not loc or hop >= MAX_REDIRECTS:
                raise ProxyError(502, "bad redirect")
            from urllib.parse import urljoin

            cur = vet_url(urljoin(cur, loc), need_allowed=False)   # every hop re-checked (scheme, host, DNS)
            continue
        break
    if res.status != 200:
        raise ProxyError(404 if res.status == 404 else 502, f"upstream said {res.status}")
    ctype = res.headers.get("content-type", "").split(";")[0].strip().lower()
    if not ctype.startswith(kinds) or "svg" in ctype:
        raise ProxyError(415, f"not {kind}")
    if len(res.body) > cap:
        raise ProxyError(413, "too large")
    if len(res.body) <= 512 * 1024:
        with _cache_lock:
            _cache[key] = (time.time(), ctype, res.body)
            while len(_cache) > CACHE_KEEP:
                _cache.popitem(last=False)
    return ctype, res.body


def proxied(url: str, kind: str = "image") -> str:
    """The page-relative proxy URL for ``url``."""
    return ("/voice/api/img?u=" if kind == "image" else "/voice/api/media?u=") + quote(url, safe="")


def reset() -> None:
    """Tests: the real network seams back, caches and the allowed-URL list emptied."""
    set_http(None)
    set_resolver(None)
    with _allowed_lock:
        _allowed.clear()
    with _cache_lock:
        _cache.clear()


# ---------------------------------------------------------------------------
# geocoding (Nominatim, OpenStreetMap)
# ---------------------------------------------------------------------------

_geo_lock = threading.Lock()
_geo_last = 0.0
_ZOOM_BY_TYPE = {"country": 5, "state": 7, "region": 7, "province": 7, "county": 9, "city": 12, "town": 13,
                 "village": 14, "municipality": 12, "suburb": 14, "neighbourhood": 15, "postcode": 13}


def _norm_query(q: str) -> str:
    return " ".join(str(q or "").lower().split())[:200]


def geocode(query: str) -> dict[str, Any] | None:
    """Place name -> {lat, lon, label, zoom} via Nominatim (cached in kv; at most one request a second)."""
    global _geo_last
    q = _norm_query(query)
    if not q:
        return None
    cached = db.kv_get(KV_GEO.format(q=q))
    if isinstance(cached, dict):
        return cached or None
    url = NOMINATIM_URL + "?" + urlencode({"q": q, "format": "jsonv2", "limit": 1, "addressdetails": 0})
    with _geo_lock:
        wait = GEO_MIN_INTERVAL_SEC - (time.monotonic() - _geo_last)
        if wait > 0:
            time.sleep(wait)
        try:
            res = _http(url, {"User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Language": "en"},
                        TIMEOUT_SEC, 512 * 1024)
        finally:
            _geo_last = time.monotonic()
    if res.status != 200:
        raise ProxyError(502, f"geocoder said {res.status}")
    try:
        rows = json.loads(res.body.decode("utf-8", "replace"))
    except ValueError:
        raise ProxyError(502, "geocoder answer unreadable") from None
    out: dict[str, Any] = {}
    if isinstance(rows, list) and rows:
        r = rows[0]
        try:
            out = {"lat": round(float(r["lat"]), 6), "lon": round(float(r["lon"]), 6),
                   "label": str(r.get("display_name") or query)[:160],
                   "zoom": _ZOOM_BY_TYPE.get(str(r.get("addresstype") or r.get("type") or ""), 15)}
        except (KeyError, TypeError, ValueError):
            out = {}
    db.kv_set(KV_GEO.format(q=q), out)
    return out or None


# ---------------------------------------------------------------------------
# panels
# ---------------------------------------------------------------------------

def _current_turn() -> str | None:
    try:
        from assistant import brain

        b = brain._brain
        return b.current.id if b is not None and b.current is not None else None
    except Exception:
        return None


def _clean(text: Any, n: int) -> str:
    return " ".join(str(text or "").split())[:n]


def make_panel(kind: str, title: str, body: dict[str, Any], *, accent: str = "default", hold: str = "turn",
               size: str = "card", turn: str | None = None) -> dict[str, Any]:
    from assistant import panels

    if kind not in KINDS:
        raise ValueError(f"unknown panel kind {kind!r}")
    return {"id": f"p{int(time.time() * 1000):x}-{next(_seq)}", "kind": kind,
            "title": _clean(title, panels.MAX_TITLE) or kind.upper(),
            "accent": accent if accent in panels.ACCENTS else "default",
            "hold": hold if hold in panels.HOLDS else "turn", "size": size if size in SIZES else "card",
            "turn": turn if turn is not None else _current_turn(), **body}


def publish_panel(panel: dict[str, Any]) -> dict[str, Any]:
    from assistant import events

    events.publish("panel", panel=panel)
    return panel


def _num(v: Any, lo: float, hi: float, name: str) -> float:
    try:
        x = float(str(v).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not lo <= x <= hi:
        raise ValueError(f"{name} must be between {lo:g} and {hi:g}")
    return x


def map_panel(args: dict[str, Any]) -> dict[str, Any]:
    """show_map arguments -> a map panel (geocodes ``query`` and marker queries). Raises ValueError."""
    query = _clean(args.get("query"), 200)
    lat, lon = args.get("lat"), args.get("lon")
    zoom = args.get("zoom")
    label = query
    if lat not in (None, "") and lon not in (None, ""):
        lat, lon = _num(lat, -85.05, 85.05, "lat"), _num(lon, -180, 180, "lon")
        z_default = 14
    elif query:
        try:
            hit = geocode(query)
        except ProxyError as exc:
            raise ValueError(f"The map service is unreachable ({exc}).") from None
        if not hit:
            raise ValueError(f"No place called {query!r} was found. Try a fuller name or give lat and lon.")
        lat, lon, z_default, label = hit["lat"], hit["lon"], hit["zoom"], hit["label"]
    else:
        raise ValueError("show_map needs a place (query) or lat and lon.")
    z = int(round(_num(zoom, 1, 18, "zoom"))) if zoom not in (None, "") else z_default
    markers: list[dict[str, Any]] = []
    for m in (args.get("markers") or [])[:8]:
        if not isinstance(m, dict):
            continue
        if m.get("lat") not in (None, "") and m.get("lon") not in (None, ""):
            markers.append({"lat": _num(m["lat"], -85.05, 85.05, "marker lat"),
                            "lon": _num(m["lon"], -180, 180, "marker lon"), "label": _clean(m.get("label"), 60)})
        elif m.get("query"):
            hit = geocode(str(m["query"]))
            if hit:
                markers.append({"lat": hit["lat"], "lon": hit["lon"], "label": _clean(m.get("label") or m["query"], 60)})
    if not markers:
        markers = [{"lat": lat, "lon": lon, "label": _clean(label, 60)}]
    title = _clean(args.get("title"), 40) or _clean((label or "MAP").split(",")[0], 40).upper()
    return make_panel("map", title, {"map": {"lat": float(lat), "lon": float(lon), "zoom": z,
                                             "label": _clean(label, 160), "markers": markers}},
                      accent=str(args.get("accent") or "default"), hold=str(args.get("hold") or "sticky"),
                      size=str(args.get("size") or "card"))


def youtube_id(url: str) -> str | None:
    u = str(url or "").strip()
    if _YT_ID.match(u):
        return u
    try:
        p = urlsplit(u)
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    cand = None
    if host in ("youtu.be",):
        cand = p.path.strip("/").split("/")[0]
    elif host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        m = re.search(r"(?:^|&)v=([^&]+)", p.query)
        if m:
            cand = m.group(1)
        else:
            m = re.match(r"^/(?:embed|shorts|live|v)/([^/?#]+)", p.path)
            cand = m.group(1) if m else None
    return cand if cand and _YT_ID.match(cand) else None


def media_panel(args: dict[str, Any]) -> dict[str, Any]:
    """show_media arguments -> an image, video or YouTube panel. Raises ValueError."""
    url = str(args.get("url") or "").strip()
    kind = str(args.get("kind") or "").lower()
    caption = _clean(args.get("caption"), 160)
    title = _clean(args.get("title"), 40)
    common = dict(accent=str(args.get("accent") or "default"), hold=str(args.get("hold") or "turn"),
                  size=str(args.get("size") or "card"))
    clip_id = args.get("clip_id")
    if clip_id not in (None, ""):
        try:
            c = db.get_clip(int(clip_id))
        except (TypeError, ValueError):
            c = None
        if c is None or not c.render_path:
            raise ValueError(f"Clip {clip_id} has no rendered video.")
        return make_panel("video", title or "SHORT", {"src": f"/voice/api/clip/{c.id}", "caption": caption or
                                                      _clean(c.title, 160)}, **common)
    vid = youtube_id(url) if kind in ("", "youtube") else None
    if vid:
        return make_panel("youtube", title or "VIDEO", {"video_id": vid, "caption": caption}, **common)
    if kind == "youtube":
        raise ValueError("That is not a YouTube video link.")
    if not re.match(r"^https?://", url, re.I):
        raise ValueError("show_media needs an http(s) url that appeared in a tool result, or a clip_id.")
    try:
        vet_url(url, need_allowed=False, resolve=False)
    except ProxyError as exc:
        raise ValueError(f"That address cannot be shown ({exc}).") from None
    if not kind:
        kind = "video" if re.search(r"\.(mp4|webm|m4v|mov)(\?|$)", url, re.I) else "image"
    if kind not in ("image", "video"):
        raise ValueError("kind is image, video or youtube.")
    allow_url(url)
    return make_panel(kind, title or kind.upper(), {"src": proxied(url, kind), "url": url, "caption": caption},
                      **common)


def data_panel(args: dict[str, Any]) -> dict[str, Any]:
    """show_data arguments -> a key/value (``pairs``) or list (``items``) panel. Raises ValueError."""
    title = _clean(args.get("title"), 40) or "DATA"
    note = _clean(args.get("note"), 200)
    common = dict(accent=str(args.get("accent") or "default"), hold=str(args.get("hold") or "turn"),
                  size=str(args.get("size") or "card"))
    pairs = args.get("pairs") or []
    items = args.get("items") or []
    if pairs:
        rows = []
        for p in pairs[:16]:
            if isinstance(p, dict):
                rows.append([_clean(p.get("label"), 60), _clean(p.get("value"), 80)])
            elif isinstance(p, (list, tuple)) and len(p) >= 2:
                rows.append([_clean(p[0], 60), _clean(p[1], 80)])
        rows = [r for r in rows if r[0] or r[1]]
        if rows:
            return make_panel("data", title, {"rows": rows, "note": note}, **common)
    if items:
        out = []
        for it in items[:12]:
            if isinstance(it, dict):
                out.append({"label": _clean(it.get("label"), 80), "sub": _clean(it.get("sub"), 120),
                            "tag": _clean(it.get("tag"), 20)})
            else:
                out.append({"label": _clean(it, 80), "sub": "", "tag": ""})
        out = [x for x in out if x["label"]]
        if out:
            return make_panel("list", title, {"items": out, "note": note}, **common)
    raise ValueError("show_data needs pairs ([{label, value}]) or items ([{label, sub, tag}]).")


# ---------------------------------------------------------------------------
# ui_*: the interface restyled (persisted in kv; effects are one-off)
# ---------------------------------------------------------------------------

COLOURS = {"red": "#ff3b30", "crimson": "#dc143c", "orange": "#ff8c1a", "amber": "#e8b054", "gold": "#ffc83d",
           "yellow": "#ffe14d", "green": "#3ddc84", "emerald": "#10b981", "lime": "#a3e635", "teal": "#14b8a6",
           "cyan": "#00d6f0", "blue": "#3b82f6", "navy": "#1e3a8a", "indigo": "#6366f1", "violet": "#8b5cf6",
           "purple": "#a855f7", "magenta": "#e040fb", "pink": "#ff6fae", "white": "#f4f7fb", "silver": "#c0c8d2",
           "grey": "#8b97a5", "gray": "#8b97a5"}
RING_STYLES = ("arc", "minimal", "dense", "radar")
EFFECTS = ("pulse", "scan", "glitch", "flash")
AUTO_WORDS = {"auto", "default", "none", "null", "reset", "clear", "stock", ""}
DEFAULT_UI: dict[str, Any] = {"accent": None, "ring": {"style": "arc", "color": None, "scale": 1.0, "spin": 1.0},
                              "rails": True}


def parse_colour(value: Any) -> str | None:
    """A colour the page can use -> ``#rrggbb``; None for "auto". Raises ValueError."""
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in AUTO_WORDS:
        return None
    if s in COLOURS:
        return COLOURS[s]
    m = re.fullmatch(r"#?([0-9a-f]{3}|[0-9a-f]{6})", s)
    if m:
        h = m.group(1)
        return "#" + ("".join(c * 2 for c in h) if len(h) == 3 else h)
    m = re.fullmatch(r"rgb\(\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*\)", s)
    if m and all(int(x) <= 255 for x in m.groups()):
        return "#" + "".join(f"{int(x):02x}" for x in m.groups())
    raise ValueError(f"{value!r} is not a colour I can use (a name like red or teal, or #rrggbb).")


def ui_state() -> dict[str, Any]:
    v = db.kv_get(KV_UI)
    st = json.loads(json.dumps(DEFAULT_UI))
    if isinstance(v, dict):
        st["accent"] = v.get("accent") if isinstance(v.get("accent"), str) else None
        ring = v.get("ring") if isinstance(v.get("ring"), dict) else {}
        st["ring"].update({k: ring[k] for k in ("style", "color", "scale", "spin") if k in ring})
        st["rails"] = bool(v.get("rails", True))
    return st


def _save_ui(st: dict[str, Any], *, effect: str | None = None, via: str = "assistant") -> dict[str, Any]:
    from assistant import events

    db.kv_set(KV_UI, st)
    events.publish("ui", ui=st, effect=effect)
    return st


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() not in ("false", "0", "no", "off", "hide", "hidden", "none", "")


def ui_theme(accent: Any) -> dict[str, Any]:
    st = ui_state()
    st["accent"] = parse_colour(accent)
    return _save_ui(st)


def ui_ring(*, style: Any = None, color: Any = "__keep__", scale: Any = None, spin: Any = None) -> dict[str, Any]:
    st = ui_state()
    ring = st["ring"]
    if style not in (None, ""):
        s = str(style).strip().lower()
        if s in AUTO_WORDS:
            s = "arc"
        if s not in RING_STYLES:
            raise ValueError(f"ring style is one of {', '.join(RING_STYLES)}")
        ring["style"] = s
    if color != "__keep__":
        ring["color"] = parse_colour(color)
    if scale not in (None, ""):
        ring["scale"] = round(_num(scale, 0.5, 1.5, "scale"), 2)
    if spin not in (None, ""):
        ring["spin"] = round(_num(spin, 0.0, 4.0, "spin"), 2)
    return _save_ui(st)


def ui_rails(show: Any) -> dict[str, Any]:
    st = ui_state()
    st["rails"] = _bool(show)
    return _save_ui(st)


def ui_effect(effect: Any) -> dict[str, Any]:
    e = str(effect or "").strip().lower()
    if e not in EFFECTS:
        raise ValueError(f"effect is one of {', '.join(EFFECTS)}")
    from assistant import events

    events.publish("ui", ui=ui_state(), effect=e)
    return {"effect": e}


def ui_reset() -> dict[str, Any]:
    db.kv_delete(KV_UI)
    return _save_ui(ui_state())


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

TOOL_NAMES = ("show_map", "show_media", "show_data", "ui_theme", "ui_ring", "ui_rails", "ui_effect", "ui_reset")
_ACCENT = {"type": "string", "enum": ["default", "amber", "violet", "green", "red"]}
_HOLD = {"type": "string", "enum": ["turn", "sticky"],
         "description": "turn = cleared when he next speaks; sticky = stays until replaced."}
_SIZE = {"type": "string", "enum": list(SIZES), "description": "card = beside the ring; blade = large, over the ring."}
_NUMISH = {"type": ["number", "string"]}


def _tool(fn: Callable[[dict[str, Any]], Any]) -> Callable[[Any, dict[str, Any]], Any]:
    def run(ctx: Any, a: dict[str, Any]) -> Any:
        from assistant.tools import ToolError

        try:
            return fn(a)
        except (ValueError, ProxyError) as exc:
            raise ToolError(str(exc)) from None
    run.__name__ = getattr(fn, "__name__", "hud_tool")
    return run


def _show(builder: Callable[[dict[str, Any]], dict[str, Any]]) -> Callable[[dict[str, Any]], Any]:
    def go(a: dict[str, Any]) -> Any:
        p = publish_panel(builder(a))
        out = {"on_screen": True, "kind": p["kind"], "title": p["title"]}
        if p["kind"] == "map":
            out.update({k: p["map"][k] for k in ("lat", "lon", "zoom", "label")})
        return out
    go.__name__ = builder.__name__
    return go


def register_tools(register: Callable[..., None]) -> None:
    from assistant.tools import Tool, _obj

    S = {"type": "string"}
    own = "assistant.hud"

    def t(name: str, desc: str, props: dict[str, Any], req: list[str], fn: Callable[[dict[str, Any]], Any],
          side: bool = False) -> None:
        register(Tool(name, desc, _obj(props, req), _tool(fn), owner=own, side_effects=side), replace=True)

    t("show_map", "Draw a map on his Jarvis screen: 'where is X', 'show me X on a map', a route's stops. Give a place "
      "name (query) or lat and lon; optional zoom 1-18 and up to eight markers ({query} or {lat, lon}, each with a "
      "label). Then say in one sentence where it is; never read coordinates aloud.",
      {"query": S, "lat": _NUMISH, "lon": _NUMISH, "zoom": _NUMISH, "title": S,
       "markers": {"type": "array", "items": {"type": "object"}}, "accent": _ACCENT, "hold": _HOLD, "size": _SIZE},
      [], _show(map_panel))
    t("show_media", "Put a picture or video on his Jarvis screen: an image URL or video file URL that appeared "
      "verbatim in a tool result (never invent one), a YouTube link (played privately, youtube-nocookie), or "
      "clip_id = one of his rendered Shorts. Optional caption. Say what it shows, briefly.",
      {"url": S, "kind": {"type": "string", "enum": ["image", "video", "youtube"]}, "clip_id": {"type": ["integer", "string"]},
       "title": S, "caption": S, "accent": _ACCENT, "hold": _HOLD, "size": _SIZE}, [], _show(media_panel))
    t("show_data", "Put key figures (pairs: [{label, value}]) or a short list (items: [{label, sub, tag}]) on his "
      "Jarvis screen, with an optional one-line note. For figures he asked to see; say what they mean, do not read "
      "them all out.",
      {"title": S, "pairs": {"type": "array"}, "items": {"type": "array"}, "note": S, "accent": _ACCENT,
       "hold": _HOLD, "size": _SIZE}, ["title"], _show(data_panel))
    t("ui_theme", "Retint the whole Jarvis screen: one accent colour (a name like red, teal, gold, or #rrggbb; "
      "'auto' hands it back). Persisted until changed. Use when he asks, or red just before reporting a failure, "
      "then put it back. Never mention it.",
      {"accent": S}, ["accent"], lambda a: ui_theme(a.get("accent")), True)
    t("ui_ring", "Reshape the voice ring: style (arc, minimal, dense, radar), color (name, #rrggbb or auto), scale "
      "0.5-1.5, spin 0-4 (0 = still). Only the parts given change. Persisted.",
      {"style": S, "color": S, "scale": _NUMISH, "spin": _NUMISH}, [],
      lambda a: ui_ring(style=a.get("style"), color=a["color"] if "color" in a else "__keep__", scale=a.get("scale"),
                        spin=a.get("spin")), True)
    t("ui_rails", "Show or hide the side rails (activity, panels) so the ring or one blade fills the screen. Persisted.",
      {"show": {"type": ["boolean", "string"]}}, ["show"], lambda a: ui_rails(a.get("show")), True)
    t("ui_effect", "Fire one flourish on the Jarvis screen: pulse, scan, glitch or flash. Not persisted. Sparingly.",
      {"effect": {"type": "string", "enum": list(EFFECTS)}}, ["effect"], lambda a: ui_effect(a.get("effect")))
    t("ui_reset", "Put the Jarvis screen back to its stock look (colour, ring, rails).", {}, [], lambda a: ui_reset(),
      True)
