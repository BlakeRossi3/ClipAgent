"""The morning surprise: every day (``ASSISTANT_MORNING_SURPRISE``, default 07:40 local) Cecil makes the
owner something cool built around his interests (:data:`THEMES`: sports, food, the gym, coding, technology,
AI, video). No stories, news or 'on this day' pieces - he asked for that (2026-11-17).

* :func:`run_morning` (scheduler job ``assistant_morning_surprise``) picks today's format from
  :data:`KINDS` and interest from :data:`THEMES`, avoiding recent ones (history in kv :data:`KV_HISTORY`),
  and queues it as a scheduled row of kind ``surprise`` that runs at once through the background-task path
  (``assistant/schedule.py``) on the sub-agent model with medium effort. The brief makes the worker read
  his profile notes first so the pick is specific to him. When plan usage is tight (``usage.constrained``) it
  waits for the window to reset instead. Once per local day.
* :func:`on_finished` files the result: a vault note ``Agent/Surprises/<day>.md`` (the worker may write a
  richer one itself), the optional self-contained toy ``<data>/surprises/<day>.html`` (served sandboxed at
  ``/dashboard/surprises/<day>.html``), and :data:`KV_LATEST` for the Scheduled page.
* :func:`announcement` returns one casual line, exactly once, the next time he opens the Jarvis page
  (``voice/web.py`` adds it to the boot briefing) and puts a sticky panel on the screen.
"""

from __future__ import annotations

import logging
import random
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import db
from assistant import store

log = logging.getLogger(__name__)

KV_HISTORY = "assistant:surprise:history"
KV_LATEST = "assistant:surprise:latest"
KV_STARTED = "assistant:surprise:started:{day}"
KV_ANNOUNCED = "assistant:surprise:announced:{day}"
HISTORY_KEEP = 21
AVOID_LAST = 5
FOLDER = "Agent/Surprises"
HTML_DIR = "surprises"
MAX_HTML_BYTES = 400_000

#: (key, what to make). Things to play with or use, never prose to read: he said no stories, no news,
#: no 'on this day' (2026-11-17). :func:`pick_kind` avoids the last few.
KINDS: list[tuple[str, str]] = [
    ("toy", "an interactive HTML toy or simulation he can poke at, as ONE self-contained HTML file"),
    ("game", "a tiny game he can play in the browser in under two minutes, as ONE self-contained HTML file"),
    ("tool", "a genuinely useful interactive calculator or planner he would actually reuse, as ONE "
             "self-contained HTML file"),
    ("quiz", "a sharp ten-question interactive quiz that reveals answers as he goes, as ONE self-contained "
             "HTML file"),
    ("explainer", "a one-screen interactive visual explainer of how something works under the hood, as ONE "
                  "self-contained HTML file"),
    ("chart", "a striking data visual built from real, current numbers you looked up (cite where they came from), "
              "on his screen with show_data and as ONE self-contained HTML file"),
    ("challenge", "a bite-size challenge he can do today (a coding kata, a workout finisher, a cook-off) with a "
                  "self-check, as ONE self-contained HTML file"),
]
KIND_KEYS = tuple(k for k, _ in KINDS)

#: (key, what it means for him). His stated interests (Profile/Preferences.md, 2026-11-17).
THEMES: list[tuple[str, str]] = [
    ("sports", "sports - he's a big NFL fan (New York Giants), follows Ohio State football and the New York Islanders"),
    ("food", "food - he likes good food and modern cafés (e.g. focaccia with egg)"),
    ("gym", "the gym - resistance training and hypertrophy science (Chris Beardsley's effective-reps model; he "
            "wants real evidence, not bro-science)"),
    ("coding", "coding - he works in C# and .NET and likes Svelte; skip the basics"),
    ("technology", "technology - hardware, gadgets that do something clever, how things work (no gadget stories)"),
    ("ai", "AI - agents, Claude, MCP, LLMs; he builds with them himself"),
    ("video", "video - YouTube, Shorts, editing and what makes a clip work (he runs a Shorts channel)"),
]
THEME_KEYS = tuple(k for k, _ in THEMES)
AVOID_THEMES = 3


def configured_time() -> str:
    from assistant.settings import get_assistant_settings

    return get_assistant_settings().morning_surprise


def history() -> list[dict[str, Any]]:
    v = db.kv_get(KV_HISTORY, [])
    return v if isinstance(v, list) else []


def latest() -> dict[str, Any] | None:
    v = db.kv_get(KV_LATEST)
    return v if isinstance(v, dict) else None


def pick_kind(day: date, past: list[dict[str, Any]] | None = None) -> str:
    """A kind not made in the last :data:`AVOID_LAST` surprises; varied but stable for a given day."""
    past = history() if past is None else past
    recent = {h.get("kind") for h in past[-AVOID_LAST:]}
    pool = [k for k in KIND_KEYS if k not in recent] or list(KIND_KEYS)
    return random.Random(day.toordinal()).choice(pool)


def pick_theme(day: date, past: list[dict[str, Any]] | None = None) -> str:
    """One of his interests not used in the last :data:`AVOID_THEMES` surprises; stable for a given day."""
    past = history() if past is None else past
    recent = {h.get("theme") for h in past[-AVOID_THEMES:]}
    pool = [k for k in THEME_KEYS if k not in recent] or list(THEME_KEYS)
    return random.Random(day.toordinal() * 31 + 7).choice(pool)


def html_path(day: date | str) -> Path:
    import config

    return config.get_settings().data_dir / HTML_DIR / f"{day}.html"


def brief(day: date, kind: str, theme: str | None = None) -> str:
    from assistant.settings import get_assistant_settings

    s = get_assistant_settings()
    what = dict(KINDS)[kind]
    theme = theme if theme in THEME_KEYS else THEME_KEYS[0]
    about = dict(THEMES)[theme]
    recent = [f"{h.get('kind')}/{h.get('theme') or '?'}: {h.get('title')}" for h in history()[-8:]]
    html = html_path(day)
    return (f"Make {s.owner_name} something cool built around what he's into. It's {day:%A %d %B}.\n"
            f"Today's interest: {about}.\nToday's format: {what}.\n"
            "First read the vault notes Profile/Preferences.md and Profile/About.md (memory_read) and pick something "
            "specific to him inside that interest - his teams, his training, his stack, the tools he uses, what is "
            "happening this week (look it up if it needs current facts). Generic picks feel like no effort; he "
            "should open it and think 'that's for me'.\n"
            "Hard rules: no stories, no news recaps, no history or 'on this day' pieces, no gadget anecdotes. Make "
            "something he can play with or use. Polish it: real content, good visuals, works first time.\n"
            + ("Recent surprises (do not repeat these): " + "; ".join(recent) + "\n" if recent else "")
            + f"- Write ONE self-contained HTML file to {html} (inline CSS "
            "and JavaScript, no external requests, no network, works offline). It opens from the Scheduled page.\n"
            f"- Save it (or a description and the content) to the vault note {FOLDER}/{day}.md with memory_write.\n"
            "- Put a small preview on his Jarvis screen with display or show_data/show_map (hold: sticky).\n"
            "- Do not message him; he hears about it when he next opens the app.\n"
            "- Also give him an idea of the day (something he could make, try, build or post - a fresh one, not one "
            "from his list in Agent/Ideas of the Day.md): save it with idea_save; if you put it on screen as a tile, "
            "it is kept anyway.\n"
            "Finish with the first line 'TITLE: <a short catchy title>', then the line 'IDEA: <idea title> - <the idea "
            "in a sentence or two>', then two plain sentences on what you made.")


def run_morning() -> str:
    """Scheduler job. Returns started | deferred | off | skipped."""
    from assistant import runtime, schedule, usage
    from assistant.settings import get_assistant_settings

    s = get_assistant_settings()
    if not s.enabled or not s.morning_surprise or runtime.is_stopped():
        return "off"
    day = db.local_today()
    if not db.kv_set_if_absent(KV_STARTED.format(day=day.isoformat()), db.now_iso()):
        return "skipped"
    kind = pick_kind(day)
    theme = pick_theme(day)
    text = brief(day, kind, theme)
    tight, _until = usage.constrained()
    meta = {"day": day.isoformat(), "surprise_kind": kind, "surprise_theme": theme}
    if tight:   # cheap, but optional: wait for the window to reset rather than eat what is left
        from assistant import handoff

        did = schedule.schedule(text, when=handoff.resume_time(), kind="surprise", title=f"Morning surprise ({kind})",
                                by="ClipAgent (morning surprise)", why="plan usage was tight at 7:40", meta=meta)
        db.log_event("assistant", "morning surprise waits for the plan to reset", data={"id": did, "kind": kind})
        return "deferred"
    did = schedule.schedule(text, when=db.utcnow(), kind="surprise", title=f"Morning surprise ({kind})",
                            by="ClipAgent (morning surprise)", why="daily, because he asked for something cool",
                            meta=meta)
    d = store.get_deferred(did)
    if d is not None and schedule.can_start_task() and store.set_deferred_status(did, "running"):
        schedule.start_row(store.get_deferred(did) or d)
    db.log_event("assistant", "morning surprise started", data={"id": did, "kind": kind})
    return "started"


def _title(full: str, fallback: str) -> str:
    m = re.search(r"^\s*TITLE:\s*(.+)$", full or "", re.M | re.I)
    t = (m.group(1) if m else "").strip().strip("*\"' ")
    return " ".join(t.split())[:80] or fallback


def on_finished(d: store.Deferred, row: dict[str, Any], full: str, status: str) -> None:
    """File today's surprise: vault note, html link, history, the 'latest' card."""
    from assistant import vault

    if status != "ok":
        return
    meta = d.meta if isinstance(d.meta, dict) else {}
    day = str(meta.get("day") or db.local_today().isoformat())
    kind = str(meta.get("surprise_kind") or "")
    title = _title(full, f"Something {kind.replace('_', ' ')}" if kind else "Something cool")
    rel = f"{FOLDER}/{day}.md"
    try:   # the idea of the day is kept even if the worker forgot idea_save
        from assistant import ideas

        ideas.from_text(full, day=day)
    except Exception as exc:
        log.warning("surprise idea not kept: %s", exc)
    body = re.sub(r"^\s*TITLE:.*$", "", full or "", count=1, flags=re.M | re.I).strip()
    try:
        if vault.note_exists(rel):
            vault.append_to_note(rel, body or title, heading="What Cecil said about it", source="assistant")
        else:
            vault.write_note(rel, f"# {title}\n\n{body}\n", meta={"type": "surprise", "kind": kind},
                             source="assistant", note_type="surprise")
    except Exception as exc:
        log.warning("surprise note not saved: %s", exc)
    html = html_path(day)
    has_html = html.is_file() and 0 < html.stat().st_size <= MAX_HTML_BYTES
    item = {"day": day, "kind": kind, "theme": str(meta.get("surprise_theme") or ""), "title": title, "path": rel, "summary": (row.get("summary") or "")[:300],
            "html_url": f"/dashboard/surprises/{day}.html" if has_html else None, "made_at": db.now_iso()}
    db.kv_set(KV_HISTORY, (history() + [item])[-HISTORY_KEEP:])
    db.kv_set(KV_LATEST, item)
    db.log_event("assistant", f"morning surprise ready: {title}", data={"kind": kind, "html": has_html})


def announcement() -> str:
    """One casual line, once, for today's (or yesterday's unseen) surprise; '' otherwise."""
    item = latest()
    if not item:
        return ""
    try:
        made = date.fromisoformat(str(item.get("day")))
    except ValueError:
        return ""
    if made < db.local_today() - timedelta(days=1):
        return ""
    if not db.kv_set_if_absent(KV_ANNOUNCED.format(day=made.isoformat()), db.now_iso()):
        return ""
    try:
        from assistant import hud

        hud.publish_panel(hud.data_panel({"title": "MORNING SURPRISE", "hold": "sticky", "accent": "violet",
                                          "items": [{"label": item.get("title"), "sub": item.get("summary") or "",
                                                     "tag": str(item.get("kind") or "").replace("_", " ")}],
                                          "note": "Open it from the Scheduled page."}))
    except Exception as exc:
        log.debug("surprise panel not shown: %s", exc)
    return f"Also, I made you something this morning: {item.get('title')}. It's on the Scheduled page."


def safe_html(day: str) -> bytes | None:
    """The day's HTML toy, if any (``day`` must be YYYY-MM-DD)."""
    try:
        date.fromisoformat(day)
    except ValueError:
        return None
    p = html_path(day)
    if not p.is_file() or p.stat().st_size > MAX_HTML_BYTES:
        return None
    return p.read_bytes()
