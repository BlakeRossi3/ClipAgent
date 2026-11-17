"""Ideas of the day: every idea Cecil puts in front of Blake is kept, not just shown for an hour.

Blake: "I like these ideas of the day that it started doing. Make sure it's saving those."

Cecil began putting an "idea of the day" on the Jarvis page with the morning surprise. Tiles vanish after
an hour (``assistant/tiles.py``), so without this the ideas were lost. Now:

* :func:`save` files an idea in the vault note :data:`NOTE` (one ``## <day>: <title>`` section each, newest
  at the end, searchable like any memory) and in kv :data:`KV_IDEAS` (the Scheduled page's list). The
  same title on the same day is saved once.
* Every way an idea reaches him lands here: the ``idea_save`` tool, an ``IDEA:`` line in the morning
  surprise's result (``assistant/surprise.on_finished``), and - the safety net - any tile whose key or
  title says "idea" (``tiles.show`` calls :func:`from_tile`).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

import db

log = logging.getLogger(__name__)

NOTE = "Agent/Ideas of the Day.md"
KV_IDEAS = "assistant:ideas"
KEEP = 400
TITLE_MAX = 100
TEXT_MAX = 2000
_IDEA_RE = re.compile(r"\bideas?\b", re.I)


def list_ideas(limit: int = 30) -> list[dict[str, Any]]:
    """Newest first (the first call also recovers earlier idea tiles, :func:`backfill`)."""
    try:
        backfill()
    except Exception as exc:
        log.warning("idea backfill failed: %s", exc)
    v = db.kv_get(KV_IDEAS, [])
    rows = v if isinstance(v, list) else []
    return list(reversed(rows))[:limit]


def _clean(text: Any, limit: int) -> str:
    s = "\n".join(" ".join(ln.split()) for ln in str(text or "").replace("\r", "").split("\n")).strip()
    return s[:limit]


def save(title: str, idea: str = "", *, why: str = "", day: str | None = None,
         source: str = "assistant") -> dict[str, Any]:
    """Keep one idea. Returns {saved, title, day, path} (saved False = already kept today)."""
    t = _clean(title, TITLE_MAX).replace("\n", " ")
    body = _clean(idea, TEXT_MAX)
    if not t:
        t = (body.split("\n")[0] or "Idea")[:TITLE_MAX]
    day = day or db.local_today().isoformat()
    rows = db.kv_get(KV_IDEAS, [])
    rows = rows if isinstance(rows, list) else []
    if any(r.get("day") == day and str(r.get("title", "")).lower() == t.lower() for r in rows):
        return {"saved": False, "title": t, "day": day, "path": NOTE, "note": "already kept today"}
    item = {"day": day, "title": t, "idea": body, "why": _clean(why, 500), "source": source, "saved_at": db.now_iso()}
    db.kv_set(KV_IDEAS, (rows + [item])[-KEEP:])
    try:
        from assistant import vault

        text = body + (f"\n\nWhy: {item['why']}" if item["why"] else "") + f"\n\n_Saved {item['saved_at']} ({source})_"
        vault.append_to_note(NOTE, text.strip(), heading=f"{day}: {t}", source="assistant", note_type="ideas",
                             tags=["idea-of-the-day"])
    except Exception as exc:   # the kv copy is enough for the page; say so in the log
        log.warning("idea not written to the vault: %s", exc)
        db.log_event("assistant", "idea kept, but not in the vault", level="warning", data={"error": repr(exc)[:300]})
    db.log_event("assistant", f"idea of the day saved: {t}", data={"day": day, "source": source})
    return {"saved": True, "title": t, "day": day, "path": NOTE}


def looks_like_idea(*texts: Any) -> bool:
    return any(_IDEA_RE.search(str(x or "")) for x in texts)


def from_tile(title: str, body: str = "", *, key: str | None = None, value: str | None = None) -> bool:
    """The safety net: a tile that is an idea (key or title says so) is kept. True when saved."""
    if not looks_like_idea(key, title):
        return False
    try:
        idea = "\n".join(x for x in (value or "", body or "") if x)
        heading = re.sub(r"^\s*ideas?\s+of\s+the\s+day\s*[:\-–—]?\s*", "", title or "", flags=re.I).strip()
        return bool(save(heading or title, idea, source="tile")["saved"])
    except Exception as exc:
        log.warning("idea tile not saved: %s", exc)
        return False


KV_BACKFILLED = "assistant:ideas:backfilled"


def backfill() -> int:
    """Once: keep the idea tiles shown before ideas were saved (tiles stay in the table for a week)."""
    if not db.kv_set_if_absent(KV_BACKFILLED, db.now_iso()):
        return 0
    from assistant import store

    n = 0
    for t in store.list_all_tiles():
        if looks_like_idea(t.tile_key, t.title):
            day = db.local_date_of(t.created_at)
            heading = re.sub(r"^\s*ideas?\s+of\s+the\s+day\s*[:\-–—]?\s*", "", t.title or "", flags=re.I).strip()
            idea = "\n".join(x for x in (t.value or "", t.body or "") if x)
            n += bool(save(heading or t.title, idea, day=str(day), source="tile")["saved"])
    if n:
        db.log_event("assistant", f"{n} earlier idea(s) of the day recovered from tiles")
    return n


def from_text(full: str, *, day: str | None = None, source: str = "surprise") -> bool:
    """An ``IDEA: <title> - <the idea>`` line (the morning surprise's report). True when saved."""
    m = re.search(r"^\s*IDEA(?: OF THE DAY)?:\s*(.+)$", full or "", re.M | re.I)
    if not m:
        return False
    line = m.group(1).strip().strip("*")
    parts = re.split(r"\s+[-–—:]\s+", line, maxsplit=1)
    title, idea = (parts[0], parts[1]) if len(parts) == 2 else (line[:TITLE_MAX], line)
    return bool(save(title, idea, day=day, source=source)["saved"])


def register_tools(register: Callable[..., None]) -> None:
    from assistant.tools import Tool, ToolError, _obj

    def _save(ctx: Any, a: dict[str, Any]) -> Any:
        if not str(a.get("title") or a.get("idea") or "").strip():
            raise ToolError("an idea needs a title or text")
        return save(str(a.get("title") or ""), str(a.get("idea") or ""), why=str(a.get("why") or ""),
                    source=f"{ctx.agent}:{ctx.channel}")

    register(Tool("idea_save", "Keep an idea of the day (or any idea worth keeping that you put in front of him) "
                  "in his vault (Agent/Ideas of the Day.md) and on the Scheduled page. Tiles disappear after an "
                  "hour; saved ideas do not. Call it every time you give him an idea of the day.",
                  _obj({"title": {"type": "string"}, "idea": {"type": "string"}, "why": {"type": "string"}},
                       ["title"]), _save, owner=__name__, side_effects=True), replace=True)
    register(Tool("ideas_list", "The ideas of the day kept so far, newest first.",
                  _obj({"limit": {"type": "integer"}}, []),
                  lambda c, a: {"ideas": list_ideas(int(a.get("limit") or 20))}, owner=__name__), replace=True)
