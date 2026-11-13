"""Owner dashboard: FastAPI ``router`` (prefix ``/dashboard``) + Jinja2 templates.

Answers "is this working as a business?" at a glance, with drill-downs:

* ``/``             overview: KPIs vs previous period, today vs caps, pipeline health,
                    pending approvals, current experiment, daily charts
* ``/clips``        sortable / filterable leaderboard; ``/clips/{id}`` per-clip audit page
* ``/insights``     "what works": breakdowns with sample sizes, Haiku score vs views
* ``/strategy``     experiments (control vs variant) + strategy version history
* ``/sources``      pipeline funnel, rejection reasons, sources table; ``/sources/{id}``;
                    ``/licences.csv`` licence audit export
* ``/costs``        LLM spend by day / model / purpose, tokens, cache hits, quotas
* ``/decisions``    approvals (POST ``/decisions/{id}/approve|reject`` ->
                    ``notify.handle_decision(via="dashboard")``) + notifications log
* ``/events``       event log with level / module filters
* ``/api/...``      JSON: ``summary`` (contract), ``series``, ``clips``, ``clips/{id}``,
                    ``insights``, ``costs``

Auth: when ``DASHBOARD_TOKEN`` is set every route (except ``/static``) requires
``?token=`` or ``Authorization: Bearer``. A browser visiting with ``?token=`` gets an
HttpOnly cookie (an HMAC of the token, never the token itself) and is redirected
to the same URL without the token. ``/login`` offers a form for phones.
State-changing routes are POST and need a CSRF field (HMAC of a server secret)
plus a same-origin ``Origin`` header when the browser sends one; Bearer-authenticated
requests are exempt (no ambient credential).

Charts are rendered client-side from JSON embedded in the page by the vendored-free
``static/charts.js`` (inline SVG, no external requests); every chart has a
server-rendered table twin, so all values are readable without JavaScript.

Read-only reporting SQL that ``db.py`` lacks lives in the clearly marked
"Reporting queries" section below and only reads through ``db.connect()``.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import math
import re
import secrets
import sqlite3
import statistics
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

import db
from config import get_settings

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

#: kv key written by ``seed_demo.py``; when present the dashboard shows a DEMO DATA banner.
DEMO_KV_KEY = "demo:dataset"
COOKIE_NAME = "clipagent_dash"
COOKIE_MAX_AGE = 30 * 24 * 3600
PAGE_SIZE = 50
MIN_GROUP_N = 5          # groups smaller than this are greyed out as "too few to judge"
MATURE_DAYS = 7          # clips are compared on views in their first 7 days
YOUTUBE_DAILY_QUOTA = 10_000

PERIODS: dict[str, int | None] = {"7": 7, "28": 28, "90": 90, "all": None}
PERIOD_LABELS = {"7": "7 days", "28": "28 days", "90": "90 days", "all": "All time"}

#: Pipeline stages shown in "pipeline health": (event module, label, hours before "idle").
STAGES: list[tuple[str, str, int]] = [
    ("scout", "Scout", 30), ("fetch", "Fetch", 30), ("transcribe", "Transcribe", 30),
    ("pick", "Pick", 30), ("edit", "Render", 30), ("upload", "Upload", 30),
    ("analytics", "Analytics", 30), ("strategist", "Weekly review", 8 * 24), ("notify", "Notify", 30),
]

NAV = [
    ("overview", "Overview", "/dashboard/"),
    ("clips", "Clips", "/dashboard/clips"),
    ("insights", "What works", "/dashboard/insights"),
    ("strategy", "Experiments", "/dashboard/strategy"),
    ("sources", "Pipeline", "/dashboard/sources"),
    ("costs", "Costs", "/dashboard/costs"),
    ("decisions", "Decisions", "/dashboard/decisions"),
    ("events", "Log", "/dashboard/events"),
]

_FLASH = {
    "approved": ("good", "Approved. The change will be applied by the agent."),
    "rejected": ("info", "Rejected. Nothing will change."),
    "already": ("warning", "That decision was already resolved (first answer wins)."),
    "failed": ("critical", "Could not record the decision - see the log for details."),
    "unapplied": ("critical", "Approved, but the change could not be applied automatically, so nothing has "
                              "changed yet. See the log for why."),
    "csrf": ("critical", "The form expired or came from another site. Reload the page and try again."),
    "loggedout": ("info", "Signed out."),
}


# ===========================================================================
# Formatting (also registered as Jinja filters)
# ===========================================================================

def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def fmt_compact(v: Any) -> str:
    """1,284 -> "1.3K"; 12_900 -> "12.9K"; 312_000 -> "312K"; 4.2e6 -> "4.2M"."""
    f = _num(v)
    if f is None:
        return "—"
    sign = "-" if f < 0 else ""
    a = abs(f)
    if a < 1000:
        return f"{sign}{a:.0f}" if a >= 10 or a == int(a) else f"{sign}{a:.1f}"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            x = a / div
            s = f"{x:.1f}" if x < 100 else f"{x:.0f}"
            return sign + s.removesuffix(".0") + suf
    return f"{sign}{a:.0f}"  # pragma: no cover


def fmt_int(v: Any) -> str:
    f = _num(v)
    return "—" if f is None else f"{f:,.0f}"


def fmt_usd(v: Any) -> str:
    f = _num(v)
    if f is None:
        return "—"
    sign = "-" if f < 0 else ""
    a = abs(f)
    if a == 0:
        return "$0"
    if a >= 10_000:
        return f"{sign}${fmt_compact(a)}"
    if a >= 100:
        return f"{sign}${a:,.0f}"
    if a >= 0.995:
        return f"{sign}${a:,.2f}"
    if a >= 0.1:
        return f"{sign}${a:.2f}"
    if a >= 0.01:
        return f"{sign}${a:.3f}"
    if a >= 0.0001:
        return f"{sign}${a:.4f}".rstrip("0")
    return f"{sign}<$0.0001"


def fmt_pct(v: Any, digits: int | None = None) -> str:
    f = _num(v)
    if f is None:
        return "—"
    if digits is None:
        digits = 1 if abs(f) < 10 else 0
    return f"{f:.{digits}f}%"


def fmt_duration(sec: Any) -> str:
    f = _num(sec)
    if f is None:
        return "—"
    s = int(round(f))
    if s >= 3600:
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


def fmt_hours(minutes: Any) -> str:
    f = _num(minutes)
    if f is None:
        return "—"
    h = f / 60
    return f"{fmt_compact(h)} h" if h >= 10 else f"{h:.1f} h"


def _local(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return db.parse_iso(iso).astimezone(db.local_tz())
    except (ValueError, TypeError):
        return None


def _hour12(dt: datetime) -> str:
    h = dt.hour % 12 or 12
    return f"{h}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def fmt_dt(iso: str | None) -> str:
    """Stored UTC ISO -> "Nov 13, 10:05 AM" in the configured timezone (year added if not this year)."""
    dt = _local(iso)
    if dt is None:
        return "—"
    year = f" {dt.year}" if dt.year != db.local_now().year else ""
    return f"{dt.strftime('%b')} {dt.day}{year}, {_hour12(dt)}"


def fmt_date(value: Any) -> str:
    """ISO timestamp or YYYY-MM-DD -> "Nov 13" (local)."""
    if value is None or value == "":
        return "—"
    if isinstance(value, date) and not isinstance(value, datetime):
        d = value
    elif isinstance(value, str) and len(value) == 10:
        try:
            d = date.fromisoformat(value)
        except ValueError:
            return value
    else:
        dt = _local(value) if isinstance(value, str) else None
        if dt is None:
            return "—"
        d = dt.date()
    year = f", {d.year}" if d.year != db.local_today().year else ""
    return f"{d.strftime('%b')} {d.day}{year}"


def fmt_ago(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        delta = db.utcnow() - db.parse_iso(iso)
    except (ValueError, TypeError):
        return "—"
    s = delta.total_seconds()
    future = s < 0
    s = abs(s)
    if s < 60:
        txt = "just now" if not future else "in <1 min"
        return txt
    if s < 3600:
        n, unit = int(s // 60), "min"
    elif s < 86400 * 2:
        n, unit = int(s // 3600), "h"
    else:
        n, unit = int(s // 86400), "d"
    return f"in {n} {unit}" if future else f"{n} {unit} ago"


def ext_url(value: Any) -> str:
    """An external link target that is safe in ``href``: only http(s) URLs, else "#".

    Source URLs come from scraped metadata / the CLI; ``javascript:`` or ``data:`` URLs must never
    become clickable (autoescaping does not neutralise a URL scheme)."""
    v = str(value or "").strip()
    return v if urlparse(v).scheme.lower() in ("http", "https") and "\\" not in v else "#"


def fmt_json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, default=str)


def _delta(cur: float | None, prev: float | None, *, kind: str = "rel") -> dict[str, Any] | None:
    """Delta vs previous period. kind="rel" -> percent change; "pts" -> difference in points."""
    if cur is None or prev is None:
        return None
    if kind == "pts":
        d = cur - prev
        if abs(d) < 0.05:
            return {"value": d, "text": "±0 pts", "dir": 0}
        return {"value": d, "text": f"{d:+.1f} pts", "dir": (d > 0) - (d < 0)}
    if prev == 0:
        return None if cur == 0 else {"value": None, "text": "new", "dir": 1 if cur > 0 else -1}
    d = (cur - prev) / abs(prev) * 100
    txt = f"{d:+.0f}%" if abs(d) >= 10 else f"{d:+.1f}%"
    return {"value": d, "text": txt, "dir": (d > 0.05) - (d < -0.05)}


# ===========================================================================
# Auth & CSRF
# ===========================================================================

def _b(s: str) -> bytes:
    return s.encode("utf-8")


def _session_value(token: str) -> str:
    return hmac.new(_b(token), b"clipagent-dashboard-session:v1", hashlib.sha256).hexdigest()


# --- failed-auth throttling (per client IP, in memory; one process serves the web) -------------
AUTH_FAIL_LIMIT = 10                 # failed token attempts per client ...
AUTH_FAIL_WINDOW_SEC = 15 * 60       # ... within this window -> 429 until the oldest ages out
_auth_failures: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _auth_throttled(request: Request) -> bool:
    import time

    now = time.monotonic()
    ip = _client_ip(request)
    hits = [t for t in _auth_failures.get(ip, []) if now - t < AUTH_FAIL_WINDOW_SEC]
    if hits:
        _auth_failures[ip] = hits
    else:
        _auth_failures.pop(ip, None)
    return len(hits) >= AUTH_FAIL_LIMIT


def _record_auth_failure(request: Request) -> None:
    import time

    if len(_auth_failures) > 10_000:  # bound memory under a spray from many addresses
        _auth_failures.clear()
    _auth_failures.setdefault(_client_ip(request), []).append(time.monotonic())


_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient", "testserver"}
_FORWARD_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip", "x-forwarded-host", "cf-connecting-ip",
                    "true-client-ip")


def _is_direct_local(request: Request) -> bool:
    """A request made on this machine to 127.0.0.1 (e.g. through ``ssh -L``), not relayed by a proxy.

    A reverse proxy or tunnel on the same box also connects from 127.0.0.1, so the client address
    alone proves nothing: relayed requests carry forwarding headers and/or the public Host name.
    """
    if not _is_loopback(request):
        return False
    if any(h in request.headers for h in _FORWARD_HEADERS):
        return False
    host = (request.headers.get("host") or "").strip().lower()
    hostname = host[1:host.index("]")] if host.startswith("[") and "]" in host else host.rsplit(":", 1)[0]
    return hostname in _LOCAL_HOSTS


def _auth_method(request: Request) -> str | None:
    """How this request is authenticated ("open", "bearer", "query", "cookie") or None.

    Without ``DASHBOARD_TOKEN`` the dashboard is "open" only to direct loopback requests (or
    everyone with ``DASHBOARD_ALLOW_UNAUTHENTICATED=true``): it can approve strategy changes.
    """
    s = get_settings()
    token = s.dashboard_token
    if not token:
        return "open" if (s.dashboard_allow_unauthenticated or _is_direct_local(request)) else None
    auth = request.headers.get("authorization", "")
    presented = False
    if auth[:7].lower() == "bearer ":
        presented = True
        if hmac.compare_digest(_b(auth[7:].strip()), _b(token)):
            return "bearer"
    q = request.query_params.get("token")
    if q is not None:
        presented = True
        if hmac.compare_digest(_b(q), _b(token)):
            return "query"
    c = request.cookies.get(COOKIE_NAME)
    if c and hmac.compare_digest(_b(c), _b(_session_value(token))):
        return "cookie"
    if presented:
        _record_auth_failure(request)
    return None


def _is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"


def _set_session_cookie(resp: Response, request: Request) -> None:
    resp.set_cookie(COOKIE_NAME, _session_value(get_settings().dashboard_token), max_age=COOKIE_MAX_AGE,
                    httponly=True, samesite="lax", secure=_is_https(request), path="/dashboard")


def _strip_token_url(request: Request) -> str:
    params = [(k, v) for k, v in request.query_params.multi_items() if k != "token"]
    return request.url.path + (("?" + urlencode(params)) if params else "")


def _safe_next(value: str | None, default: str = "/dashboard/") -> str:
    """Only allow redirects to dashboard paths on this host (no open redirect)."""
    if not value or not value.startswith("/dashboard") or value.startswith("//") or "\\" in value:
        return default
    p = urlparse(value)
    if p.scheme or p.netloc:
        return default
    return value


def _guard(request: Request, *, api: bool = False) -> tuple[Response | None, str]:
    """Return (response_to_send_instead, auth_method)."""
    if _auth_throttled(request):
        return _too_many(request, api), ""
    method = _auth_method(request)
    if method is None and not get_settings().dashboard_token:
        msg = ("The dashboard has no password (DASHBOARD_TOKEN is not set), so it only answers "
               "requests made directly on the server, e.g. through an SSH tunnel to 127.0.0.1. "
               "Set DASHBOARD_TOKEN in config.env and restart to use it from here.")
        if api:
            return _security_headers(JSONResponse({"error": "dashboard_token_not_set", "detail": msg},
                                                  status_code=403)), ""
        return _render(request, "error.html", {"title": "Dashboard locked", "message": msg},
                       status_code=403, nav="login"), ""
    if method is None:
        if api:
            return JSONResponse({"error": "unauthorized", "detail": "set ?token= or Authorization: Bearer"},
                                status_code=401, headers={"WWW-Authenticate": "Bearer"}), ""
        return _render(request, "login.html", {"next": _strip_token_url(request), "error": None},
                       status_code=401, nav="login"), ""
    if method == "query" and not api and request.method == "GET":
        resp = RedirectResponse(_strip_token_url(request), status_code=303)
        _set_session_cookie(resp, request)
        _security_headers(resp)
        return resp, method
    return None, method


def _too_many(request: Request, api: bool) -> Response:
    msg = "Too many wrong tokens from this address. Wait 15 minutes and try again."
    if api:
        return _security_headers(JSONResponse({"error": "too_many_attempts", "detail": msg}, status_code=429,
                                              headers={"Retry-After": str(AUTH_FAIL_WINDOW_SEC)}))
    resp = _render(request, "error.html", {"title": "Too many attempts", "message": msg}, status_code=429,
                   nav="login")
    resp.headers["Retry-After"] = str(AUTH_FAIL_WINDOW_SEC)
    return resp


def _csrf_secret() -> str:
    token = get_settings().dashboard_token
    if token:
        return token
    secret = db.kv_get("dashboard:csrf_secret")
    if not secret:
        db.kv_set_if_absent("dashboard:csrf_secret", secrets.token_hex(32))
        secret = db.kv_get("dashboard:csrf_secret")
    return str(secret)


def csrf_token() -> str:
    return hmac.new(_b(_csrf_secret()), b"clipagent-dashboard-csrf:v1", hashlib.sha256).hexdigest()


def _origin_ok(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return True  # same-origin form posts from older browsers; the CSRF field still applies
    if origin == "null":
        return False
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    return urlparse(origin).netloc.lower() == host.lower()


def _csrf_ok(request: Request, method: str, form: Mapping[str, Any]) -> bool:
    if method == "bearer":
        return True
    if not _origin_ok(request):
        return False
    sent = str(form.get("csrf") or "")
    return bool(sent) and hmac.compare_digest(_b(sent), _b(csrf_token()))


def _is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost", "testclient", "testserver")


# ===========================================================================
# Templates
# ===========================================================================

@lru_cache(maxsize=1)
def _env():
    import jinja2

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=jinja2.select_autoescape(["html", "xml"], default_for_string=True),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(
        compact=fmt_compact, int=fmt_int, usd=fmt_usd, pct=fmt_pct, duration=fmt_duration, hours=fmt_hours,
        dt=fmt_dt, date=fmt_date, ago=fmt_ago, pretty_json=fmt_json, ext_url=ext_url,
    )
    return env


def _security_headers(resp: Response) -> Response:
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers.setdefault("Cache-Control", "no-store")
    return resp


def _link_factory(request: Request):
    base = {k: v for k, v in request.query_params.items() if k not in ("token", "msg")}

    def link(_path: str | None = None, **changes: Any) -> str:
        params = dict(base)
        for k, v in changes.items():
            if v is None or v == "":
                params.pop(k, None)
            else:
                params[k] = str(v)
        if "page" not in changes and any(k not in ("page",) for k in changes):
            params.pop("page", None)
        path = _path or request.url.path
        return path + (("?" + urlencode(params)) if params else "")

    return link


def _render(request: Request, template: str, ctx: dict[str, Any], *, status_code: int = 200,
            nav: str = "") -> HTMLResponse:
    s = get_settings()
    try:
        demo = db.kv_get(DEMO_KV_KEY)
    except sqlite3.Error:  # pragma: no cover - unreadable DB still renders the error page
        demo = None
    flash = _FLASH.get(request.query_params.get("msg", ""))
    base = {
        "request_path": request.url.path,
        "nav": NAV,
        "active": nav,
        "demo": demo,
        "dry_run": s.dry_run,
        "token_required": bool(s.dashboard_token),
        "open_remote": not s.dashboard_token and not _is_loopback(request),
        "tz": s.timezone,
        "now_local": fmt_dt(db.now_iso()),
        "csrf": csrf_token() if nav != "login" else "",
        "flash": flash,
        "link": _link_factory(request),
        "owner": s.owner_name,
    }
    base.update(ctx)
    html = _env().get_template(template).render(**base)
    return _security_headers(HTMLResponse(html, status_code=status_code))


def _json(data: Any, status_code: int = 200) -> JSONResponse:
    resp = JSONResponse(json.loads(json.dumps(data, default=str)), status_code=status_code)
    return _security_headers(resp)  # type: ignore[return-value]


def _not_found(request: Request, what: str) -> HTMLResponse:
    return _render(request, "error.html", {"title": "Not found", "message": f"{what} does not exist."},
                   status_code=404)


# ===========================================================================
# Small statistics helpers
# ===========================================================================

def _median(xs: Sequence[float]) -> float | None:
    return statistics.median(xs) if xs else None


def _mean(xs: Sequence[float]) -> float | None:
    return statistics.fmean(xs) if xs else None


def _ranks(xs: Sequence[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        r = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = r
        i = j + 1
    return ranks


def spearman(xs: Sequence[float], ys: Sequence[float]) -> dict[str, Any]:
    """Spearman rank correlation with a Fisher-z two-sided p-value (approximate)."""
    n = len(xs)
    if n < 3:
        return {"n": n, "r": None, "p": None}
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx == 0 or syy == 0:
        return {"n": n, "r": None, "p": None}
    r = max(-0.999999, min(0.999999, sxy / math.sqrt(sxx * syy)))
    p = None
    if n > 3:
        z = math.atanh(r) * math.sqrt(n - 3)
        p = math.erfc(abs(z) / math.sqrt(2))
    return {"n": n, "r": r, "p": p}


def describe_correlation(res: dict[str, Any]) -> str:
    n, r, p = res["n"], res["r"], res["p"]
    if r is None:
        return (f"Not enough data yet ({n} published clip{'s' if n != 1 else ''} with {MATURE_DAYS}-day views). "
                "Come back when there are at least 20.")
    size = abs(r)
    strength = ("no meaningful" if size < 0.1 else "a weak" if size < 0.3 else "a moderate" if size < 0.5
                else "a strong")
    direction = "" if size < 0.1 else (" positive" if r > 0 else " negative")
    txt = f"Spearman rank correlation r = {r:+.2f} across {n} clips: {strength}{direction} relationship."
    if n < 20:
        txt += " With fewer than 20 clips this is anecdotal - do not act on it yet."
    elif p is not None and p >= 0.05:
        txt += f" A relationship this size could easily be noise at this sample size (p ≈ {p:.2f})."
    elif p is not None:
        txt += f" Unlikely to be pure noise (p ≈ {p:.3f}), but correlation is not causation."
    if size >= 0.1 and r > 0:
        txt += " Higher Haiku scores have tended to come with more views."
    elif size >= 0.1:
        txt += " Higher Haiku scores have tended to come with FEWER views - the picker may be miscalibrated."
    else:
        txt += " So far the picker's score does not predict views."
    return txt


def _welch(a: Sequence[float], b: Sequence[float]) -> tuple[float, float, float] | None:
    """(diff of means b-a, ci_low, ci_high) with a normal-approx 95% CI."""
    if len(a) < 2 or len(b) < 2:
        return None
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    va, vb = statistics.variance(a), statistics.variance(b)
    se = math.sqrt(va / len(a) + vb / len(b))
    d = mb - ma
    return d, d - 1.96 * se, d + 1.96 * se


def hook_form(text: str | None) -> str:
    """Rough hook classification from the on-screen hook text (derived, not stored)."""
    t = (text or "").strip()
    if not t:
        return "No hook"
    first = re.split(r"\W+", t.lower(), maxsplit=1)[0] if re.search(r"\w", t) else ""
    if t.endswith("?") or first in {"why", "how", "what", "who", "when", "where", "which", "can", "could",
                                     "would", "is", "are", "do", "does", "did", "will", "should", "ever"}:
        return "Question"
    if re.search(r"\d", t):
        return "Number / stat"
    return "Statement / claim"


def length_bucket(sec: float | None) -> str:
    if sec is None:
        return "Unknown"
    for hi, label in ((20, "< 20 s"), (30, "20–29 s"), (40, "30–39 s"), (50, "40–49 s"), (60, "50–59 s")):
        if sec < hi:
            return label
    return "60 s +"


def score_bucket(score: float | None) -> str:
    if score is None:
        return "Unscored"
    s = round(score)
    return "≤ 6" if s <= 6 else str(int(s))


_LENGTH_ORDER = ["< 20 s", "20–29 s", "30–39 s", "40–49 s", "50–59 s", "60 s +", "Unknown"]
_SCORE_ORDER = ["≤ 6", "7", "8", "9", "10", "Unscored"]
_DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ===========================================================================
# Reporting queries (read-only; reads via db.connect()). Candidates for db.py helpers.
# ===========================================================================

@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    c = db.connect()
    try:
        yield c
    finally:
        c.close()


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r["name"] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _sql_local_offset() -> str:
    """SQLite date modifier approximating the local UTC offset (e.g. '-300 minutes')."""
    off = db.local_now().utcoffset() or timedelta(0)
    return f"{int(off.total_seconds() // 60):+d} minutes"


def _day_bounds(start: date, end: date) -> tuple[str, str]:
    return db.local_day_bounds(start)[0], db.local_day_bounds(end)[1]


def _days(start: date, end: date) -> list[date]:
    n = (end - start).days
    return [start + timedelta(days=i) for i in range(n + 1)] if n >= 0 else []


def _bucket_hours_to_local_days(rows: Sequence[sqlite3.Row], key: str = "h") -> dict[date, dict[str, float]]:
    """Rows keyed by UTC hour text 'YYYY-MM-DDTHH' -> summed numeric columns per local day."""
    out: dict[date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    tz = db.local_tz()
    for r in rows:
        h = r[key]
        if not h:
            continue
        d = datetime.fromisoformat(h + ":00:00+00:00").astimezone(tz).date()
        for k in r.keys():
            if k != key and isinstance(r[k], (int, float)):
                out[d][k] += r[k]
    return out


def analytics_as_of(conn: sqlite3.Connection) -> date | None:
    row = conn.execute("SELECT MAX(day) AS d FROM stats").fetchone()
    return date.fromisoformat(row["d"]) if row and row["d"] else None


def first_data_day(conn: sqlite3.Connection) -> date | None:
    cands = []
    r = conn.execute("SELECT MIN(day) AS d FROM stats").fetchone()
    if r["d"]:
        cands.append(date.fromisoformat(r["d"]))
    for sql in ("SELECT MIN(ts) AS t FROM llm_usage", "SELECT MIN(uploaded_at) AS t FROM uploads",
                "SELECT MIN(discovered_at) AS t FROM sources"):
        t = conn.execute(sql).fetchone()["t"]
        if t:
            cands.append(db.local_date_of(t))
    return min(cands) if cands else None


@dataclass
class Window:
    key: str
    label: str
    start: date
    end: date
    prev_start: date | None
    prev_end: date | None

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "start": self.start.isoformat(), "end": self.end.isoformat(),
                "previous_start": self.prev_start.isoformat() if self.prev_start else None,
                "previous_end": self.prev_end.isoformat() if self.prev_end else None}


def resolve_window(conn: sqlite3.Connection, key: str | None, end: date, default: str = "28") -> Window:
    key = key if key in PERIODS else default
    n = PERIODS[key]
    if n is None:
        start = min(first_data_day(conn) or end, end)
        return Window(key, PERIOD_LABELS[key], start, end, None, None)
    start = end - timedelta(days=n - 1)
    return Window(key, PERIOD_LABELS[key], start, end, start - timedelta(days=n), start - timedelta(days=1))


def overview_anchor(conn: sqlite3.Connection) -> tuple[date, date | None]:
    """(end day for period KPIs, latest analytics day). YouTube Analytics lags ~2 days, so
    period metrics end on the latest day with stats when that is recent (keeps views and spend
    in the same window)."""
    today = db.local_today()
    as_of = analytics_as_of(conn)
    if as_of and today - timedelta(days=7) <= as_of <= today:
        return as_of, as_of
    return today, as_of


def revenue_column(conn: sqlite3.Connection) -> str | None:
    """Name of the per-day revenue column in ``stats`` (USD), if the schema has one."""
    for col in ("estimated_revenue", "estimated_revenue_usd"):
        if _has_column(conn, "stats", col):
            return col
    return None


def has_revenue_data(conn: sqlite3.Connection) -> bool:
    """Revenue tiles only appear once YouTube has actually reported revenue (monetary scope granted)."""
    col = revenue_column(conn)
    return bool(col) and conn.execute(f"SELECT 1 FROM stats WHERE {col} IS NOT NULL LIMIT 1").fetchone() is not None


def video_retention(conn: sqlite3.Connection, video_id: str, stats: Sequence[Any] = ()) -> Any:
    """Retention curve: the ``retention`` table (db.get_retention) when present, else the latest per-day copy."""
    getter = getattr(db, "get_retention", None)
    if getter is not None:
        try:
            r = getter(video_id, conn=conn)
            if r is not None and getattr(r, "curve", None):
                return r.curve
        except sqlite3.Error:
            pass
    return next((s.retention for s in reversed(stats) if getattr(s, "retention", None)), None)


def stats_by_day(conn: sqlite3.Connection, start: date, end: date) -> dict[str, dict[str, float]]:
    """Per local day sums from ``stats`` (one indexed range scan shared by tiles and charts)."""
    col = revenue_column(conn)
    rev = f", SUM({col}) AS revenue, COUNT({col}) AS revenue_n" if col else ""
    rows = conn.execute(
        f"""SELECT day, SUM(views) AS views, SUM(watch_minutes) AS watch_minutes,
                  SUM(subscribers_gained) AS sg, SUM(subscribers_lost) AS sl,
                  SUM(CASE WHEN avg_view_pct IS NOT NULL THEN avg_view_pct * views END) AS pct_w,
                  SUM(CASE WHEN avg_view_pct IS NOT NULL THEN views END) AS pct_v,
                  SUM(likes) AS likes, SUM(shares) AS shares {rev}
           FROM stats WHERE day >= ? AND day <= ? GROUP BY day""", (start.isoformat(), end.isoformat())).fetchall()
    return {r["day"]: {k: (r[k] or 0) for k in r.keys() if k != "day"} for r in rows}


def window_metrics(conn: sqlite3.Connection, start: date, end: date,
                   daily: Mapping[str, Mapping[str, float]] | None = None) -> dict[str, Any]:
    if daily is None:
        daily = stats_by_day(conn, start, end)
    s_day, e_day = start.isoformat(), end.isoformat()
    tot: dict[str, float] = defaultdict(float)
    for day, row in daily.items():
        if s_day <= day <= e_day:
            for k, v in row.items():
                tot[k] += v
    has_rev = revenue_column(conn) is not None and has_revenue_data(conn)
    r = {k: tot.get(k, 0) for k in ("views", "watch_minutes", "sg", "sl", "pct_w", "pct_v", "likes", "shares",
                                     "revenue")}
    s, e = _day_bounds(start, end)
    pub = conn.execute(
        "SELECT COUNT(*) AS n FROM uploads WHERE status IN ('uploaded','removed') AND dry_run = 0 "
        "AND COALESCE(publish_at, uploaded_at) >= ? AND COALESCE(publish_at, uploaded_at) < ?", (s, e)).fetchone()["n"]
    spend = db.llm_spend_between(s, e, conn=conn)
    views = int(r["views"])
    net = int(r["sg"]) - int(r["sl"])
    out = {
        "views": views,
        "watch_minutes": float(r["watch_minutes"]),
        "subs_net": net,
        "subs_gained": int(r["sg"]),
        "avg_view_pct": (r["pct_w"] / r["pct_v"]) if r["pct_v"] else None,
        "published": int(pub),
        "spend": spend,
        "cost_per_1k": (spend / views * 1000) if views else None,
        "cost_per_sub": (spend / net) if net > 0 else None,
        "likes": int(r["likes"]),
        "like_rate": (int(r["likes"]) / views * 100) if views else None,
        "revenue": None,
        "rpm": None,
    }
    if has_rev:
        out["revenue"] = float(r["revenue"])
        out["rpm"] = (out["revenue"] / views * 1000) if views else None
    return out


def strategy_caps_by_day(conn: sqlite3.Connection, days: Sequence[date]) -> tuple[list[float], list[int]]:
    """Daily LLM cap and upload cap in force on each local day (from strategy_versions history)."""
    import budget
    import strategy as strategy_mod

    s = get_settings()
    rows = conn.execute("SELECT version, created_at, json FROM strategy_versions ORDER BY version").fetchall()
    hist: list[tuple[date, float, int]] = []
    for r in rows:
        try:
            j = json.loads(r["json"])
            hist.append((db.local_date_of(r["created_at"]),
                         min(float(j.get("daily_llm_budget_usd", s.daily_llm_budget_usd)), s.max_daily_llm_budget_usd),
                         min(int(j.get("uploads_per_day", s.max_uploads_per_day)), s.max_uploads_per_day)))
        except (ValueError, TypeError):
            continue
    cur_cap = budget.daily_cap_usd()
    cur_up = strategy_mod.upload_cap()
    caps, ups = [], []
    for d in days:
        eff = [h for h in hist if h[0] <= d]
        if eff:
            caps.append(eff[-1][1]); ups.append(eff[-1][2])
        elif hist:
            caps.append(hist[0][1]); ups.append(hist[0][2])
        else:
            caps.append(cur_cap); ups.append(cur_up)
    if days and days[-1] == db.local_today():  # today: the live values win
        caps[-1], ups[-1] = cur_cap, cur_up
    return caps, ups


def daily_channel(conn: sqlite3.Connection, start: date, end: date) -> dict[str, int | None]:
    rows = conn.execute("SELECT day, subscribers FROM channel_stats WHERE day >= ? AND day <= ? ORDER BY day",
                        (start.isoformat(), end.isoformat())).fetchall()
    return {r["day"]: r["subscribers"] for r in rows}


def daily_spend(conn: sqlite3.Connection, start: date, end: date, *, by_model: bool = False) -> dict[date, dict[str, float]]:
    s, e = _day_bounds(start, end)
    if by_model:
        rows = conn.execute(
            "SELECT substr(ts,1,13) AS h, model, SUM(cost_usd) AS c FROM llm_usage WHERE ts >= ? AND ts < ? "
            "GROUP BY h, model", (s, e)).fetchall()
        out: dict[date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        tz = db.local_tz()
        for r in rows:
            d = datetime.fromisoformat(r["h"] + ":00:00+00:00").astimezone(tz).date()
            out[d][r["model"]] += r["c"] or 0.0
        return out
    rows = conn.execute("SELECT substr(ts,1,13) AS h, SUM(cost_usd) AS c FROM llm_usage WHERE ts >= ? AND ts < ? "
                        "GROUP BY h", (s, e)).fetchall()
    return _bucket_hours_to_local_days(rows)


def daily_uploads(conn: sqlite3.Connection, start: date, end: date) -> dict[date, dict[str, float]]:
    """Successful uploads per local day of ``uploaded_at`` (what the cap counts), dry runs included."""
    s, e = _day_bounds(start, end)
    rows = conn.execute(
        """SELECT substr(uploaded_at,1,13) AS h, COUNT(*) AS n, SUM(dry_run) AS dry, SUM(quota_units) AS quota
           FROM uploads WHERE status IN ('uploaded','removed') AND uploaded_at >= ? AND uploaded_at < ?
           GROUP BY h""", (s, e)).fetchall()
    return _bucket_hours_to_local_days(rows)


def pipeline_health(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    now = db.utcnow()
    since = db.to_iso(now - timedelta(hours=24))
    rows = conn.execute(
        """SELECT module,
                  MAX(CASE WHEN level IN ('info','debug') THEN ts END) AS last_ok,
                  MAX(CASE WHEN level = 'error' THEN ts END) AS last_error,
                  SUM(CASE WHEN level = 'error' AND ts >= ? THEN 1 ELSE 0 END) AS errors_24h,
                  SUM(CASE WHEN level = 'warning' AND ts >= ? THEN 1 ELSE 0 END) AS warnings_24h
           FROM events GROUP BY module""", (since, since)).fetchall()
    by_mod = {r["module"]: dict(r) for r in rows}
    out = []
    for module, label, idle_h in STAGES:
        r = by_mod.get(module, {})
        last_ok, last_err = r.get("last_ok"), r.get("last_error")
        errs = int(r.get("errors_24h") or 0)
        if errs:
            status = "critical"
            status_text = f"{errs} failure{'s' if errs != 1 else ''} in 24 h"
        elif not last_ok and not last_err:
            status, status_text = "none", "No runs yet"
        elif last_ok and now - db.parse_iso(last_ok) > timedelta(hours=idle_h):
            status, status_text = "warning", "Idle"
        elif last_err and (not last_ok or last_err > last_ok):
            status, status_text = "serious", "Last run failed"
        else:
            status, status_text = "good", "OK"
        last_msg = None
        if last_err:
            m = conn.execute("SELECT message FROM events WHERE module = ? AND level = 'error' ORDER BY ts DESC, id DESC "
                             "LIMIT 1", (module,)).fetchone()
            last_msg = m["message"] if m else None
        out.append({"module": module, "label": label, "last_success": last_ok, "last_error": last_err,
                    "last_error_message": last_msg, "errors_24h": errs,
                    "warnings_24h": int(r.get("warnings_24h") or 0), "status": status, "status_text": status_text})
    return out


def failures_24h(conn: sqlite3.Connection) -> int:
    since = db.to_iso(db.utcnow() - timedelta(hours=24))
    return int(conn.execute("SELECT COUNT(*) AS n FROM events WHERE level = 'error' AND ts >= ?",
                            (since,)).fetchone()["n"])


def status_counts(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    out = {"sources": db.count_sources_by_status(conn=conn), "clips": {}, "uploads": {}}
    for r in conn.execute("SELECT status, COUNT(*) AS n FROM clips GROUP BY status"):
        out["clips"][r["status"]] = r["n"]
    for r in conn.execute("SELECT status, COUNT(*) AS n FROM uploads GROUP BY status"):
        out["uploads"][r["status"]] = r["n"]
    return out


CLIP_SORTS = {
    "views": "views", "published": "published_at", "title": "c.title COLLATE NOCASE", "topic": "s.topic",
    "type": "s.source_type", "length": "length", "score": "c.haiku_score", "pct": "pct", "avd": "avd",
    "likes": "likes", "subs": "subs", "cost": "cost", "status": "c.status", "created": "c.created_at",
}

_CLIP_SQL = """
WITH lu AS (SELECT clip_id, COALESCE(MAX(CASE WHEN status != 'superseded' THEN id END), MAX(id)) AS uid
            FROM uploads GROUP BY clip_id),
st AS (
    SELECT youtube_video_id AS vid, SUM(views) AS views, SUM(likes) AS likes,
           SUM(subscribers_gained) - SUM(subscribers_lost) AS subs, SUM(watch_minutes) AS wm,
           SUM(CASE WHEN avg_view_pct IS NOT NULL THEN avg_view_pct * views END)
             / NULLIF(SUM(CASE WHEN avg_view_pct IS NOT NULL THEN views END), 0) AS pct,
           SUM(CASE WHEN avg_view_duration_sec IS NOT NULL THEN avg_view_duration_sec * views END)
             / NULLIF(SUM(CASE WHEN avg_view_duration_sec IS NOT NULL THEN views END), 0) AS avd
    FROM stats {stats_where} GROUP BY youtube_video_id),
cd AS (SELECT clip_id, SUM(cost_usd) AS c FROM llm_usage WHERE clip_id IS NOT NULL GROUP BY clip_id),
cs AS (SELECT source_id, SUM(cost_usd) AS c FROM llm_usage WHERE clip_id IS NULL AND source_id IS NOT NULL
       GROUP BY source_id),
nc AS (SELECT source_id, COUNT(*) AS n FROM clips GROUP BY source_id)
SELECT c.id, c.title, c.status, c.status_reason, c.haiku_score, c.start_sec, c.end_sec,
       (c.end_sec - c.start_sec) AS length, c.experiment_id, c.experiment_variant, c.strategy_version,
       c.created_at, c.hook_text, s.id AS source_id, s.topic, s.source_type, s.channel,
       u.id AS upload_id, u.status AS upload_status, u.youtube_video_id, u.dry_run, u.copyright_claim,
       u.publish_at, u.uploaded_at, u.error AS upload_error, u.youtube_status,
       u.lifetime_views, u.lifetime_fetched_at, COALESCE(st.views, 0) AS analytics_views,
       COALESCE(u.publish_at, u.uploaded_at) AS published_at,
       MAX(COALESCE(st.views, 0), COALESCE(u.lifetime_views, 0)) AS views, st.pct AS pct, st.avd AS avd,
       MAX(COALESCE(st.likes, 0), COALESCE(u.lifetime_likes, 0)) AS likes,
       COALESCE(st.subs, 0) AS subs, COALESCE(st.wm, 0) AS watch_minutes,
       COALESCE(cd.c, 0) + COALESCE(cs.c, 0) / COALESCE(nc.n, 1) AS cost
FROM clips c
JOIN sources s ON s.id = c.source_id
LEFT JOIN lu ON lu.clip_id = c.id
LEFT JOIN uploads u ON u.id = lu.uid
LEFT JOIN st ON st.vid = u.youtube_video_id
LEFT JOIN cd ON cd.clip_id = c.id
LEFT JOIN cs ON cs.source_id = c.source_id
LEFT JOIN nc ON nc.source_id = c.source_id
"""


def _clip_filters(f: Mapping[str, Any]) -> tuple[str, list[Any]]:
    where, params = [], []
    if f.get("status"):
        if f["status"] == "published":
            where.append("c.status = 'uploaded'")
        else:
            where.append("c.status = ?")
            params.append(f["status"])
    if f.get("topic"):
        where.append("s.topic = ?")
        params.append(f["topic"])
    if f.get("type"):
        where.append("s.source_type = ?")
        params.append(f["type"])
    if f.get("experiment"):
        where.append("c.experiment_id = ?")
        params.append(f["experiment"])
    if f.get("q"):
        q = str(f["q"]).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("(c.title LIKE ? ESCAPE '\\' OR s.channel LIKE ? ESCAPE '\\' OR s.title LIKE ? ESCAPE '\\')")
        params += [f"%{q}%"] * 3
    if f.get("published_from"):
        where.append("c.id IN (SELECT clip_id FROM uploads WHERE status IN ('uploaded','removed') AND dry_run = 0 "
                     "AND COALESCE(publish_at, uploaded_at) >= ? AND COALESCE(publish_at, uploaded_at) < ?)")
        params += [f["published_from"], f["published_to"]]
    return (" WHERE " + " AND ".join(where)) if where else "", params


def clip_rows(conn: sqlite3.Connection, filters: Mapping[str, Any] | None = None, *, sort: str = "views",
              desc: bool = True, limit: int | None = PAGE_SIZE, offset: int = 0,
              clip_ids: Sequence[int] | None = None) -> list[dict[str, Any]]:
    filters = filters or {}
    where, params = _clip_filters(filters)
    if clip_ids is not None:
        ids = [int(i) for i in clip_ids] or [-1]
        where += (" AND " if where else " WHERE ") + f"c.id IN ({','.join('?' * len(ids))})"
        params += ids
    order = CLIP_SORTS.get(sort, "views")
    stats_where = ""
    if clip_ids is not None or filters.get("published_from"):
        # only aggregate stats for the clips this query can return (cheap detail / top-N lookups)
        stats_where = (" WHERE youtube_video_id IN (SELECT u.youtube_video_id FROM uploads u JOIN clips c "
                       "ON c.id = u.clip_id JOIN sources s ON s.id = c.source_id" + where + ")")
        params = params * 2
    sql = _CLIP_SQL.format(stats_where=stats_where) + where + \
        f" ORDER BY {order} {'DESC' if desc else 'ASC'} NULLS LAST, c.id DESC"
    if limit is not None:
        sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    for r in rows:
        r["display_status"] = _clip_display_status(r)
    return rows


def clip_count(conn: sqlite3.Connection, filters: Mapping[str, Any] | None = None) -> int:
    where, params = _clip_filters(filters or {})
    return int(conn.execute(f"SELECT COUNT(*) AS n FROM clips c JOIN sources s ON s.id = c.source_id{where}",
                            params).fetchone()["n"])


def _clip_display_status(r: Mapping[str, Any]) -> tuple[str, str]:
    """(label, tone) for a clip row."""
    st = r["status"]
    if st == "uploaded":
        if r.get("dry_run"):
            return "Dry run", "neutral"
        if r.get("copyright_claim"):
            return "Claimed", "critical"
        pub = r.get("publish_at")
        if pub and pub > db.now_iso():
            return "Scheduled", "info"
        if r.get("upload_status") == "removed":
            return "Removed", "serious"
        return "Published", "good"
    return {"picked": ("Picked", "neutral"), "rendered": ("Ready", "info"), "scheduled": ("Scheduled", "info"),
            "failed": ("Failed", "critical"), "skipped": ("Skipped", "neutral")}.get(st, (st, "neutral"))


def distinct_values(conn: sqlite3.Connection) -> dict[str, list[str]]:
    return {
        "topics": [r[0] for r in conn.execute(
            "SELECT DISTINCT s.topic FROM clips c JOIN sources s ON s.id = c.source_id WHERE s.topic IS NOT NULL "
            "ORDER BY s.topic")],
        "types": [r[0] for r in conn.execute("SELECT DISTINCT source_type FROM sources ORDER BY source_type")],
        "experiments": [r[0] for r in conn.execute(
            "SELECT DISTINCT experiment_id FROM clips WHERE experiment_id IS NOT NULL ORDER BY experiment_id")],
    }


def published_clip_facts(conn: sqlite3.Connection, experiment_id: str | None = None) -> list[dict[str, Any]]:
    """One row per real (non-dry-run) published clip with views in its first MATURE_DAYS days.

    The first-N-days window uses an approximate local offset in SQL (DST can shift it by an hour).
    """
    rows = conn.execute(
        f"""SELECT c.id, c.title, c.hook_text, c.haiku_score, (c.end_sec - c.start_sec) AS length,
                   c.experiment_id, c.experiment_variant, c.strategy_version,
                   s.topic, s.source_type, s.channel,
                   COALESCE(u.publish_at, u.uploaded_at) AS pub, u.youtube_video_id,
                   COALESCE(SUM(st.views), 0) AS views,
                   COALESCE(SUM(CASE WHEN st.day <= date(COALESCE(u.publish_at, u.uploaded_at), ?, '+{MATURE_DAYS - 1} days')
                                     THEN st.views END), 0) AS views_7d,
                   SUM(CASE WHEN st.avg_view_pct IS NOT NULL THEN st.avg_view_pct * st.views END)
                     / NULLIF(SUM(CASE WHEN st.avg_view_pct IS NOT NULL THEN st.views END), 0) AS pct,
                   COALESCE(SUM(st.subscribers_gained), 0) - COALESCE(SUM(st.subscribers_lost), 0) AS subs,
                   COUNT(st.id) AS days_tracked
            FROM uploads u
            JOIN clips c ON c.id = u.clip_id
            JOIN sources s ON s.id = c.source_id
            LEFT JOIN stats st ON st.youtube_video_id = u.youtube_video_id
            WHERE u.status IN ('uploaded','removed') AND u.dry_run = 0 {"AND c.experiment_id = ?" if experiment_id else ""}
            GROUP BY u.id ORDER BY pub""", (_sql_local_offset(), *([experiment_id] if experiment_id else []))).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        loc = _local(d["pub"])
        d["pub_local"] = loc
        d["pub_day"] = loc.date() if loc else None
        out.append(d)
    return out


def experiment_catalog(conn: sqlite3.Connection, current: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Every experiment seen in strategy history / the live strategy / clip rows, newest first."""
    exps: dict[str, dict[str, Any]] = {}
    for r in conn.execute("SELECT version, created_at, json FROM strategy_versions ORDER BY version"):
        try:
            e = json.loads(r["json"]).get("experiment")
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("id"):
            cur = exps.setdefault(e["id"], dict(e, versions=[]))
            cur.update({k: v for k, v in e.items()})
            cur["versions"].append(r["version"])
    if current and isinstance(current.get("experiment"), dict) and current["experiment"].get("id"):
        e = current["experiment"]
        cur = exps.setdefault(e["id"], dict(e, versions=[]))
        cur.update(e)
    for r in conn.execute("SELECT experiment_id, MIN(created_at) AS first FROM clips WHERE experiment_id IS NOT NULL "
                          "GROUP BY experiment_id"):
        exps.setdefault(r["experiment_id"], {"id": r["experiment_id"], "hypothesis": None, "variable": None,
                                             "control": None, "variant": None, "start": None, "end": None,
                                             "success_metric": None, "versions": []})
    today = db.local_today().isoformat()
    for e in exps.values():
        if e.get("start") and e.get("end"):
            e["status"] = "running" if e["start"] <= today <= e["end"] else ("finished" if e["end"] < today
                                                                           else "scheduled")
        else:
            e["status"] = "unknown"
    return sorted(exps.values(), key=lambda e: (e.get("start") or "", e["id"]), reverse=True)


def source_rows(conn: sqlite3.Connection, *, status: str | None, source_type: str | None, q: str | None,
                limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
    where, params = [], []
    if status:
        where.append("s.status = ?")
        params.append(status)
    if source_type:
        where.append("s.source_type = ?")
        params.append(source_type)
    if q:
        qq = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("(s.title LIKE ? ESCAPE '\\' OR s.channel LIKE ? ESCAPE '\\' OR s.url LIKE ? ESCAPE '\\')")
        params += [f"%{qq}%"] * 3
    w = (" WHERE " + " AND ".join(where)) if where else ""
    total = int(conn.execute(f"SELECT COUNT(*) AS n FROM sources s{w}", params).fetchone()["n"])
    rows = conn.execute(
        f"""SELECT s.id, s.url, s.source_type, s.platform, s.channel, s.title, s.duration_sec, s.licence,
                   s.licence_evidence, s.topic, s.status, s.status_reason, s.discovered_at,
                   (SELECT COUNT(*) FROM clips c WHERE c.source_id = s.id) AS clips,
                   (SELECT COUNT(*) FROM clips c WHERE c.source_id = s.id AND c.status = 'uploaded') AS published
            FROM sources s{w} ORDER BY s.discovered_at DESC, s.id DESC LIMIT ? OFFSET ?""",
        [*params, limit, offset]).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            ev = json.loads(d["licence_evidence"]) if d["licence_evidence"] else None
        except ValueError:
            ev = None
        d["evidence"] = ev if isinstance(ev, dict) else ({"raw": ev} if ev is not None else None)
        out.append(d)
    return out, total


def funnel(conn: sqlite3.Connection, start_iso: str, end_iso: str) -> dict[str, Any]:
    src = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM sources WHERE discovered_at >= ? AND discovered_at < ? GROUP BY status",
        (start_iso, end_iso))}
    total = sum(src.values())
    fetched = sum(src.get(k, 0) for k in ("fetched", "transcribed", "picked", "done"))
    transcribed = sum(src.get(k, 0) for k in ("transcribed", "picked", "done"))
    yielded = int(conn.execute(
        "SELECT COUNT(DISTINCT c.source_id) AS n FROM clips c JOIN sources s ON s.id = c.source_id "
        "WHERE s.discovered_at >= ? AND s.discovered_at < ?", (start_iso, end_iso)).fetchone()["n"])
    clips = {r["status"]: r["n"] for r in conn.execute(
        "SELECT c.status, COUNT(*) AS n FROM clips c JOIN sources s ON s.id = c.source_id "
        "WHERE s.discovered_at >= ? AND s.discovered_at < ? GROUP BY c.status", (start_iso, end_iso))}
    picked = sum(clips.values())
    rendered = sum(clips.get(k, 0) for k in ("rendered", "scheduled", "uploaded"))
    uploaded = clips.get("uploaded", 0)
    stages = [
        {"label": "Candidates found", "n": total, "unit": "sources"},
        {"label": "Licence-cleared & downloaded", "n": fetched, "unit": "sources"},
        {"label": "Transcribed", "n": transcribed, "unit": "sources"},
        {"label": "Yielded at least one clip", "n": yielded, "unit": "sources"},
        {"label": "Clips picked", "n": picked, "unit": "clips"},
        {"label": "Clips rendered", "n": rendered, "unit": "clips"},
        {"label": "Clips uploaded", "n": uploaded, "unit": "clips"},
    ]
    return {"stages": stages, "sources": src, "clips": clips,
            "nothing_worth_clipping": max(0, src.get("done", 0) + src.get("picked", 0) - yielded),
            "backlog": src.get("candidate", 0) + src.get("fetched", 0) + src.get("transcribed", 0)}


def loss_reasons(conn: sqlite3.Connection, start_iso: str, end_iso: str) -> dict[str, list[dict[str, Any]]]:
    def grp(sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
        return [dict(r) for r in conn.execute(sql, params)]
    return {
        "sources": grp("""SELECT status, COALESCE(NULLIF(substr(status_reason,1,140),''), '(no reason recorded)') AS reason,
                                 COUNT(*) AS n FROM sources
                          WHERE status IN ('rejected','failed') AND discovered_at >= ? AND discovered_at < ?
                          GROUP BY status, reason ORDER BY n DESC LIMIT 25""", (start_iso, end_iso)),
        "clips": grp("""SELECT c.status, COALESCE(NULLIF(substr(c.status_reason,1,140),''), '(no reason recorded)') AS reason,
                               COUNT(*) AS n FROM clips c
                        WHERE c.status IN ('failed','skipped') AND c.created_at >= ? AND c.created_at < ?
                        GROUP BY c.status, reason ORDER BY n DESC LIMIT 25""", (start_iso, end_iso)),
        "uploads": grp("""SELECT 'failed' AS status, COALESCE(NULLIF(substr(error,1,140),''), '(no error recorded)') AS reason,
                                 COUNT(*) AS n FROM uploads
                          WHERE status = 'failed' AND created_at >= ? AND created_at < ?
                          GROUP BY reason ORDER BY n DESC LIMIT 25""", (start_iso, end_iso)),
    }


LICENCE_CSV_COLUMNS = [
    "clip_id", "youtube_video_id", "youtube_url", "published_at_utc", "uploaded_at_utc", "dry_run", "upload_status",
    "copyright_claim", "clip_title", "clip_start_sec", "clip_end_sec", "licence", "credit_text", "source_url",
    "source_title", "publisher", "publisher_channel_id", "platform", "source_type", "licence_rule",
    "licence_metadata", "source_id",
]


def licence_audit_rows(conn: sqlite3.Connection, *, include_dry_run: bool = False) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""SELECT c.id AS clip_id, u.youtube_video_id, COALESCE(u.publish_at, u.uploaded_at) AS published_at_utc,
                   u.uploaded_at AS uploaded_at_utc, u.dry_run, u.status AS upload_status, u.copyright_claim,
                   c.title AS clip_title, c.start_sec AS clip_start_sec, c.end_sec AS clip_end_sec,
                   c.licence, c.credit_text, c.source_url, s.title AS source_title, s.channel AS publisher,
                   s.channel_id AS publisher_channel_id, s.platform, s.source_type, s.licence_evidence, s.id AS source_id
            FROM uploads u JOIN clips c ON c.id = u.clip_id JOIN sources s ON s.id = c.source_id
            WHERE u.status IN ('uploaded','removed') {'' if include_dry_run else 'AND u.dry_run = 0'}
            ORDER BY COALESCE(u.publish_at, u.uploaded_at), u.id""").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            ev = json.loads(d.pop("licence_evidence") or "null")
        except ValueError:
            ev = None
        ev = ev if isinstance(ev, dict) else {}
        vid = d["youtube_video_id"] or ""
        d["youtube_url"] = f"https://www.youtube.com/shorts/{vid}" if vid and not d["dry_run"] else ""
        d["licence_rule"] = ev.get("matched_rule") or ""
        d["licence_metadata"] = ev.get("license") or ""
        d["dry_run"] = int(bool(d["dry_run"]))
        d["copyright_claim"] = int(bool(d["copyright_claim"]))
        out.append(d)
    return out


def _csv_safe(v: Any) -> Any:
    """Neutralise spreadsheet formula injection in untrusted text cells."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def spend_breakdown(conn: sqlite3.Connection, start_iso: str, end_iso: str) -> dict[str, Any]:
    by_purpose = [dict(r) for r in conn.execute(
        """SELECT purpose, model, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens,
                  SUM(output_tokens) AS output_tokens, SUM(cache_read_tokens) AS cache_read_tokens,
                  SUM(cache_write_tokens) AS cache_write_tokens, SUM(cost_usd) AS cost, SUM(fake) AS fake_calls
           FROM llm_usage WHERE ts >= ? AND ts < ? GROUP BY purpose, model ORDER BY cost DESC""", (start_iso, end_iso))]
    by_model = [dict(r) for r in conn.execute(
        """SELECT model, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
                  SUM(cache_read_tokens) AS cache_read_tokens, SUM(cache_write_tokens) AS cache_write_tokens,
                  SUM(cost_usd) AS cost FROM llm_usage WHERE ts >= ? AND ts < ? GROUP BY model ORDER BY cost DESC""",
        (start_iso, end_iso))]
    tot = conn.execute(
        """SELECT COUNT(*) AS calls, COALESCE(SUM(input_tokens),0) AS input_tokens,
                  COALESCE(SUM(output_tokens),0) AS output_tokens, COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens,
                  COALESCE(SUM(cache_write_tokens),0) AS cache_write_tokens, COALESCE(SUM(cost_usd),0) AS cost,
                  COALESCE(SUM(CASE WHEN fake = 1 THEN cost_usd END),0) AS fake_cost
           FROM llm_usage WHERE ts >= ? AND ts < ?""", (start_iso, end_iso)).fetchone()
    for row in [*by_purpose, *by_model]:
        prompt = (row["input_tokens"] or 0) + (row["cache_read_tokens"] or 0) + (row["cache_write_tokens"] or 0)
        row["cache_hit"] = (row["cache_read_tokens"] or 0) / prompt * 100 if prompt else None
        row["cost_per_call"] = row["cost"] / row["calls"] if row["calls"] else None
    t = dict(tot)
    prompt = t["input_tokens"] + t["cache_read_tokens"] + t["cache_write_tokens"]
    t["cache_hit"] = t["cache_read_tokens"] / prompt * 100 if prompt else None
    t["clips_created"] = int(conn.execute("SELECT COUNT(*) AS n FROM clips WHERE created_at >= ? AND created_at < ?",
                                          (start_iso, end_iso)).fetchone()["n"])
    t["clips_published"] = int(conn.execute(
        "SELECT COUNT(*) AS n FROM uploads WHERE status IN ('uploaded','removed') AND uploaded_at >= ? AND uploaded_at < ?",
        (start_iso, end_iso)).fetchone()["n"])
    t["sources_scored"] = int(conn.execute(
        "SELECT COUNT(DISTINCT source_id) AS n FROM llm_usage WHERE ts >= ? AND ts < ? AND source_id IS NOT NULL",
        (start_iso, end_iso)).fetchone()["n"])
    t["per_clip"] = t["cost"] / t["clips_created"] if t["clips_created"] else None
    t["per_published"] = t["cost"] / t["clips_published"] if t["clips_published"] else None
    t["per_source"] = t["cost"] / t["sources_scored"] if t["sources_scored"] else None
    return {"by_purpose": by_purpose, "by_model": by_model, "totals": t}


def budget_cap_hits(conn: sqlite3.Connection, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
    return [{"ts": r["ts"], "message": r["message"], "data": json.loads(r["data"]) if r["data"] else None}
            for r in conn.execute(
                "SELECT ts, message, data FROM events WHERE module = 'budget' AND level = 'warning' "
                "AND message LIKE '%cap hit%' AND ts >= ? AND ts < ? ORDER BY ts DESC", (start_iso, end_iso))]


def event_page(conn: sqlite3.Connection, *, level: str | None, module: str | None, q: str | None,
               before: tuple[str, int] | None, limit: int) -> list[db.Event]:
    where, params = [], []
    if level:
        where.append("level = ?")
        params.append(level)
    if module:
        where.append("module = ?")
        params.append(module)
    if q:
        qq = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("message LIKE ? ESCAPE '\\'")
        params.append(f"%{qq}%")
    if before:
        where.append("(ts, id) < (?, ?)")
        params += list(before)
    w = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(f"SELECT * FROM events{w} ORDER BY ts DESC, id DESC LIMIT ?", [*params, limit]).fetchall()
    return [db.Event.from_row(r) for r in rows]


def event_modules(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT module FROM events ORDER BY module")]


def clip_costs(conn: sqlite3.Connection, clip_id: int, source_id: int) -> dict[str, Any]:
    direct = conn.execute("SELECT purpose, model, COUNT(*) AS calls, SUM(cost_usd) AS cost FROM llm_usage "
                          "WHERE clip_id = ? GROUP BY purpose, model", (clip_id,)).fetchall()
    shared = conn.execute("SELECT purpose, model, COUNT(*) AS calls, SUM(cost_usd) AS cost FROM llm_usage "
                          "WHERE source_id = ? AND clip_id IS NULL GROUP BY purpose, model", (source_id,)).fetchall()
    n = int(conn.execute("SELECT COUNT(*) AS n FROM clips WHERE source_id = ?", (source_id,)).fetchone()["n"]) or 1
    rows = [dict(r, share=1.0, attributed=r["cost"]) for r in direct]
    rows += [dict(r, share=1 / n, attributed=r["cost"] / n) for r in shared]
    return {"rows": rows, "total": sum(r["attributed"] for r in rows), "clips_from_source": n}


# ===========================================================================
# Chart specs (rendered by static/charts.js; table twin rendered server-side)
# ===========================================================================

def _fmt_for(kind: str):
    return {"usd": fmt_usd, "pct": fmt_pct, "int": fmt_int, "compact": fmt_compact,
            "dec1": lambda v: "—" if _num(v) is None else f"{float(v):.1f}"}.get(kind, fmt_int)


def time_chart(chart_id: str, days: Sequence[date], series: list[dict[str, Any]], *, kind: str = "line",
               fmt: str = "int", refs: list[dict[str, Any]] | None = None, stacked: bool = False,
               area: bool = False, height: int = 200) -> dict[str, Any]:
    """Spec for a daily time-series chart. ``series``: [{name, values, slot}] aligned with ``days``."""
    f = _fmt_for(fmt)
    cols = ["Date"] + [s["name"] for s in series] + [r["name"] for r in (refs or [])]
    rows = []
    for i, d in enumerate(days):
        row = [fmt_date(d.isoformat())]
        row += [f(s["values"][i]) if s["values"][i] is not None else "—" for s in series]
        row += [f(r["values"][i]) if r["values"][i] is not None else "—" for r in (refs or [])]
        rows.append(row)
    has_data = any(v not in (None, 0) for s in series for v in s["values"])
    return {"id": chart_id, "type": kind, "x": [d.isoformat() for d in days], "series": series, "refs": refs or [],
            "fmt": fmt, "stacked": stacked, "area": area, "height": height, "has_data": has_data,
            "table": {"columns": cols, "rows": list(reversed(rows))}}


def bar_rows(rows: list[dict[str, Any]], *, value_key: str, fmt: str, ref: float | None = None,
             ref_label: str = "") -> dict[str, Any]:
    """Spec for server-rendered ranked horizontal bars (HTML, no JS needed)."""
    f = _fmt_for(fmt)
    vals = [r[value_key] for r in rows if r.get(value_key) is not None]
    mx = max([*vals, ref or 0, 0]) or 1
    out = []
    for r in rows:
        v = r.get(value_key)
        out.append(dict(r, value=v, value_text=f(v) if v is not None else "—",
                        width=0 if v is None or v <= 0 else max(0.5, v / mx * 100)))
    return {"rows": out, "ref": ref, "ref_text": f(ref) if ref is not None else "", "ref_label": ref_label,
            "ref_pos": (ref / mx * 100) if ref else None}


def sparkline(values: Sequence[float | None]) -> str | None:
    """SVG path data in a 100x24 box for a tiny trend line (None when <2 points)."""
    pts = [v for v in values if v is not None]
    if len(pts) < 2 or not any(pts):
        return None
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1
    n = len(values)
    out = []
    for i, v in enumerate(values):
        if v is None:
            continue
        x = i / (n - 1) * 100
        y = 22 - (v - lo) / span * 20
        out.append(f"{'M' if not out else 'L'}{x:.2f},{y:.2f}")
    return " ".join(out)


# ===========================================================================
# Page data builders (shared by HTML pages and JSON API)
# ===========================================================================

def _load_strategy() -> tuple[dict[str, Any] | None, str | None]:
    import strategy as strategy_mod

    try:
        return dict(strategy_mod.load_strategy()), None
    except (OSError, ValueError) as exc:
        latest = db.latest_strategy_version()
        return (dict(latest.json) if latest else None), f"strategy file unreadable: {exc}"


def today_status() -> dict[str, Any]:
    import budget
    import strategy as strategy_mod

    cap = budget.daily_cap_usd()
    spent = budget.spent_today()
    up_cap = strategy_mod.upload_cap()
    uploads = db.count_uploads_on()
    pause = db.kv_get("budget:paused")
    return {"date": db.local_today().isoformat(), "uploads": uploads, "upload_cap": up_cap,
            "uploads_pct": min(100.0, uploads / up_cap * 100) if up_cap else 100.0,
            "spend_usd": spent, "spend_cap_usd": cap, "spend_pct": min(100.0, spent / cap * 100) if cap else 100.0,
            "paused": pause is not None, "pause_reason": (pause or {}).get("reason") if isinstance(pause, dict) else None,
            "cap_reached": spent >= cap, "quota_units": db.quota_used_on()}


def experiment_results(facts: list[dict[str, Any]], exp: Mapping[str, Any], as_of: date) -> dict[str, Any]:
    eid = exp["id"]
    metric = exp.get("success_metric") or "avg_view_pct"
    rows = [f for f in facts if f["experiment_id"] == eid]
    mature = [f for f in rows if f["pub_day"] and f["pub_day"] <= as_of - timedelta(days=MATURE_DAYS - 1)]
    arms: dict[str, dict[str, Any]] = {}
    for arm in ("control", "variant"):
        a_all = [f for f in rows if f["experiment_variant"] == arm]
        a = [f for f in mature if f["experiment_variant"] == arm]
        pct = [f["pct"] for f in a if f["pct"] is not None]
        v7 = [f["views_7d"] for f in a]
        views = sum(f["views"] for f in a)
        arms[arm] = {"n": len(a), "n_all": len(a_all), "pct": _mean(pct), "views_7d_median": _median(v7),
                     "subs_per_1k": (sum(f["subs"] for f in a) / views * 1000) if views else None,
                     "_pct": pct, "_logv": [math.log1p(x) for x in v7]}
    c, v = arms["control"], arms["variant"]
    if metric in ("views", "views_7d"):
        res = _welch(c["_logv"], v["_logv"])
        if res:
            d, lo, hi = res
            effect = {"text": f"×{math.exp(d):.2f}", "ci": f"×{math.exp(lo):.2f} – ×{math.exp(hi):.2f}",
                      "lo": lo, "hi": hi, "unit": "ratio of typical 7-day views (variant ÷ control)"}
        else:
            effect = None
        metric_label = f"{MATURE_DAYS}-day views"
    else:
        res = _welch(c["_pct"], v["_pct"])
        effect = ({"text": f"{res[0]:+.1f} pts", "ci": f"{res[1]:+.1f} to {res[2]:+.1f} pts", "lo": res[1],
                   "hi": res[2], "unit": "difference in average view % (variant − control)"} if res else None)
        metric_label = "Average view %"
    need = 20
    if c["n"] < need or v["n"] < need:
        verdict, tone = (f"Too early to call: {c['n']} control and {v['n']} variant clips have {MATURE_DAYS} days "
                         f"of data; need at least {need} each."), "neutral"
    elif effect and effect["lo"] > 0:
        verdict, tone = "Variant is ahead: the whole 95% interval is above zero effect.", "good"
    elif effect and effect["hi"] < 0:
        verdict, tone = "Control is ahead: the whole 95% interval is below zero effect.", "serious"
    else:
        verdict, tone = "No clear difference: the 95% interval includes zero effect.", "neutral"
    for arm in arms.values():
        arm.pop("_pct"); arm.pop("_logv")
    return {"metric": metric, "metric_label": metric_label, "arms": arms, "effect": effect, "verdict": verdict,
            "tone": tone, "pending": len(rows) - len(mature)}


def overview_data(conn: sqlite3.Connection, period: str | None) -> dict[str, Any]:
    end, as_of = overview_anchor(conn)
    w = resolve_window(conn, period, end)
    daily = stats_by_day(conn, w.prev_start or w.start, w.end)
    cur = window_metrics(conn, w.start, w.end, daily)
    prev = window_metrics(conn, w.prev_start, w.prev_end, daily) if w.prev_start and w.prev_end else None

    def k(key, label, fmt, *, good_up=True, delta_kind="rel", hint=""):
        v = cur[key]
        p = prev.get(key) if prev else None
        return {"key": key, "label": label, "value": v, "previous": p, "fmt": fmt, "good_up": good_up,
                "delta": _delta(v, p, kind=delta_kind) if prev else None, "hint": hint}

    kpis = [
        k("views", "Views", "compact"),
        k("watch_minutes", "Watch time", "hours"),
        k("subs_net", "Subscribers (net)", "signed"),
        k("published", "Shorts published", "int"),
        k("avg_view_pct", "Avg view %", "pct", delta_kind="pts", hint="views-weighted average percentage viewed"),
        k("like_rate", "Like rate", "pct", delta_kind="pts", hint="likes per 100 views"),
        k("spend", "LLM spend", "usd", good_up=False),
        k("cost_per_1k", "Cost / 1K views", "usd", good_up=False),
        k("cost_per_sub", "Cost / subscriber", "usd", good_up=False, hint="LLM spend ÷ net subscribers"),
    ]
    if cur["revenue"] is not None:
        cur["profit"] = cur["revenue"] - cur["spend"]
        if prev is not None and prev["revenue"] is not None:
            prev["profit"] = prev["revenue"] - prev["spend"]
        kpis.insert(1, k("revenue", "Revenue", "usd", hint="YouTube estimated revenue"))
        kpis.insert(2, k("profit", "Revenue − LLM cost", "usd"))
        kpis.insert(3, k("rpm", "RPM", "usd", hint="revenue per 1,000 views"))

    # --- daily series ------------------------------------------------------
    chart_days = _days(w.start, w.end)
    def _net(row: Mapping[str, float]) -> float:
        return row.get("sg", 0) - row.get("sl", 0)
    views_series = [daily.get(d.isoformat(), {}).get("views", 0) if as_of and d <= as_of else None for d in chart_days]
    net_series = [_net(daily.get(d.isoformat(), {})) if as_of and d <= as_of else None for d in chart_days]
    ch = daily_channel(conn, w.start, w.end)
    subs_cum: list[float | None] = [ch.get(d.isoformat()) for d in chart_days]
    if not any(v is not None for v in subs_cum) and any(v for v in net_series if v):
        run = 0.0
        subs_cum = []
        for v in net_series:
            run += v or 0
            subs_cum.append(run if v is not None else None)
    ops_end = db.local_today()
    ops_days = _days(w.start, ops_end) if w.start <= ops_end else [ops_end]
    sp = daily_spend(conn, ops_days[0], ops_days[-1])
    up = daily_uploads(conn, ops_days[0], ops_days[-1])
    caps, up_caps = strategy_caps_by_day(conn, ops_days)
    charts = {
        "views": time_chart("chart-views", chart_days, [{"name": "Views", "values": views_series, "slot": 1}],
                            kind="line", fmt="compact", area=True),
        "subs": time_chart("chart-subs", chart_days, [{"name": "Subscribers", "values": subs_cum, "slot": 1}],
                           kind="line", fmt="compact"),
        "subs_net": time_chart("chart-subs-net", chart_days, [{"name": "Net subscribers", "values": net_series, "slot": 1}],
                               kind="bars", fmt="int", height=160),
        "uploads": time_chart("chart-uploads", ops_days,
                              [{"name": "Uploads", "values": [int(up.get(d, {}).get("n", 0)) for d in ops_days], "slot": 1}],
                              kind="bars", fmt="int", refs=[{"name": "Daily cap", "values": up_caps}], height=160),
        "spend": time_chart("chart-spend", ops_days,
                            [{"name": "LLM spend", "values": [round(sp.get(d, {}).get("c", 0.0), 6) for d in ops_days],
                              "slot": 1}],
                            kind="bars", fmt="usd", refs=[{"name": "Daily cap", "values": caps}], height=160),
    }
    # sparklines for a few tiles (daily values over the window)
    spark = {
        "views": sparkline([v for v in views_series]),
        "subs_net": sparkline(net_series),
        "spend": sparkline([sp.get(d, {}).get("c", 0.0) for d in chart_days if d <= ops_end]),
    }
    for kp in kpis:
        kp["spark"] = spark.get(kp["key"])

    pending = db.list_decisions("pending", conn=conn)
    strat, strat_err = _load_strategy()
    exp = None
    if strat and strat.get("experiment"):
        e = dict(strat["experiment"])
        today = db.local_today().isoformat()
        e["status"] = "running" if e["start"] <= today <= e["end"] else ("finished" if e["end"] < today else "scheduled")
        facts = published_clip_facts(conn, e["id"])
        e["results"] = experiment_results(facts, e, as_of or db.local_today())
        exp = e
    top = clip_rows(conn, {"published_from": _day_bounds(w.start, w.end)[0], "published_to": _day_bounds(w.start, w.end)[1]},
                    sort="views", limit=5)
    counts = status_counts(conn)
    health = pipeline_health(conn)
    return {
        "window": w, "as_of": as_of, "kpis": kpis, "current": cur, "previous": prev, "charts": charts,
        "today": today_status(), "pending": pending, "health": health, "failures_24h": failures_24h(conn),
        "experiment": exp, "strategy": strat, "strategy_error": strat_err, "top_clips": top, "counts": counts,
        "recent_events": db.list_events(limit=8, conn=conn),
        "ready_to_upload": counts["clips"].get("rendered", 0) + counts["clips"].get("scheduled", 0),
        "is_empty": not any(counts["sources"].values()) and not counts["clips"],
    }


def summary_json(conn: sqlite3.Connection, period: str | None) -> dict[str, Any]:
    d = overview_data(conn, period)
    return {
        "generated_at": db.now_iso(),
        "timezone": get_settings().timezone,
        "demo": db.kv_get(DEMO_KV_KEY) is not None,
        "dry_run": get_settings().dry_run,
        "period": d["window"].to_dict() | {"analytics_through": d["as_of"].isoformat() if d["as_of"] else None},
        "kpis": {k["key"]: {"value": k["value"], "previous": k["previous"],
                            "delta_pct": (k["delta"] or {}).get("value") if k["key"] != "avg_view_pct" else None,
                            "delta_pts": (k["delta"] or {}).get("value") if k["key"] == "avg_view_pct" else None}
                 for k in d["kpis"]},
        "today": d["today"],
        "pending_decisions": [{"id": x.id, "kind": x.kind, "summary": x.summary, "requested_at": x.requested_at,
                               "expires_at": x.expires_at} for x in d["pending"]],
        "pipeline": [{k: h[k] for k in ("module", "status", "status_text", "last_success", "last_error",
                                        "errors_24h", "warnings_24h")} for h in d["health"]],
        "failures_24h": d["failures_24h"],
        "experiment": ({k: v for k, v in d["experiment"].items() if k != "results"}
                       | {"results": d["experiment"]["results"]}) if d["experiment"] else None,
        "counts": d["counts"],
        "recent_events": [e.to_dict() for e in d["recent_events"]],
    }


INSIGHT_DIMENSIONS = [
    ("topic", "Topic (search query)"), ("source_type", "Source type"), ("length", "Clip length"),
    ("slot", "Posting time (local)"), ("dow", "Day of week (local)"), ("hook", "Hook form (derived from hook text)"),
    ("score", "Haiku score"), ("channel", "Publisher"),
]
INSIGHT_METRICS = {"views_7d": ("Median 7-day views", "compact"), "pct": ("Avg view %", "pct"),
                   "subs_per_1k": ("Subscribers per 1K views", "dec1")}


def _dim_value(f: Mapping[str, Any], dim: str) -> str:
    if dim == "topic":
        return f["topic"] or "(none)"
    if dim == "source_type":
        return (f["source_type"] or "(none)").replace("_", " ")
    if dim == "length":
        return length_bucket(f["length"])
    if dim == "slot":
        loc = f["pub_local"]
        if not loc:
            return "Unknown"
        h = loc.hour
        return f"{h % 12 or 12} {'AM' if h < 12 else 'PM'}"
    if dim == "dow":
        return _DOW[f["pub_local"].weekday()] if f["pub_local"] else "Unknown"
    if dim == "hook":
        return hook_form(f["hook_text"])
    if dim == "score":
        return score_bucket(f["haiku_score"])
    if dim == "channel":
        return f["channel"] or "(unknown)"
    raise KeyError(dim)


def _group_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    views = sum(r["views"] for r in rows)
    pct = [r["pct"] for r in rows if r["pct"] is not None]
    return {"n": len(rows), "views_7d": _median([r["views_7d"] for r in rows]), "pct": _mean(pct),
            "subs_per_1k": (sum(r["subs"] for r in rows) / views * 1000) if views else None,
            "views_total": views}


def insights_data(conn: sqlite3.Connection, period: str | None, metric: str) -> dict[str, Any]:
    end, as_of = overview_anchor(conn)
    w = resolve_window(conn, period, end, default="all")
    metric = metric if metric in INSIGHT_METRICS else "views_7d"
    facts = published_clip_facts(conn)
    in_window = [f for f in facts if f["pub_day"] and w.start <= f["pub_day"] <= w.end]
    cutoff = (as_of or end) - timedelta(days=MATURE_DAYS - 1)
    mature = [f for f in in_window if f["pub_day"] <= cutoff]
    overall = _group_stats(mature)
    label, fmt = INSIGHT_METRICS[metric]
    dims = []
    for dim, title in INSIGHT_DIMENSIONS:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for f in mature:
            groups[_dim_value(f, dim)].append(f)
        rows = []
        for name, items in groups.items():
            g = _group_stats(items)
            g.update(name=name, low_n=g["n"] < MIN_GROUP_N)
            rows.append(g)
        if dim == "length":
            rows.sort(key=lambda r: _LENGTH_ORDER.index(r["name"]) if r["name"] in _LENGTH_ORDER else 99)
        elif dim == "score":
            rows.sort(key=lambda r: _SCORE_ORDER.index(r["name"]) if r["name"] in _SCORE_ORDER else 99)
        elif dim == "dow":
            rows.sort(key=lambda r: _DOW.index(r["name"]) if r["name"] in _DOW else 99)
        elif dim == "slot":
            def hour_key(r):
                m = re.match(r"(\d+) (AM|PM)", r["name"])
                return (int(m.group(1)) % 12 + (12 if m.group(2) == "PM" else 0)) if m else 99
            rows.sort(key=hour_key)
        else:
            rows.sort(key=lambda r: (r["low_n"], -(r[metric] or 0)))
            if len(rows) > 12:
                rows = rows[:12]
        dims.append({"key": dim, "title": title, "bars": bar_rows(rows, value_key=metric, fmt=fmt,
                                                                   ref=overall[metric], ref_label="all clips"),
                     "groups": len(groups)})
    pts = [f for f in mature if f["haiku_score"] is not None]
    corr = spearman([f["haiku_score"] for f in pts], [f["views_7d"] for f in pts])
    corr_pct = spearman([f["haiku_score"] for f in pts if f["pct"] is not None],
                        [f["pct"] for f in pts if f["pct"] is not None])
    scatter = {"id": "chart-score-views", "type": "scatter", "height": 260, "fmt": "compact",
               "xLabel": "Haiku score", "yLabel": f"{MATURE_DAYS}-day views (log scale)", "logY": True,
               "xDomain": [max(1.0, min(f["haiku_score"] for f in pts)) if pts else 1.0, 10],
               "points": [{"x": f["haiku_score"], "y": f["views_7d"], "label": f["title"] or f"Clip {f['id']}",
                           "href": f"/dashboard/clips/{f['id']}", "id": f["id"]} for f in pts],
               "has_data": len(pts) >= 3,
               "table": {"columns": ["Haiku score", "Clips", "Median 7-day views", "Avg view %"],
                         "rows": [[r["name"], str(r["n"]), fmt_compact(r["views_7d"]), fmt_pct(r["pct"])]
                                  for r in next(d for d in dims if d["key"] == "score")["bars"]["rows"]]}}
    return {"window": w, "as_of": as_of, "metric": metric, "metric_label": label, "metric_fmt": fmt,
            "overall": overall, "dims": dims, "n_total": len(in_window), "n_mature": len(mature),
            "n_pending": len(in_window) - len(mature), "corr": corr, "corr_text": describe_correlation(corr),
            "corr_pct": corr_pct, "scatter": scatter, "metrics": INSIGHT_METRICS}


def strategy_diff(old: Mapping[str, Any] | None, new: Mapping[str, Any]) -> list[str]:
    """Human summary of what changed between two strategy versions."""
    if not old:
        return ["Initial strategy"]
    out: list[str] = []
    keys = sorted({*old, *new} - {"version", "updated_at"})
    for k in keys:
        if k.startswith("_"):
            continue
        a, b = old.get(k), new.get(k)
        if a == b:
            continue
        name = k.replace("_", " ")
        if k == "topics":
            qa = {t.get("query") for t in a or [] if isinstance(t, dict)}
            qb = {t.get("query") for t in b or [] if isinstance(t, dict)}
            parts = [f"+{q}" for q in sorted(qb - qa)] + [f"−{q}" for q in sorted(qa - qb)]
            out.append(f"topics: {', '.join(parts)}" if parts else "topics: settings changed")
        elif k == "experiment":
            ia = (a or {}).get("id") if isinstance(a, dict) else None
            ib = (b or {}).get("id") if isinstance(b, dict) else None
            out.append(f"experiment: {ia or 'none'} → {ib or 'none'}" if ia != ib else "experiment: details changed")
        elif isinstance(a, list) and isinstance(b, list) and all(isinstance(x, str) for x in a + b):
            parts = [f"+{x}" for x in b if x not in a] + [f"−{x}" for x in a if x not in b]
            out.append(f"{name}: {', '.join(parts) if parts else 'reordered'}")
        elif isinstance(a, dict) and isinstance(b, dict) and k != "allowlist":
            parts = [f"{sk} {a.get(sk)!s} → {b.get(sk)!s}" for sk in sorted({*a, *b})
                     if a.get(sk) != b.get(sk) and not str(sk).startswith("_")]
            out.append(f"{name}: {'; '.join(parts)}")
        elif isinstance(a, (int, float, type(None))) and isinstance(b, (int, float)):
            out.append(f"{name}: {a} → {b}")
        elif isinstance(a, str) and isinstance(b, str) and len(a) < 40 and len(b) < 40:
            out.append(f"{name}: {a} → {b}")
        else:
            out.append(f"{name} rewritten")
    return out or ["No setting changes (report only)"]


def strategy_history(conn: sqlite3.Connection, limit: int = 60) -> list[dict[str, Any]]:
    versions = db.list_strategy_versions(limit=limit + 1, conn=conn)
    out = []
    for i, v in enumerate(versions[:limit]):
        prev = versions[i + 1].json if i + 1 < len(versions) else None
        dec = db.get_decision(v.decision_id, conn=conn) if v.decision_id else None
        out.append({"version": v.version, "created_at": v.created_at, "report": v.report,
                    "changes": strategy_diff(prev, v.json or {}), "decision": dec, "json": v.json})
    return out


# ===========================================================================
# Routes
# ===========================================================================

def _int_arg(request: Request, name: str, default: int, lo: int = 1, hi: int = 10_000) -> int:
    try:
        return max(lo, min(hi, int(request.query_params.get(name, default))))
    except (TypeError, ValueError):
        return default


@router.get("/static/{path:path}", include_in_schema=False)
def static_file(path: str) -> Response:
    target = (STATIC_DIR / path).resolve()
    if not target.is_file() or not target.is_relative_to(STATIC_DIR.resolve()):
        return Response("not found", status_code=404, media_type="text/plain")
    resp = FileResponse(target)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_form(request: Request) -> Response:
    if _auth_method(request) in ("open", "cookie", "bearer"):
        return RedirectResponse(_safe_next(request.query_params.get("next")), status_code=303)
    return _render(request, "login.html", {"next": _safe_next(request.query_params.get("next")), "error": None},
                   nav="login")


@router.post("/login", include_in_schema=False)
async def login_submit(request: Request) -> Response:
    form = await request.form()
    token = get_settings().dashboard_token
    nxt = _safe_next(str(form.get("next") or ""))
    given = str(form.get("token") or "")
    if not token:
        return RedirectResponse(nxt, status_code=303)
    if _auth_throttled(request):
        return _too_many(request, api=False)
    if not _origin_ok(request) or not hmac.compare_digest(_b(given), _b(token)):
        _record_auth_failure(request)
        return _render(request, "login.html", {"next": nxt, "error": "That token is not right."},
                       status_code=401, nav="login")
    resp = RedirectResponse(nxt, status_code=303)
    _set_session_cookie(resp, request)
    return _security_headers(resp)


@router.post("/logout", include_in_schema=False)
async def logout(request: Request) -> Response:
    resp, method = _guard(request)
    if resp is not None:
        return resp
    form = await request.form()
    if not _csrf_ok(request, method, form):
        return _render(request, "error.html", {"title": "Forbidden", "message": _FLASH["csrf"][1]}, status_code=403)
    r = RedirectResponse("/dashboard/login?msg=loggedout", status_code=303)
    r.delete_cookie(COOKIE_NAME, path="/dashboard")
    return _security_headers(r)


@router.get("", response_class=HTMLResponse, include_in_schema=False)
@router.get("/", response_class=HTMLResponse)
def overview(request: Request) -> Response:
    resp, _ = _guard(request)
    if resp is not None:
        return resp
    with _conn() as c:
        data = overview_data(c, request.query_params.get("period"))
    return _render(request, "overview.html", {"d": data, "periods": PERIOD_LABELS}, nav="overview")


@router.get("/clips", response_class=HTMLResponse)
def clips_page(request: Request) -> Response:
    resp, _ = _guard(request)
    if resp is not None:
        return resp
    qp = request.query_params
    filters = {k: qp.get(k) or None for k in ("status", "topic", "type", "experiment", "q")}
    sort = qp.get("sort") if qp.get("sort") in CLIP_SORTS else "views"
    desc = qp.get("dir", "desc") != "asc"
    page = _int_arg(request, "page", 1)
    with _conn() as c:
        total = clip_count(c, filters)
        pages = max(1, math.ceil(total / PAGE_SIZE))
        page = min(page, pages)
        rows = clip_rows(c, filters, sort=sort, desc=desc, offset=(page - 1) * PAGE_SIZE)
        opts = distinct_values(c)
        any_clips = total > 0 or c.execute("SELECT 1 FROM clips LIMIT 1").fetchone() is not None
    return _render(request, "clips.html", {"rows": rows, "total": total, "page": page, "pages": pages, "sort": sort,
                                           "desc": desc, "filters": filters, "opts": opts, "any_clips": any_clips,
                                           "statuses": ["published", *db.CLIP_STATUSES]}, nav="clips")


@router.get("/clips/{clip_id}", response_class=HTMLResponse)
def clip_page(request: Request, clip_id: int) -> Response:
    resp, _ = _guard(request)
    if resp is not None:
        return resp
    with _conn() as c:
        data = clip_detail(c, clip_id)
    if data is None:
        return _not_found(request, f"Clip {clip_id}")
    return _render(request, "clip.html", data, nav="clips")


def clip_detail(conn: sqlite3.Connection, clip_id: int) -> dict[str, Any] | None:
    clip = db.get_clip(clip_id, conn=conn)
    if clip is None:
        return None
    source = db.get_source(clip.source_id, conn=conn)
    uploads = db.list_uploads(clip_id=clip_id, conn=conn)
    live = next((u for u in uploads if u.status in ("uploaded", "removed")), uploads[0] if uploads else None)
    stats = db.get_stats(live.youtube_video_id, conn=conn) if live and live.youtube_video_id else []
    row = (clip_rows(conn, clip_ids=[clip_id], limit=1) or [None])[0]
    retention = video_retention(conn, live.youtube_video_id, stats) if live and live.youtube_video_id else None
    days = [date.fromisoformat(s.day) for s in stats]
    cum, run = [], 0
    for s in stats:
        run += s.views
        cum.append(run)
    charts = {}
    if stats:
        charts["views"] = time_chart("chart-clip-views", days,
                                     [{"name": "Daily views", "values": [s.views for s in stats], "slot": 1}],
                                     kind="bars", fmt="compact", height=180)
        charts["cum"] = time_chart("chart-clip-cum", days, [{"name": "Total views", "values": cum, "slot": 1}],
                                   kind="line", fmt="compact", height=180, area=True)
    if retention:
        pts = [p for p in retention if isinstance(p, (list, tuple)) and len(p) >= 2]
        charts["retention"] = {
            "id": "chart-retention", "type": "curve", "height": 200, "fmt": "pct", "has_data": bool(pts),
            "x": [p[0] for p in pts], "xFmt": "pct",
            "series": [{"name": "Audience watching", "values": [p[1] * 100 for p in pts], "slot": 1}],
            "refs": [{"name": "100% (everyone who started)", "values": [100.0] * len(pts)}], "area": True,
            "table": {"columns": ["Point in clip", "Audience watching"],
                      "rows": [[fmt_pct(p[0] * 100, 0), fmt_pct(p[1] * 100, 0)] for p in pts]}}
    exp = None
    if clip.experiment_id:
        for v in db.list_strategy_versions(limit=200, conn=conn):
            e = (v.json or {}).get("experiment") or {}
            if e.get("id") == clip.experiment_id:
                exp = e
                break
    sv = db.get_strategy_version(clip.strategy_version, conn=conn) if clip.strategy_version else None
    events = [db.Event.from_row(r) for r in conn.execute(
        "SELECT * FROM events WHERE message LIKE ? OR message LIKE ? ORDER BY ts DESC, id DESC LIMIT 20",
        (f"%clip {clip_id} %", f"%clip {clip_id}")).fetchall()]
    siblings = [x for x in db.list_clips(source_id=clip.source_id, conn=conn) if x.id != clip_id]
    return {"clip": clip, "source": source, "uploads": uploads, "live": live, "stats": stats, "row": row,
            "charts": charts, "experiment": exp, "strategy_version": sv, "costs": clip_costs(conn, clip_id, clip.source_id),
            "events": events, "siblings": siblings, "hook_form": hook_form(clip.hook_text), "retention": retention,
            "totals": {"views": max(sum(s.views for s in stats), getattr(live, "lifetime_views", None) or 0),
                       "analytics_views": sum(s.views for s in stats),
                       "lifetime_views": getattr(live, "lifetime_views", None),
                       "lifetime_fetched_at": getattr(live, "lifetime_fetched_at", None),
                       "likes": max(sum(s.likes for s in stats), getattr(live, "lifetime_likes", None) or 0),
                       "comments": sum(s.comments for s in stats), "shares": sum(s.shares for s in stats),
                       "subs": sum(s.subscribers_gained - s.subscribers_lost for s in stats),
                       "watch_minutes": sum(s.watch_minutes for s in stats)}}


@router.get("/insights", response_class=HTMLResponse)
def insights_page(request: Request) -> Response:
    resp, _ = _guard(request)
    if resp is not None:
        return resp
    with _conn() as c:
        data = insights_data(c, request.query_params.get("period"), request.query_params.get("metric", "views_7d"))
    return _render(request, "insights.html", {"d": data, "periods": PERIOD_LABELS, "min_n": MIN_GROUP_N,
                                              "mature_days": MATURE_DAYS}, nav="insights")


@router.get("/strategy", response_class=HTMLResponse)
def strategy_page(request: Request) -> Response:
    resp, _ = _guard(request)
    if resp is not None:
        return resp
    with _conn() as c:
        strat, err = _load_strategy()
        exps = experiment_catalog(c, strat)
        _, as_of = overview_anchor(c)
        facts = published_clip_facts(c)
        for e in exps:
            e["results"] = experiment_results(facts, e, as_of or db.local_today())
        history = strategy_history(c)
    return _render(request, "strategy.html", {"strategy": strat, "strategy_error": err, "experiments": exps,
                                              "history": history, "mature_days": MATURE_DAYS}, nav="strategy")


@router.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request) -> Response:
    resp, _ = _guard(request)
    if resp is not None:
        return resp
    qp = request.query_params
    page = _int_arg(request, "page", 1)
    with _conn() as c:
        w = resolve_window(c, qp.get("period"), db.local_today(), default="28")
        s_iso, e_iso = _day_bounds(w.start, w.end)
        fun = funnel(c, s_iso, e_iso)
        reasons = loss_reasons(c, s_iso, e_iso)
        rows, total = source_rows(c, status=qp.get("status") or None, source_type=qp.get("type") or None,
                                  q=qp.get("q") or None, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
        types = [r[0] for r in c.execute("SELECT DISTINCT source_type FROM sources ORDER BY source_type")]
        published = int(c.execute("SELECT COUNT(*) AS n FROM uploads WHERE status IN ('uploaded','removed') "
                                   "AND dry_run = 0").fetchone()["n"])
    first = fun["stages"][0]["n"] or 1
    for st in fun["stages"]:
        st["pct_of_first"] = st["n"] / first * 100 if fun["stages"][0]["n"] else 0
    clip_first = fun["stages"][4]["n"] or 1
    for st in fun["stages"][4:]:
        st["pct_of_first"] = st["n"] / clip_first * 100 if fun["stages"][4]["n"] else 0
    return _render(request, "sources.html", {
        "w": w, "periods": PERIOD_LABELS, "funnel": fun, "reasons": reasons, "rows": rows, "total": total,
        "page": page, "pages": max(1, math.ceil(total / PAGE_SIZE)), "types": types,
        "statuses": db.SOURCE_STATUSES, "filters": {k: qp.get(k) or "" for k in ("status", "type", "q")},
        "published": published}, nav="sources")


@router.get("/sources/{source_id}", response_class=HTMLResponse)
def source_page(request: Request, source_id: int) -> Response:
    resp, _ = _guard(request)
    if resp is not None:
        return resp
    with _conn() as c:
        src = db.get_source(source_id, conn=c)
        if src is None:
            return _not_found(request, f"Source {source_id}")
        clips = clip_rows(c, clip_ids=[x.id for x in db.list_clips(source_id=source_id, conn=c)], limit=None)
        spend = c.execute("SELECT COALESCE(SUM(cost_usd),0) AS c, COUNT(*) AS n FROM llm_usage WHERE source_id = ?",
                          (source_id,)).fetchone()
    return _render(request, "source.html", {"s": src, "clips": clips, "spend": spend["c"], "calls": spend["n"]},
                   nav="sources")


@router.get("/licences.csv")
def licences_csv(request: Request) -> Response:
    resp, _ = _guard(request, api=True)
    if resp is not None:
        return resp
    include_dry = request.query_params.get("include_dry_run") in ("1", "true", "yes")
    with _conn() as c:
        rows = licence_audit_rows(c, include_dry_run=include_dry)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=LICENCE_CSV_COLUMNS, extrasaction="ignore", lineterminator="\r\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: _csv_safe(r.get(k)) if r.get(k) is not None else "" for k in LICENCE_CSV_COLUMNS})
    fname = f"licence-audit-{db.local_today().isoformat()}.csv"
    out = Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                   headers={"Content-Disposition": f'attachment; filename="{fname}"'})
    return _security_headers(out)


@router.get("/costs", response_class=HTMLResponse)
def costs_page(request: Request) -> Response:
    resp, _ = _guard(request)
    if resp is not None:
        return resp
    with _conn() as c:
        data = costs_data(c, request.query_params.get("period"))
    return _render(request, "costs.html", {"d": data, "periods": PERIOD_LABELS}, nav="costs")


_MODEL_SLOTS = {"haiku": 1, "sonnet": 2}


def costs_data(conn: sqlite3.Connection, period: str | None) -> dict[str, Any]:
    w = resolve_window(conn, period, db.local_today(), default="28")
    s_iso, e_iso = _day_bounds(w.start, w.end)
    days = _days(w.start, w.end)
    by_day = daily_spend(conn, w.start, w.end, by_model=True)
    models = sorted({m for d in by_day.values() for m in d},
                    key=lambda m: (next((v for k, v in _MODEL_SLOTS.items() if k in m), 3), m))
    series = []
    other_vals = [0.0] * len(days)
    for m in models:
        slot = next((v for k, v in _MODEL_SLOTS.items() if k in m), 3)
        vals = [round(by_day.get(d, {}).get(m, 0.0), 6) for d in days]
        if slot == 3:
            other_vals = [a + b for a, b in zip(other_vals, vals)]
        else:
            series.append({"name": m, "values": vals, "slot": slot})
    if any(other_vals):
        series.append({"name": "Other models", "values": other_vals, "slot": 3})
    caps, up_caps = strategy_caps_by_day(conn, days)
    up = daily_uploads(conn, w.start, w.end)
    breakdown = spend_breakdown(conn, s_iso, e_iso)
    purpose_rows = sorted(({"name": p["purpose"] + " · " + p["model"], **p} for p in breakdown["by_purpose"]),
                          key=lambda r: -(r["cost"] or 0))
    cap_hits = budget_cap_hits(conn, s_iso, e_iso)
    over_cap_days = len({db.local_date_of(h["ts"]) for h in cap_hits})
    quota = [int(up.get(d, {}).get("quota", 0)) for d in days]
    return {
        "window": w, "breakdown": breakdown,
        "charts": {
            "spend": time_chart("chart-cost-spend", days, series or [{"name": "LLM spend", "values": [0.0] * len(days),
                                                                      "slot": 1}],
                                kind="bars", fmt="usd", stacked=True, refs=[{"name": "Daily cap", "values": caps}]),
            "uploads": time_chart("chart-cost-uploads", days,
                                  [{"name": "Uploads", "values": [int(up.get(d, {}).get("n", 0)) for d in days], "slot": 1}],
                                  kind="bars", fmt="int", refs=[{"name": "Daily cap", "values": up_caps}], height=160),
        },
        "purpose_bars": bar_rows(purpose_rows, value_key="cost", fmt="usd"),
        "cap_hits": cap_hits, "days_at_cap": over_cap_days,
        "quota": {"max_day": max(quota) if quota else 0, "total": sum(quota), "daily_limit": YOUTUBE_DAILY_QUOTA,
                  "dry_uploads": int(sum(up.get(d, {}).get("dry", 0) for d in days))},
        "today": today_status(),
    }


@router.get("/decisions", response_class=HTMLResponse)
def decisions_page(request: Request) -> Response:
    resp, _ = _guard(request)
    if resp is not None:
        return resp
    qp = request.query_params
    nstatus = qp.get("nstatus") or None
    if nstatus not in db.NOTIFICATION_STATUSES:
        nstatus = None
    with _conn() as c:
        pending = db.list_decisions("pending", conn=c)
        history = [d for d in db.list_decisions(limit=200, conn=c) if d.status != "pending"]
        notes = db.list_notifications(nstatus, limit=200, conn=c)
        if nstatus == "queued":
            notes = list(reversed(notes))
        versions = {}
        for d in history:
            if d.status == "approved":
                sv = db.strategy_version_for_decision(d.id, conn=c)
                if sv is not None:
                    versions[d.id] = sv.version
    return _render(request, "decisions.html", {"pending": pending, "history": history, "notes": notes,
                                               "nstatus": nstatus or "", "nstatuses": db.NOTIFICATION_STATUSES,
                                               "versions": versions}, nav="decisions")


@router.post("/decisions/{decision_id}/{action}")
async def decide(request: Request, decision_id: int, action: str) -> Response:
    if action not in ("approve", "reject"):
        return Response("not found", status_code=404, media_type="text/plain")
    resp, method = _guard(request, api=request.headers.get("authorization") is not None)
    if resp is not None:
        return resp
    form = await request.form()
    nxt = _safe_next(str(form.get("next") or ""), "/dashboard/decisions")
    sep = "&" if "?" in nxt else "?"
    if not _csrf_ok(request, method, form):
        return _render(request, "error.html", {"title": "Forbidden", "message": _FLASH["csrf"][1]}, status_code=403)
    d = db.get_decision(decision_id)
    if d is None:
        return _not_found(request, f"Decision {decision_id}")
    if d.status != "pending":
        return _security_headers(RedirectResponse(f"{nxt}{sep}msg=already", status_code=303))
    approved = action == "approve"
    try:
        import notify  # late import: notify is optional at import time and patched in tests

        applied: bool | None = None
        if hasattr(notify, "decide"):
            out = notify.decide(decision_id, approved, via="dashboard")
            ok, applied = out.resolved, out.applied
        else:
            ok = notify.handle_decision(decision_id, approved, via="dashboard")
    except Exception as exc:  # surface the failure to the owner instead of a 500
        db.log_event("dashboard", f"decision {decision_id} {action} from dashboard failed", level="error",
                     data={"decision_id": decision_id, "error": repr(exc)[:500]})
        return _security_headers(RedirectResponse(f"{nxt}{sep}msg=failed", status_code=303))
    db.log_event("dashboard", f"decision {decision_id} {'approved' if approved else 'rejected'} on dashboard",
                 data={"decision_id": decision_id, "accepted": bool(ok),
                       "client": request.client.host if request.client else None})
    msg = ("approved" if approved else "rejected") if ok else "already"
    if ok and approved and applied is False:
        msg = "unapplied"
    if method == "bearer":
        return _json({"ok": bool(ok), "decision_id": decision_id, "status": msg})
    return _security_headers(RedirectResponse(f"{nxt}{sep}msg={msg}", status_code=303))


@router.get("/events", response_class=HTMLResponse)
def events_page(request: Request) -> Response:
    resp, _ = _guard(request)
    if resp is not None:
        return resp
    qp = request.query_params
    level = qp.get("level") if qp.get("level") in db.EVENT_LEVELS else None
    module = qp.get("module") or None
    q = qp.get("q") or None
    before = None
    if qp.get("before_ts") and qp.get("before_id"):
        try:
            before = (str(qp["before_ts"]), int(qp["before_id"]))
        except ValueError:
            before = None
    limit = 100
    with _conn() as c:
        events = event_page(c, level=level, module=module, q=q, before=before, limit=limit + 1)
        modules = event_modules(c)
    more = len(events) > limit
    events = events[:limit]
    return _render(request, "events.html", {"events": events, "modules": modules, "more": more,
                                            "filters": {"level": level or "", "module": module or "", "q": q or ""},
                                            "levels": db.EVENT_LEVELS, "paged": before is not None}, nav="events")


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

@router.get("/api/summary")
def api_summary(request: Request) -> Response:
    resp, _ = _guard(request, api=True)
    if resp is not None:
        return resp
    with _conn() as c:
        return _json(summary_json(c, request.query_params.get("period")))


def _series_payload(spec: Mapping[str, Any]) -> dict[str, Any]:
    return {k: spec[k] for k in ("x", "series", "refs", "fmt") if k in spec}


@router.get("/api/series")
def api_series(request: Request) -> Response:
    resp, _ = _guard(request, api=True)
    if resp is not None:
        return resp
    with _conn() as c:
        d = overview_data(c, request.query_params.get("period"))
    return _json({"period": d["window"].to_dict(),
                  "analytics_through": d["as_of"].isoformat() if d["as_of"] else None,
                  "series": {k: _series_payload(v) for k, v in d["charts"].items()}})


@router.get("/api/clips")
def api_clips(request: Request) -> Response:
    resp, _ = _guard(request, api=True)
    if resp is not None:
        return resp
    qp = request.query_params
    filters = {k: qp.get(k) or None for k in ("status", "topic", "type", "experiment", "q")}
    sort = qp.get("sort") if qp.get("sort") in CLIP_SORTS else "views"
    limit = _int_arg(request, "limit", PAGE_SIZE, 1, 1000)
    page = _int_arg(request, "page", 1)
    with _conn() as c:
        total = clip_count(c, filters)
        rows = clip_rows(c, filters, sort=sort, desc=qp.get("dir", "desc") != "asc", limit=limit,
                         offset=(page - 1) * limit)
    for r in rows:
        r["display_status"] = r["display_status"][0]
    return _json({"total": total, "page": page, "limit": limit, "clips": rows})


@router.get("/api/clips/{clip_id}")
def api_clip(request: Request, clip_id: int) -> Response:
    resp, _ = _guard(request, api=True)
    if resp is not None:
        return resp
    with _conn() as c:
        d = clip_detail(c, clip_id)
    if d is None:
        return _json({"error": "not found"}, status_code=404)
    return _json({"clip": d["clip"].to_dict() | {"duration_sec": d["clip"].duration_sec},
                  "source": d["source"].to_dict() if d["source"] else None,
                  "uploads": [u.to_dict() for u in d["uploads"]],
                  "daily": [{"day": s.day, "views": s.views, "watch_minutes": s.watch_minutes,
                             "avg_view_pct": s.avg_view_pct, "likes": s.likes,
                             "subscribers_net": s.subscribers_gained - s.subscribers_lost} for s in d["stats"]],
                  "retention": d["retention"],
                  "totals": d["totals"], "cost_usd": d["costs"]["total"]})


@router.get("/api/insights")
def api_insights(request: Request) -> Response:
    resp, _ = _guard(request, api=True)
    if resp is not None:
        return resp
    with _conn() as c:
        d = insights_data(c, request.query_params.get("period"), request.query_params.get("metric", "views_7d"))
    return _json({"period": d["window"].to_dict(), "n_mature": d["n_mature"], "n_pending": d["n_pending"],
                  "overall": d["overall"], "haiku_score_vs_views": d["corr"],
                  "dimensions": {x["key"]: [{k: r[k] for k in ("name", "n", "views_7d", "pct", "subs_per_1k",
                                                                "low_n")} for r in x["bars"]["rows"]]
                                 for x in d["dims"]}})


@router.get("/api/costs")
def api_costs(request: Request) -> Response:
    resp, _ = _guard(request, api=True)
    if resp is not None:
        return resp
    with _conn() as c:
        d = costs_data(c, request.query_params.get("period"))
    return _json({"period": d["window"].to_dict(), "totals": d["breakdown"]["totals"],
                  "by_model": d["breakdown"]["by_model"], "by_purpose": d["breakdown"]["by_purpose"],
                  "daily": {k: _series_payload(v) for k, v in d["charts"].items()},
                  "cap_hits": d["cap_hits"], "today": d["today"]})


__all__ = ["router", "DEMO_KV_KEY", "csrf_token", "spearman", "hook_form", "strategy_diff"]
