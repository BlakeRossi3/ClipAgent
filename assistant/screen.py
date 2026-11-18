"""What is on Blake's Jarvis page right now, and the means to clear it.

Blake: "do you see the issue? it has no awareness of what's on the dashboard." Cecil had put four
video panels ("CLIP 5", "CLIP 4"...) on the Jarvis page with ``show_media``; when Blake asked him to
take them down he could only list *tiles*, so he guessed - the Clips page, then the Reels list - and
was wrong four times. Panels lived only in the event bus, with no tool to see or remove them.

Two sources, combined by :func:`read`:

* **What the page itself reports** (ground truth, like the page reader for Chrome): the Jarvis page
  posts a compact report whenever what it shows changes, and every 30 s while open
  (``POST /voice/api/screen``, :func:`record`): every section (``data-widget``: key, heading, shown or
  hidden/collapsed, a little of its text, whether it is in view), every HUD panel, the tiles, and the
  open overlays (expanded panel, History, Diagnostics). Kept in kv :data:`KV_REPORT`.
* **What Cecil put there** (always known, even with the page closed): the HUD panels in the event bus
  and the live tiles.

Clearing: :func:`clear` takes panels down (one by id or title, or all) through the bus
(``panel_remove`` event: every open page drops them at once) and tiles through ``assistant.tiles``.
Blake can close a panel himself with its x (``POST /voice/api/panel/{id}/dismiss``).

Tools: ``screen_read``, ``screen_clear``. :func:`summary_line` puts one line about the screen into every
spoken turn's context (``assistant.status.turn_context``), so Cecil never has to guess.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Callable

import db
from assistant.textutil import one_line as _s

log = logging.getLogger(__name__)

KV_REPORT = "screen:latest"
FRESH_SEC = 120            # a page report older than this may no longer be what he sees
MAX_SECTIONS = 40
MAX_PANELS = 12
MAX_TILES = 12
TEXT_CHARS = 240




def _b(v: Any) -> bool:
    return bool(v) and str(v).lower() not in ("0", "false", "no")


def record(report: Any) -> dict[str, Any]:
    """Keep the page's report (sanitised and size-capped: it is page-supplied data)."""
    r = report if isinstance(report, dict) else {}
    sections = []
    for x in (r.get("sections") or [])[:MAX_SECTIONS]:
        if isinstance(x, dict) and _s(x.get("key"), 40):
            sections.append({"key": _s(x.get("key"), 40), "title": _s(x.get("title"), 80),
                             "area": _s(x.get("area"), 20), "shown": _b(x.get("shown")),
                             "collapsed": _b(x.get("collapsed")), "in_view": _b(x.get("in_view")),
                             "text": _s(x.get("text"), TEXT_CHARS)})
    panels = []
    for x in (r.get("panels") or [])[:MAX_PANELS]:
        if isinstance(x, dict):
            panels.append({"id": _s(x.get("id"), 40), "kind": _s(x.get("kind"), 20), "title": _s(x.get("title"), 80),
                           "text": _s(x.get("text"), TEXT_CHARS)})
    tiles = [{"title": _s(x.get("title"), 80), "text": _s(x.get("text"), TEXT_CHARS)}
             for x in (r.get("tiles") or [])[:MAX_TILES] if isinstance(x, dict)]
    ov = r.get("overlays") if isinstance(r.get("overlays"), dict) else {}
    out = {"at": db.now_iso(), "page": _s(r.get("page"), 120), "sections": sections, "panels": panels,
           "tiles": tiles, "overlays": {k: _b(ov.get(k)) for k in ("expanded_panel", "history", "diagnostics")},
           "scrolled": _s(r.get("scrolled"), 20)}
    db.kv_set(KV_REPORT, out)
    return out


def latest() -> dict[str, Any] | None:
    v = db.kv_get(KV_REPORT)
    return v if isinstance(v, dict) and v.get("at") else None


def fresh(report: dict[str, Any] | None, now: Any = None) -> bool:
    if not report:
        return False
    now = now or db.utcnow()
    return db.parse_iso(report["at"]) >= now - timedelta(seconds=FRESH_SEC)


def _panel_text(p: dict[str, Any]) -> str:
    bits = [p.get("caption"), p.get("note")]
    if p.get("clip_id"):
        bits.append(f"rendered Short, clip #{p.get('clip_id')}")
    if p.get("items"):
        bits.append("; ".join(_s(i.get("label"), 40) for i in p["items"][:4] if isinstance(i, dict)))
    return _s(" · ".join(str(b) for b in bits if b), TEXT_CHARS)


def panels() -> list[dict[str, Any]]:
    """The HUD panels Cecil has up (newest last). In the server they come from the event bus; in a text or
    dashboard turn's tool process (no bus there) from the page's own fresh report."""
    from assistant import events

    out = []
    for p in events.snapshot().get("panels") or []:
        out.append({"id": p.get("id"), "kind": p.get("kind") or "markup", "title": p.get("title") or "",
                    "hold": p.get("hold") or "turn", "text": _panel_text(p)})
    if not out and events._relay:
        rep = latest()
        if fresh(rep):
            out = [{"id": p.get("id"), "kind": p.get("kind") or "", "title": p.get("title") or "", "hold": "",
                    "text": p.get("text") or ""} for p in reversed(rep.get("panels") or []) if p.get("id")]
    return out


def _tiles() -> list[dict[str, Any]]:
    try:
        from assistant import tiles

        return [{"id": t.id, "title": t.title, "text": _s(" ".join(x for x in (t.value or "", t.body or "") if x), 160),
                 "expires_at": t.expires_at} for t in tiles.live()]
    except Exception as exc:  # the screen view must still load
        log.warning("tiles unreadable: %s", exc)
        return []


def read() -> dict[str, Any]:
    """Everything known about his Jarvis page now (the ``screen_read`` tool)."""
    rep = latest()
    live = fresh(rep)
    out: dict[str, Any] = {
        "panels_put_up_by_you": panels(),
        "tiles": _tiles(),
        "page_open": live,
        "where": ("Panels sit at the top of the right-hand rail (he sees the newest 4; the expand button opens one "
                  "large over the ring). Tiles sit under them. Sections: left rail = Recent chats, Staff at work; "
                  "right rail = Now, Tasks, Reels, countdown, Recently worked on; below the ring = Today in numbers, "
                  "then the cards (Needs you, Your week, The staff, New stuff, Lately, What I know...)."),
    }
    if rep:
        out["page_report"] = rep
        out["page_report_note"] = ("what his page itself said it was showing" + ("" if live else
                                   f" - but that was at {rep['at']}, so it may be out of date"))
    else:
        out["page_report_note"] = "his page has not reported yet (not opened since this was added)"
    return out


def summary_line() -> str:
    """One line for every spoken turn: what Cecil has on his screen."""
    ps, ts = panels(), _tiles()
    if not ps and not ts:
        return ""
    bits = []
    if ps:
        bits.append(f"{len(ps)} panel{'s' if len(ps) != 1 else ''} you put up ("
                    + ", ".join(f"{p['kind']} \"{p['title']}\"" for p in ps[-6:]) + ")")
    if ts:
        bits.append(f"{len(ts)} tile{'s' if len(ts) != 1 else ''} (" + ", ".join(f"\"{t['title']}\"" for t in ts[:6]) + ")")
    return "On his Jarvis screen now: " + "; ".join(bits) + ". screen_read shows everything; screen_clear takes it down."


def clear(target: str = "panels") -> dict[str, Any]:
    """Take things off his screen. ``target``: ``panels`` (all panels), ``tiles``, ``all``, or one panel or
    tile by id or (case-insensitive) title."""
    from assistant import events, tiles

    t = " ".join(str(target or "").split())
    low = t.lower()
    ps = panels()
    removed_panels: list[str] = []
    removed_tiles = 0
    if low in ("panels", "all", "everything"):
        removed_panels = [p["id"] for p in ps]
    if low in ("tiles", "all", "everything"):
        removed_tiles = tiles.remove(by="assistant")
    if low not in ("panels", "tiles", "all", "everything"):
        removed_panels = [p["id"] for p in ps if p["id"] == t or p["title"].lower() == low]
        if not removed_panels:
            for tile in tiles.live():
                if str(tile.id) == t or tile.title.lower() == low:
                    removed_tiles += tiles.remove(tile.id, by="assistant")
    if removed_panels:
        events.publish("panel_remove", ids=removed_panels)
    if not removed_panels and not removed_tiles and low not in ("panels", "tiles", "all", "everything"):
        return {"removed_panels": 0, "removed_tiles": 0,
                "note": f"nothing called {t!r} is up; screen_read lists what is",
                "panels": [p["title"] for p in ps]}
    db.log_event("assistant", "screen cleared", data={"target": t[:80], "panels": len(removed_panels),
                                                       "tiles": removed_tiles})
    return {"removed_panels": len(removed_panels), "removed_tiles": removed_tiles,
            "left": [p["title"] for p in panels()]}


def dismiss(panel_id: str) -> bool:
    """He closed a panel with its x."""
    from assistant import events

    if not any(p["id"] == panel_id for p in panels()):
        return False
    events.publish("panel_remove", ids=[panel_id])
    return True


def register_tools(register: Callable[..., None]) -> None:
    from assistant.tools import Tool, _obj

    register(Tool("screen_read", "What is on his Jarvis page right now: every panel you put up (videos, images, maps, "
                  "lists...), the tiles, and - from the page itself - every section with what it shows, what is "
                  "collapsed or hidden and what is in view. ALWAYS call this before saying anything about what he "
                  "sees on his screen or dashboard; never guess.", _obj({}, []), lambda c, a: read(),
                  owner=__name__), replace=True)
    register(Tool("screen_clear", "Take things off his Jarvis page: target = 'panels' (every panel you put up), "
                  "'tiles', 'all', or one panel/tile by its id or exact title (e.g. 'CLIP 5'). Then tell him what "
                  "went.", _obj({"target": {"type": "string"}}, ["target"]),
                  lambda c, a: clear(str(a.get("target") or "panels")), owner=__name__, side_effects=True),
             replace=True)
