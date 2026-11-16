"""Dynamic tiles: small, short-lived widgets on the Jarvis page (the dashboard home stage).

The owner's brief: tiles "go up on the screen for a set amount of time, maybe an hour. And then
they're going to go bye bye. You're going to take them down. They're not permanent."

* A tile is one glanceable piece of information: a title, an optional big ``value`` ("72°F",
  "$1,240"), a line or two of ``body`` text and a ``kind`` (info | metric | reminder | alert).
* Every tile has a lifetime (default :data:`DEFAULT_MINUTES` = 60, at most :data:`MAX_MINUTES`).
  When it runs out the tile is simply no longer live: the page drops it on its next refresh and the
  browser also removes it the moment its time is up. Nothing has to run to take it down.
* A ``key`` ("weather", "next-meeting") makes a tile replaceable: showing a tile with a key takes
  down the live one with the same key, so an update never stacks duplicates.
* At most :data:`MAX_LIVE` tiles are live; showing one more takes down the oldest.
* The owner can dismiss a tile on the page (``POST /dashboard/tiles/{id}/dismiss``); the assistant
  can take one down early with ``tile_remove``.
* Old rows are pruned whenever a tile is shown (:data:`KEEP_DAYS`).

Storage: ``as_tiles`` (assistant/store.py, migration v8). Tools: ``tile_show``, ``tile_remove``,
``tile_list``. CLI: ``python -m assistant.tiles [list | show TITLE [BODY] [--minutes N] | clear]``.
"""

from __future__ import annotations

import re
import sys
from datetime import timedelta
from typing import Any

import db
from assistant import store

DEFAULT_MINUTES = 60
MIN_MINUTES = 1
MAX_MINUTES = 24 * 60
MAX_LIVE = 6
KEEP_DAYS = 7
TITLE_MAX = 60
BODY_MAX = 280
VALUE_MAX = 24
KEY_MAX = 40
KINDS = store.TILE_KINDS

_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_KEY = re.compile(r"[^a-z0-9_-]+")


class TileError(ValueError):
    """Bad tile input (shown to the model as the tool error)."""


def _line(text: Any, limit: int) -> str:
    s = " ".join(_CTRL.sub(" ", str(text or "")).split())
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _body(text: Any, limit: int = BODY_MAX) -> str:
    lines = [" ".join(_CTRL.sub(" ", ln).split()) for ln in str(text or "").replace("\r", "").split("\n")]
    s = "\n".join(ln for ln in lines if ln)[:limit * 2]
    s = "\n".join(s.split("\n")[:4])
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _key(key: Any) -> str | None:
    k = _KEY.sub("-", str(key or "").strip().lower()).strip("-")[:KEY_MAX]
    return k or None


def _minutes(minutes: Any) -> int:
    if minutes is None:
        return DEFAULT_MINUTES
    try:
        m = int(round(float(minutes)))
    except (TypeError, ValueError):
        raise TileError("minutes must be a number") from None
    return max(MIN_MINUTES, min(MAX_MINUTES, m))


def show(title: str, body: str = "", *, value: str | None = None, kind: str = "info",
         minutes: Any = None, key: str | None = None, source: str = "assistant") -> store.Tile:
    """Put a tile up for ``minutes`` (default an hour). Same ``key`` replaces the live tile."""
    t = _line(title, TITLE_MAX)
    if not t:
        raise TileError("a tile needs a title")
    if kind not in KINDS:
        raise TileError(f"kind must be one of {', '.join(KINDS)}")
    v = _line(value, VALUE_MAX) or None
    b = _body(body)
    k = _key(key)
    now = db.utcnow()
    expires = db.to_iso(now + timedelta(minutes=_minutes(minutes)))
    with store.transaction() as c:
        if k:
            for old in store.list_live_tiles(tile_key=k, conn=c):
                store.remove_tile(old.id, by="replaced", conn=c)
        tid = store.add_tile(t, expires_at=expires, body=b, kind=kind, value=v, tile_key=k, source=source, conn=c)
        live = store.list_live_tiles(limit=MAX_LIVE + 50, conn=c)
        for extra in live[MAX_LIVE:]:
            store.remove_tile(extra.id, by="overflow", conn=c)
        store.prune_tiles(db.to_iso(now - timedelta(days=KEEP_DAYS)), conn=c)
        tile = store.get_tile(tid, conn=c)
    db.log_event("assistant", f"tile up: {t}", data={"tile_id": tid, "expires_at": expires, "key": k})
    assert tile is not None
    return tile


def live() -> list[store.Tile]:
    """Tiles on the page right now, newest first (expired ones are already gone)."""
    return store.list_live_tiles(limit=MAX_LIVE)


def remove(tile_id: int | None = None, *, key: str | None = None, by: str = "assistant") -> int:
    """Take tiles down early: one by id, every live one with ``key``, or (neither) all of them."""
    if tile_id is not None:
        return int(store.remove_tile(int(tile_id), by=by))
    k = _key(key) if key is not None else None
    if key is not None and not k:
        return 0
    n = 0
    for t in store.list_live_tiles(tile_key=k):
        n += int(store.remove_tile(t.id, by=by))
    return n


def _left(expires_at: str, now: Any) -> str:
    secs = max(0, int((db.parse_iso(expires_at) - now).total_seconds()))
    mins = (secs + 59) // 60
    if mins >= 60:
        h, m = divmod(mins, 60)
        return f"{h} h {m} min" if m else f"{h} h"
    return f"{mins} min"


def view() -> list[dict[str, Any]]:
    """What the page template needs (plain dicts; text is escaped by Jinja)."""
    now = db.utcnow()
    return [{"id": t.id, "kind": t.kind, "title": t.title, "body": t.body, "value": t.value,
             "expires_at": t.expires_at, "expires_ms": int(db.parse_iso(t.expires_at).timestamp() * 1000),
             "left": _left(t.expires_at, now)} for t in live()]


def _summary(t: store.Tile) -> dict[str, Any]:
    return {"id": t.id, "title": t.title, "value": t.value, "body": t.body, "kind": t.kind, "key": t.tile_key,
            "expires_at": t.expires_at, "left": _left(t.expires_at, db.utcnow())}


# --- tools ------------------------------------------------------------------------------------

SHOW_DESCRIPTION = (
    "Put a small tile on his Jarvis page for a glanceable fact (weather, a score, a figure, a reminder). It "
    "disappears after `minutes` (default 60). Keep it tiny; `key` (e.g. 'weather') makes a later tile replace it.")


def _tool_show(ctx: Any, a: dict[str, Any]) -> Any:
    from assistant.tools import ToolError

    try:
        t = show(a.get("title", ""), a.get("body", ""), value=a.get("value"), kind=a.get("kind") or "info",
                 minutes=a.get("minutes"), key=a.get("key"), source=f"{getattr(ctx, 'agent', 'assistant')}")
    except TileError as exc:
        raise ToolError(str(exc)) from None
    return {"ok": True, "tile": _summary(t)}


def _tool_remove(ctx: Any, a: dict[str, Any]) -> Any:
    if a.get("all"):
        return {"removed": remove(by="assistant")}
    if a.get("id") is None and not a.get("key"):
        from assistant.tools import ToolError

        raise ToolError("give id, key, or all: true")
    return {"removed": remove(a.get("id"), key=a.get("key"), by="assistant")}


def _tool_list(ctx: Any, a: dict[str, Any]) -> Any:
    return {"live": [_summary(t) for t in live()], "max_live": MAX_LIVE, "default_minutes": DEFAULT_MINUTES}


def register_tools(register: Any) -> None:
    from assistant.tools import Tool, _obj

    register(Tool("tile_show", SHOW_DESCRIPTION, _obj({
        "title": {"type": "string"},
        "value": {"type": "string", "description": "big figure, e.g. '72°F'"},
        "body": {"type": "string"},
        "kind": {"type": "string", "enum": list(KINDS)},
        "minutes": {"type": "integer"},
        "key": {"type": "string"},
    }, ["title"]), _tool_show, owner=__name__, side_effects=True), replace=True)
    register(Tool("tile_remove", "Take Jarvis-page tiles down early: `id`, `key` or `all: true`.",
                  _obj({"id": {"type": "integer"}, "key": {"type": "string"}, "all": {"type": "boolean"}}),
                  _tool_remove, owner=__name__, side_effects=True), replace=True)
    register(Tool("tile_list", "Tiles on the Jarvis page now and time left.",
                  _obj({}), _tool_list, owner=__name__), replace=True)


# --- CLI ----------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="python -m assistant.tiles", description="Dynamic tiles on the Jarvis page")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("list")
    s = sub.add_parser("show")
    s.add_argument("title")
    s.add_argument("body", nargs="?", default="")
    s.add_argument("--value")
    s.add_argument("--kind", default="info", choices=KINDS)
    s.add_argument("--minutes", type=int, default=DEFAULT_MINUTES)
    s.add_argument("--key")
    sub.add_parser("clear")
    args = p.parse_args(argv)
    if args.cmd == "show":
        t = show(args.title, args.body, value=args.value, kind=args.kind, minutes=args.minutes, key=args.key,
                 source="cli")
        print(f"tile {t.id} up until {t.expires_at}")
    elif args.cmd == "clear":
        print(f"removed {remove(by='cli')}")
    else:
        for t in live():
            print(f"{t.id}  {t.title}  {t.value or ''}  until {t.expires_at}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
