"""``fast_search``: a quick, date-aware web search for current facts (scores, news, prices, weather).

Claude Code's own WebSearch is a nested server-side search that takes 5-15 s per call and does not
put today's date in the query, so "what's the Ohio State score" took 20-40 s and came back stale
(docs/VOICE_SPEED_RESEARCH.md, item 5). This tool answers in well under a second when a search key
is set:

* ``BRAVE_SEARCH_API_KEY`` -> Brave Search API (``freshness`` = pd / pw / pm), preferred;
* else ``EXA_API_KEY``     -> Exa (``startPublishedDate`` from the freshness window);
* neither                  -> no request at all: the tool hands back a dated query ("<query> latest
  November 14, 2026" for sports/news) and tells the model to use WebSearch with exactly that.

Results: the top five as ``{title, url, snippet, age}``; also put on his Jarvis screen as a list panel
(``show=false`` skips it). Keys come from :func:`config.get_settings` (both are in
``admin.SECRET_KEYS``) and never appear in a result, an error or the log. Tests replace the network
with :func:`set_http`. Importing this module touches nothing.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any, Callable

log = logging.getLogger(__name__)

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
EXA_URL = "https://api.exa.ai/search"
TOP_N = 5
TIMEOUT_SEC = 6.0
FRESHNESS = ("day", "week", "month", "any")
_BRAVE_FRESHNESS = {"day": "pd", "week": "pw", "month": "pm"}
_DAYS = {"day": 1, "week": 7, "month": 31}

#: Sports / news / live-data words: such queries always get "latest" and today's date in the fallback.
CURRENT_RE = re.compile(
    r"\b(score|scores|game|games|match|matches|won|win|wins|beat|beats|lost|vs\.?|versus|playoffs?|standings|"
    r"fixture|fixtures|kickoff|tip-?off|nfl|nba|mlb|nhl|ncaa|premier league|champions league|super bowl|"
    r"world cup|news|headlines?|breaking|latest|today|tonight|yesterday|this week|election|results?|"
    r"stocks?|price|prices|weather|forecast)\b", re.I)

# (method, url, headers, params, json_body, timeout) -> (status, parsed json)
Http = Callable[[str, str, dict[str, str], dict[str, Any] | None, dict[str, Any] | None, float],
                tuple[int, Any]]


def _httpx(method: str, url: str, headers: dict[str, str], params: dict[str, Any] | None,
           body: dict[str, Any] | None, timeout: float) -> tuple[int, Any]:
    import httpx

    r = httpx.request(method, url, headers=headers, params=params, json=body, timeout=timeout)
    try:
        data = r.json()
    except ValueError:
        data = None
    return r.status_code, data


_http: Http = _httpx


def set_http(fn: Http | None) -> None:
    """Tests: ``fn(method, url, headers, params, json, timeout) -> (status, data)``; None = httpx."""
    global _http
    _http = fn or _httpx


class SearchError(Exception):
    pass


def provider() -> str:
    """brave | exa | none (which key is set)."""
    import config

    s = config.get_settings()
    if s.brave_search_api_key:
        return "brave"
    if s.exa_api_key:
        return "exa"
    return "none"


def is_current(query: str) -> bool:
    """Sports, news or other live data (the query needs today's date)."""
    return bool(CURRENT_RE.search(query or ""))


def dated_query(query: str) -> str:
    """Sports/news queries get "latest" and today's local date (unless they already name a date or year)."""
    import db

    q = " ".join((query or "").split())
    if not is_current(q):
        return q
    today = db.local_today()
    label = f"{today:%B} {today.day}, {today.year}"
    extra = []
    if not re.search(r"\blatest\b", q, re.I):
        extra.append("latest")
    if str(today.year) not in q and label.lower() not in q.lower():
        extra.append(label)
    return (q + " " + " ".join(extra)).strip()


def _clean(text: Any, limit: int) -> str:
    t = re.sub(r"<[^>]+>", "", str(text or ""))
    t = " ".join(t.replace("&quot;", '"').replace("&#x27;", "'").replace("&amp;", "&").split())
    return t[:limit]


def _age_from_iso(iso: str | None) -> str:
    import db

    if not iso:
        return ""
    try:
        when = db.parse_iso(str(iso)[:19] + "Z")
    except (ValueError, TypeError):
        return str(iso)[:10]
    secs = (db.utcnow() - when).total_seconds()
    if secs < 0:
        return str(iso)[:10]
    if secs < 3600:
        return f"{max(1, int(secs // 60))} minutes ago"
    if secs < 86400:
        return f"{int(secs // 3600)} hours ago"
    days = int(secs // 86400)
    return "1 day ago" if days == 1 else f"{days} days ago"


def _scrub(text: str, *secrets: str) -> str:
    for k in secrets:
        if k:
            text = text.replace(k, "[redacted]")
    return text


def _brave(query: str, freshness: str, key: str) -> list[dict[str, str]]:
    params: dict[str, Any] = {"q": query, "count": TOP_N, "safesearch": "moderate"}
    if freshness in _BRAVE_FRESHNESS:
        params["freshness"] = _BRAVE_FRESHNESS[freshness]
    status, data = _http("GET", BRAVE_URL, {"Accept": "application/json", "X-Subscription-Token": key}, params,
                         None, TIMEOUT_SEC)
    if status != 200 or not isinstance(data, dict):
        raise SearchError(f"Brave search answered HTTP {status}")
    out = []
    for r in ((data.get("web") or {}).get("results") or [])[:TOP_N]:
        if not isinstance(r, dict):
            continue
        out.append({"title": _clean(r.get("title"), 160), "url": str(r.get("url") or "")[:500],
                    "snippet": _clean(r.get("description") or " ".join(r.get("extra_snippets") or []), 300),
                    "age": _clean(r.get("age") or "", 40) or _age_from_iso(r.get("page_age"))})
    return out


def _exa(query: str, freshness: str, key: str) -> list[dict[str, str]]:
    import db

    body: dict[str, Any] = {"query": query, "numResults": TOP_N, "type": "auto",
                            "contents": {"highlights": {"numSentences": 2}, "text": {"maxCharacters": 400}}}
    if freshness in _DAYS:
        body["startPublishedDate"] = db.to_iso(db.utcnow() - timedelta(days=_DAYS[freshness]))
    status, data = _http("POST", EXA_URL, {"Accept": "application/json", "x-api-key": key}, None, body, TIMEOUT_SEC)
    if status != 200 or not isinstance(data, dict):
        raise SearchError(f"Exa search answered HTTP {status}")
    out = []
    for r in (data.get("results") or [])[:TOP_N]:
        if not isinstance(r, dict):
            continue
        hl = r.get("highlights") or []
        snippet = " ".join(str(h) for h in hl) if hl else r.get("text")
        out.append({"title": _clean(r.get("title"), 160), "url": str(r.get("url") or "")[:500],
                    "snippet": _clean(snippet, 300), "age": _age_from_iso(r.get("publishedDate"))})
    return out


def search(query: str, freshness: str = "day") -> dict[str, Any]:
    """One fast search. ``{"provider", "query", "results": [{title, url, snippet, age}], "note"}``.
    With no key: ``provider "none"``, no results, ``use_web_search`` = the dated query to give WebSearch."""
    import config
    import db

    q = " ".join(str(query or "").split())[:300]
    if not q:
        raise SearchError("fast_search needs a query.")
    fresh = freshness if freshness in FRESHNESS else "day"
    which = provider()
    today = db.local_today().isoformat()
    if which == "none":
        dq = dated_query(q)
        return {"provider": "none", "query": dq, "today": today, "results": [], "use_web_search": dq,
                "note": "No fast search key is set. Use WebSearch with exactly this query (it carries today's "
                        "date), then answer from the newest result."}
    s = config.get_settings()
    key = s.brave_search_api_key if which == "brave" else s.exa_api_key
    sq = q if which == "brave" else dated_query(q)
    try:
        results = _brave(sq, fresh, key) if which == "brave" else _exa(sq, fresh, key)
    except SearchError:
        raise
    except Exception as exc:   # network trouble: a short reason, never the key or the URL with it
        raise SearchError(_scrub(f"{which} search failed: {type(exc).__name__}", key)) from None
    note = "" if results else "Nothing found in that window; try freshness week, or WebSearch for deeper research."
    return {"provider": which, "query": sq, "today": today, "freshness": fresh,
            "results": [{k: _scrub(v, key) for k, v in r.items()} for r in results], "note": note}


def _panel(res: dict[str, Any], query: str) -> bool:
    try:
        from assistant import hud

        items = [{"label": r["title"] or r["url"], "sub": r["snippet"][:120], "tag": r["age"][:20]}
                 for r in res["results"]]
        hud.publish_panel(hud.data_panel({"title": ("Search: " + query)[:40], "items": items,
                                          "note": f"via {res['provider'].capitalize()}, {res['today']}"}))
        return True
    except Exception as exc:  # the answer matters more than the screen
        log.debug("fast_search panel skipped: %s", exc)
        return False


DESCRIPTION = ("Fast web search for CURRENT facts: scores, results, news, prices, weather, anything from today. "
               "Returns the top five results (title, url, snippet, age) in under a second and puts them on his "
               "screen (show=false to skip). freshness: day (default), week, month or any. Use this first for "
               "live facts; use WebSearch only for deeper research. If it says use_web_search, call WebSearch "
               "with exactly that query.")


def register_tools(register: Callable[..., None]) -> None:
    from assistant.tools import Tool, ToolError, _obj

    def run(ctx: Any, a: dict[str, Any]) -> Any:
        try:
            res = search(str(a.get("query") or ""), str(a.get("freshness") or "day"))
        except SearchError as exc:
            raise ToolError(str(exc)) from None
        show = a.get("show", True)
        if res["results"] and str(show).lower() not in ("false", "0", "no"):
            res["on_screen"] = _panel(res, str(a.get("query") or ""))
        return res

    register(Tool("fast_search", DESCRIPTION,
                  _obj({"query": {"type": "string"},
                        "freshness": {"type": "string", "enum": list(FRESHNESS)},
                        "show": {"type": ["boolean", "string"]}}, ["query"]),
                  run, owner="assistant.fast_search", side_effects=False), replace=True)
