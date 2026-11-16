"""Initiative: Cecil works on himself while the owner is away, from a backlog, within a share of the plan.

The owner's words: "If I don't say anything all day it should still be doing things ... I should come back
after a day and Cecil says: hey, I built this, this and this - your usage didn't go to waste."

* **Backlog** (``as_backlog``, assistant/store.py): things he said he wants and things Cecil should do
  better. :func:`seed` adds :data:`SEED_LIST` once; :func:`harvest` (job ``assistant_initiative_harvest``
  03:20, and from :func:`tick` after :data:`HARVEST_AFTER_MESSAGES` new messages of his) asks the cheap quick
  model to pull concrete wants/complaints out of his recent messages, plus failed/abandoned capability gaps
  (assistant/capabilities.py), repeatedly failing agent runs and proposed lessons, de-duplicated against every
  title already on the list (also dropped ones, so a removed wish never comes back). Editable on
  ``/dashboard/initiative`` and with the ``backlog_*`` tools.
* **Tick** (job ``assistant_initiative``, every 20 min, :func:`tick`): when :func:`gate` allows, the top open
  item (lowest position, not tried in the last :data:`RETRY_AFTER`, fewer than :data:`MAX_ATTEMPTS` tries)
  becomes ONE background task (``tasks.start(..., self_edit=True)``: a full-power worker session wrapped in
  the self-edit guardrail, so code it changes is tested and committed, or rolled back). :func:`on_finished`
  records what was built, why, tests, what is left and the commits; the item becomes done / open / blocked.
* **Gate**: assistant on, not stopped, ``ASSISTANT_INITIATIVE`` on; he has been quiet for
  ``ASSISTANT_INITIATIVE_IDLE_MIN`` (no inbound message, voice turn or dashboard chat, and no turn in
  progress); no background task or self-edit in flight; and the usage share allows it (:func:`usage_gate`):
  never when ``usage.constrained()`` or a known window is >= :data:`RESERVE` used; per known window
  (5-hour / weekly), the initiative runs that started in it may use at most ``ASSISTANT_INITIATIVE_SHARE``
  of it (each run costs its observed utilisation rise, or :data:`EST_RUN_COST` when no fresh usage event
  was seen since it started); with no window data, at most :data:`FALLBACK_RUNS` per :data:`FALLBACK_SPAN`.
* **Report**: :func:`announcement` - one spoken line in the Jarvis boot briefing, once ("While you were out I
  built X, Y and Z."); :func:`send_digest` - one text per away period (after :data:`DIGEST_AFTER` of quiet,
  only if something was built); the Work log on the page (what/why/tests/commits, Undo per item:
  :func:`undo_item` reverts that item's commits and restarts, refused while anything is busy).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any, Callable

import db
from assistant import store
from assistant.settings import get_assistant_settings

log = logging.getLogger(__name__)

MODULE = "initiative"
AGENT = "initiative"
KV_SEEDED = "initiative:seeded"
KV_RUNS = "initiative:runs"                 # [{id, item_id, title, started, finished, status, windows, end, ...}]
KV_HARVEST = "initiative:harvest"           # {"last_id": int, "at": iso}
KV_LAST_TICK = "initiative:last_tick"       # {"at", "status", "reason"}
KV_REPORTED = "initiative:reported_through"  # finished_at of the newest item already spoken
KV_DIGEST = "initiative:digest:{away}"
RUNS_KEEP = 60
MAX_ATTEMPTS = 3
RETRY_AFTER = timedelta(hours=6)
RESERVE = 0.80                              # never start when a window is this full: the rest is his
FALLBACK_RUNS = 2                           # no usage data known: at most this many runs ...
FALLBACK_SPAN = timedelta(hours=5)          # ... per this span
EST_RUN_COST = {"five_hour": 0.10, "seven_day": 0.02, "seven_day_opus": 0.03, "seven_day_sonnet": 0.02}
WINDOW_LEN = {"five_hour": timedelta(hours=5)}   # the weekly ones: 7 days
HARVEST_AFTER_MESSAGES = 20
HARVEST_MAX_MESSAGES = 120
DIGEST_AFTER = timedelta(hours=4)
STALE_TASK = timedelta(minutes=20)          # beyond the task timeout: a lost run (the app restarted)
MAX_LINE_CHARS = 500
MAX_TOKENS = 1200
SIMILAR = 0.82

#: Things Blake asked for repeatedly in the build chat (source ``seed``), in this order.
SEED_LIST: list[tuple[str, str]] = [
    ("Fast, natural voice replies", "Voice replies fast and natural: never silent while working, judgement-based "
     "narration he can train; shorter turns, fewer tokens."),
    ("A more realistic voice that stays fast", "A more realistic voice that does not slow replies (natural Edge "
     "voices / Kokoro on his GPU); casual tone, 'sir' only now and then."),
    ("Learn skills from the reels he sends", "Watch Instagram reels he sends: work out which AI skills/tools they "
     "show, vet them for safety, install the safe ones."),
    ("Connect to Higgsfield for images and video", "Connect to Higgsfield (official remote MCP at "
     "https://mcp.higgsfield.ai/mcp, sign-in with his Higgsfield account) so Cecil can generate images/video."),
    ("Texting that works end to end", "Texting that works end to end (SMS or WhatsApp via Twilio): diagnose and "
     "fix, then check-ins and summaries by text."),
    ("Phone calls for approvals and urgent news", "Phone calls for approvals and urgent news."),
    ("Post YouTube Shorts for real", "Post YouTube Shorts for real once YouTube is connected (pipeline is in dry "
     "run)."),
    ("Drive his Chrome visibly", "Drive his Chrome visibly (Claude in Chrome) to do things on websites."),
    ("Fill the memory vault from his Claude history", "Fill the memory vault from his Claude chat history "
     "export."),
    ("Desktop modes that prepare his laptop", "Desktop modes (gaming / work) that prepare his laptop."),
    ("A richer Jarvis screen", "A Jarvis screen Cecil can throw things on (maps, charts, media) - make it "
     "richer."),
    ("Keep plan usage low", "Keep usage low: measure what eats it (usage_breakdown) and cut the biggest cost."),
    ("Meals, hours and pay by voice without repeat questions", "Meals, hours and pay tracked by voice without "
     "repeated questions."),
    ("Google Calendar and Gmail", "Google Calendar and Gmail (needs his OAuth when ready)."),
    ("A 'Hey Cecil' wake word", "A 'Hey Cecil' wake word."),
    ("More charts and widgets on the home dashboard", "More charts and useful widgets on the home dashboard."),
]

_STOP = {"a", "an", "the", "to", "of", "and", "or", "for", "in", "on", "with", "my", "me", "i", "it", "be", "is",
         "are", "can", "so", "that", "this", "make", "get", "more", "better", "please", "want", "cecil", "his", "he",
         "you", "your", "should", "would", "could", "by", "at", "from", "up", "do", "does"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _STOP]


def similar(a: str, b: str) -> bool:
    """Same wish in other words? Normalised text ratio, token overlap, or one title inside the other."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return (a or "").strip().lower() == (b or "").strip().lower()
    na, nb = " ".join(wa), " ".join(wb)
    if na == nb or SequenceMatcher(None, na, nb).ratio() >= SIMILAR:
        return True
    sa, sb = set(wa), set(wb)
    overlap = len(sa & sb) / len(sa | sb)
    small = min(len(sa), len(sb))
    return overlap >= 0.6 or (small >= 3 and (sa <= sb or sb <= sa))


def find_duplicate(title: str, items: list[store.BacklogItem] | None = None) -> store.BacklogItem | None:
    """An item already on the backlog (any status, dropped too) that says the same thing."""
    for it in items if items is not None else store.list_backlog(None):
        if similar(title, it.title):
            return it
    return None


def add_item(title: str, detail: str = "", *, source: str = "cecil", priority: int = 0) -> tuple[int, bool]:
    """Add unless a similar item exists. Returns (id, added)."""
    dup = find_duplicate(title)
    if dup is not None:
        return dup.id, False
    iid = store.add_backlog(title, detail=detail, source=source, priority=priority)
    db.log_event(MODULE, f"Backlog: {' '.join(title.split())[:120]}", data={"id": iid, "source": source})
    return iid, True


def seed() -> int:
    """Insert :data:`SEED_LIST` once (never duplicating). Returns how many were added."""
    if db.kv_get(KV_SEEDED):
        return 0
    n = 0
    existing = store.list_backlog(None)
    for title, detail in SEED_LIST:
        if find_duplicate(title, existing) is None:
            store.add_backlog(title, detail=detail, source="seed")
            n += 1
    db.kv_set(KV_SEEDED, db.now_iso())
    if n:
        db.log_event(MODULE, f"backlog seeded with {n} items")
    return n


def last_interaction() -> datetime | None:
    """When he last said something to Cecil (any channel: text, voice turn, dashboard chat)."""
    msgs = store.list_messages(1, direction="in")
    return db.parse_iso(msgs[0].ts) if msgs else None


def _talking_now() -> bool:
    try:
        from assistant import brain

        return brain.running() and brain.get_brain().owner() is not None
    except Exception:
        return False


def live_tasks(now: datetime | None = None) -> list[dict[str, Any]]:
    """Background tasks really running (a row left 'running' by a restart does not count)."""
    from assistant import tasks

    now = now or db.utcnow()
    limit = timedelta(seconds=get_assistant_settings().cc_task_timeout_sec) + STALE_TASK
    out = []
    for t in tasks.running_tasks():
        th = tasks._threads.get(t.get("id"))
        if th is not None:
            if th.is_alive():
                out.append(t)
        elif t.get("started") and now - db.parse_iso(t["started"]) <= limit:
            out.append(t)
    return out


def busy(now: datetime | None = None) -> str:
    """Why nothing may start or be undone now ('' = free)."""
    from assistant import selfedit

    if selfedit.in_flight():
        return "a code change is being tested"
    if db.kv_get(selfedit.KV_PENDING):
        return "the last change is still being checked after its restart"
    running = live_tasks(now)
    if running:
        return f"a background task is running ({str(running[0].get('task', ''))[:60]})"
    if _talking_now():
        return "he is talking to Cecil right now"
    return ""


# ---------------------------------------------------------------------------
# usage share
# ---------------------------------------------------------------------------

def runs() -> list[dict[str, Any]]:
    v = db.kv_get(KV_RUNS, [])
    return v if isinstance(v, list) else []


def _save_run(run: dict[str, Any]) -> None:
    rows = [r for r in runs() if r.get("id") != run["id"]] + [run]
    db.kv_set(KV_RUNS, rows[-RUNS_KEEP:])


def windows() -> dict[str, dict[str, Any]]:
    """Known, current plan windows: {kind: {util, resets_at, seen_at}} (from assistant/usage.py)."""
    from assistant import usage

    out = {}
    for k in usage.WINDOW_KINDS:
        w = usage._window(k)
        if not w or w.get("stale") or w.get("utilization") is None:
            continue
        out[k] = {"util": float(w["utilization"]), "resets_at": w.get("resets_at"), "seen_at": w.get("seen_at")}
    return out


def _window_start(kind: str, w: dict[str, Any], now: datetime) -> datetime:
    length = WINDOW_LEN.get(kind, timedelta(days=7))
    return (db.parse_iso(w["resets_at"]) - length) if w.get("resets_at") else now - length


def run_cost(run: dict[str, Any], kind: str, w: dict[str, Any]) -> float:
    """Share of window ``kind`` this run used: the observed rise when a fresh usage event came after it
    started (same window), else the estimate :data:`EST_RUN_COST`."""
    est = EST_RUN_COST.get(kind, 0.03)
    start = (run.get("windows") or {}).get(kind)
    end = (run.get("end") or {}).get(kind)
    if start and end and start.get("resets_at") == end.get("resets_at") == w.get("resets_at") \
            and end.get("seen_at") and end["seen_at"] > run.get("started", ""):
        return max(0.0, float(end["util"]) - float(start["util"]))
    return est


def share_used(kind: str, w: dict[str, Any], now: datetime | None = None) -> float:
    """How much of window ``kind`` initiative runs that started in it have used (0-1)."""
    now = now or db.utcnow()
    since = _window_start(kind, w, now)
    return round(sum(run_cost(r, kind, w) for r in runs()
                     if r.get("started") and db.parse_iso(r["started"]) >= since), 4)


def _label(kind: str) -> str:
    return {"five_hour": "5-hour", "seven_day": "weekly"}.get(kind, kind.replace("seven_day_", "weekly ").replace("_", " "))


def usage_gate(now: datetime | None = None) -> tuple[bool, str, datetime | None]:
    """(may start?, why not, when it may next be possible if known)."""
    from assistant import usage

    now = now or db.utcnow()
    share = get_assistant_settings().initiative_share
    if share <= 0:
        return False, "the initiative share is set to 0", None
    tight, until = usage.constrained()
    if tight:
        return False, "plan usage is tight", until
    wins = windows()
    if wins:
        for k, w in wins.items():
            reset = db.parse_iso(w["resets_at"]) if w.get("resets_at") else None
            if w["util"] >= RESERVE:
                return False, f"the {_label(k)} window is {round(100 * w['util'])}% used (the rest is kept for him)", reset
            used = share_used(k, w, now)
            if used >= share:
                return (False, f"initiative already used its {round(100 * share)}% share of the {_label(k)} window",
                        reset)
        return True, "", None
    recent = sorted(db.parse_iso(r["started"]) for r in runs()
                    if r.get("started") and now - db.parse_iso(r["started"]) < FALLBACK_SPAN)
    if len(recent) >= FALLBACK_RUNS:
        return (False, f"plan usage not reported yet: at most {FALLBACK_RUNS} runs per {int(FALLBACK_SPAN.total_seconds() // 3600)} hours",
                recent[-FALLBACK_RUNS] + FALLBACK_SPAN)
    return True, "", None


def gate(now: datetime | None = None) -> tuple[bool, str, datetime | None]:
    """May an initiative run start now? (ok, why not, earliest time it might, if known)."""
    from assistant import runtime

    s = get_assistant_settings()
    now = now or db.utcnow()
    if not s.enabled:
        return False, "the assistant is off", None
    if not s.initiative:
        return False, "initiative is off (ASSISTANT_INITIATIVE)", None
    if runtime.is_stopped():
        return False, "Cecil is stopped", None
    last = last_interaction()
    idle_need = timedelta(minutes=s.initiative_idle_min)
    if last is not None and now - last < idle_need:
        return False, f"he was active {int((now - last).total_seconds() // 60)} min ago", last + idle_need
    why = busy(now)
    if why:
        return False, why, None
    return usage_gate(now)


# ---------------------------------------------------------------------------
# the work
# ---------------------------------------------------------------------------

def pick(now: datetime | None = None) -> store.BacklogItem | None:
    """The top open item not tried in the last :data:`RETRY_AFTER` with fewer than :data:`MAX_ATTEMPTS` tries."""
    now = now or db.utcnow()
    for it in store.list_backlog("open"):
        if it.attempts >= MAX_ATTEMPTS:
            continue
        if it.last_attempt_at and now - db.parse_iso(it.last_attempt_at) < RETRY_AFTER:
            continue
        return it
    return None


def brief(item: store.BacklogItem) -> str:
    s = get_assistant_settings()
    owner = s.owner_name or "the owner"
    prev = f"\nLast attempt: {item.result}" if item.result else ""
    left = (item.meta or {}).get("left") if isinstance(item.meta, dict) else None
    if left:
        prev += f"\nLeft over from last time: {left}"
    return (f"<initiative_task item=\"{item.id}\">\n"
            f"You are working on your own initiative while {owner} is away. He set this up himself: when he has been "
            "quiet for a while and plan usage allows, take the top item of your backlog (things he said he wants and "
            "things you should do better) and build it without asking him. He approved this mode: you may change "
            "your own code (this app), skills and agent files, test them and keep them.\n\n"
            f"Backlog item #{item.id}: {item.title}\n{item.detail}\n"
            f"Source: {item.source}. Attempt {item.attempts} of {MAX_ATTEMPTS}.{prev}\n\n"
            "How:\n"
            "1. capability_check first; prefer existing tools, installed skills and trusted catalog skills over new "
            "code.\n"
            "2. Follow the test-driven-development, systematic-debugging and verification-before-completion skills "
            "(skill_read). Keep the change small and focused: one useful slice that works beats a big half-finished "
            "one. If the item is large, build the first useful slice and say what is left.\n"
            "3. Do not git commit, push or switch branches: when you finish, your changes are tested with the full "
            "suite and committed automatically (or rolled back if the tests fail). Every change is undoable from "
            "the Initiative page.\n"
            "4. Never: read or change secrets (keys, tokens, passwords, config.env secret values), the approval "
            "rules, or Agent/Heartbeat.md; spend money; message or call anyone (him included - he hears about it "
            "when he is back); install third-party software or skills (skill_request_install proposes one for his "
            "approval); change anything outside this app and his vault.\n"
            "5. If it needs him (a sign-in, OAuth, a purchase, a decision), do what you can without him and finish "
            "with STATUS: blocked and what you need.\n\n"
            "Finish with exactly these lines:\n"
            "STATUS: done | partial | blocked\n"
            "BUILT: <one short line: what now works, as you would say it to him>\n"
            "WHY: <one line>\n"
            "TESTS: <what you ran and the result>\n"
            "LEFT: <what is left, or none>\n"
            "</initiative_task>")


def parse_report(text: str) -> dict[str, str]:
    out = {}
    for key in ("status", "built", "why", "tests", "left"):
        m = re.search(rf"^\s*\**{key}\**\s*:\s*(.+)$", text or "", re.I | re.M)
        out[key] = " ".join(m.group(1).split()).strip("* ")[:MAX_LINE_CHARS] if m else ""
    st = out["status"].lower()
    out["status"] = next((w for w in ("done", "partial", "blocked") if st.startswith(w)), "")
    if out["left"].lower().rstrip(".") in ("none", "nothing", "n/a", "-"):
        out["left"] = ""
    return out


def start_item(item: store.BacklogItem) -> str | None:
    """Claim ``item`` and start its background task. Returns the task id (None if someone else claimed it)."""
    from assistant import tasks

    if not store.claim_backlog(item.id):
        return None
    item = store.get_backlog(item.id) or item
    run = {"id": "", "item_id": item.id, "title": item.title, "started": db.now_iso(), "finished": None,
           "status": "running", "windows": windows(), "end": None, "commits": []}
    rid = f"{item.id}-{item.attempts}-{run['started']}"
    run["id"] = rid
    _save_run(run)
    tid = tasks.start(f"Initiative: {item.title}", origin="any", brief=brief(item), announce=False,
                      meta={"initiative_id": item.id, "kind": "initiative"}, self_edit=True,
                      on_finish=lambda row, full, status, iid=item.id, r=rid: on_finished(iid, r, row, full, status))
    meta = item.meta if isinstance(item.meta, dict) else {}
    store.update_backlog(item.id, meta={**meta, "task_id": tid, "run_id": rid})
    run["task_id"] = tid
    _save_run(run)
    db.log_event(MODULE, f"Working on: {item.title}"[:200], data={"id": item.id, "task_id": tid,
                                                                  "attempt": item.attempts})
    return tid


def on_finished(item_id: int, run_id: str, row: dict[str, Any], full: str, status: str) -> None:
    """The background task ended: record what was built and the commits; done / open / blocked."""
    item = store.get_backlog(item_id)
    if item is None:
        return
    rep = parse_report(full)
    edit = row.get("edit") if isinstance(row.get("edit"), dict) else {}
    new_commits = list(edit.get("commits") or [])
    if edit.get("commit") and edit["commit"] not in new_commits:
        new_commits.append(edit["commit"])
    commits = list(dict.fromkeys(list(item.commits or []) + new_commits))
    estat = edit.get("status") or "unchanged"
    attempts = item.attempts
    out_of_tries = attempts >= MAX_ATTEMPTS
    summary = rep["built"] or (row.get("summary") or full or "")[:300]
    if status == "budget":
        new, attempts = "open", max(0, attempts - 1)
        summary = "Stopped by the plan limit; I'll pick it up again later."
    elif estat == "rolled_back":
        new = "blocked" if out_of_tries else "open"
        summary = "My change failed the tests, so it was rolled back. " + str(edit.get("summary") or "")[:300]
    elif status != "ok":
        new = "blocked" if out_of_tries else "open"
        summary = f"The run did not finish ({status}). " + (full or "")[:200]
    elif rep["status"] == "blocked":
        new = "blocked"
    elif rep["status"] == "partial":
        new = "blocked" if out_of_tries else "open"
    else:
        new = "done"
    meta = item.meta if isinstance(item.meta, dict) else {}
    meta = {**meta, "built": rep["built"], "why": rep["why"], "tests": rep["tests"], "left": rep["left"],
            "edit": estat, "edit_summary": str(edit.get("summary") or "")[:400], "files": edit.get("files") or [],
            "task_status": status}
    store.update_backlog(item_id, status=new, attempts=attempts, result=summary[:MAX_LINE_CHARS], commits=commits,
                         meta=meta, finished_at=db.now_iso())
    run = next((r for r in runs() if r.get("id") == run_id), None)
    if run is not None:
        run.update(finished=db.now_iso(), status=new, end=windows(), commits=new_commits, task_status=status,
                   edit=estat)
        _save_run(run)
    level = "info" if new == "done" else "warning" if status != "ok" or estat == "rolled_back" else "info"
    verb = {"done": "Built", "blocked": "Blocked", "open": "Worked on"}.get(new, "Worked on")
    db.log_event(MODULE, f"{verb}: {item.title}"[:200], level=level,
                 data={"id": item_id, "status": new, "commits": new_commits, "edit": estat, "built": rep["built"]})


def _reconcile(now: datetime | None = None) -> int:
    """Items left in_progress by a run that is gone (the app restarted mid-task) -> open again."""
    live = {t.get("id") for t in live_tasks(now)}
    n = 0
    for it in store.list_backlog("in_progress"):
        tid = (it.meta or {}).get("task_id") if isinstance(it.meta, dict) else None
        if tid in live:
            continue
        store.update_backlog(it.id, status="blocked" if it.attempts >= MAX_ATTEMPTS else "open",
                             result="Interrupted (the app restarted while I worked on it).", finished_at=db.now_iso())
        n += 1
    return n


def tick(now: datetime | None = None) -> dict[str, Any]:
    """Scheduler job (every 20 min). Returns {status: off|stopped|waiting|empty|started, reason, ...}."""
    from assistant import runtime

    s = get_assistant_settings()
    if not s.enabled or not s.initiative:
        return {"status": "off"}
    if runtime.is_stopped():
        return {"status": "stopped"}
    now = now or db.utcnow()
    seed()
    _reconcile(now)
    for fn in (maybe_harvest, send_digest):
        try:
            fn()
        except Exception as exc:
            log.warning("initiative %s failed: %s", fn.__name__, exc)
    ok, why, _when = gate(now)
    if not ok:
        res = {"status": "waiting", "reason": why}
    else:
        item = pick(now)
        if item is None:
            res = {"status": "empty", "reason": "nothing on the backlog is ready"}
        else:
            tid = start_item(item)
            res = ({"status": "started", "item_id": item.id, "task_id": tid, "title": item.title} if tid
                   else {"status": "waiting", "reason": "the item was taken"})
    db.kv_set(KV_LAST_TICK, {"at": db.to_iso(now), **{k: v for k, v in res.items() if k in ("status", "reason",
                                                                                              "item_id", "title")}})
    return res


# ---------------------------------------------------------------------------
# harvest: wishes from his messages, gaps, failures, lessons
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You keep {name}'s self-improvement backlog for {owner}. Read his recent messages to {name} and extract only "
    "concrete things he WANTS {name} to do, build, connect or get better at, and complaints about how {name} works "
    "(each complaint becomes a fix). Skip one-off requests that were simply answered at the time (weather, a "
    "quick lookup, a timer), chit-chat, and anything vague. Never include secrets. Each item: a short imperative "
    "title (under 12 words) and one or two sentences of detail that quote or paraphrase what he said. Do not "
    "repeat anything already on the backlog. Reply with JSON only: {{\"items\": [{{\"title\": \"...\", "
    "\"detail\": \"...\"}}]}}. An empty list is normal.")


def _harvest_cursor() -> dict[str, Any]:
    v = db.kv_get(KV_HARVEST, {})
    return v if isinstance(v, dict) else {}


def new_owner_messages(limit: int = HARVEST_MAX_MESSAGES) -> list[store.Message]:
    """His messages after the harvest cursor, oldest first (check-in bookkeeping skipped)."""
    last = int(_harvest_cursor().get("last_id") or 0)
    rows = [m for m in store.list_messages(limit, direction="in") if m.id > last]
    return [m for m in reversed(rows) if not (m.kind or "").startswith("checkin") and (m.body or "").strip()]


def maybe_harvest() -> dict[str, Any] | None:
    """From :func:`tick`: harvest early once he has sent :data:`HARVEST_AFTER_MESSAGES` new messages."""
    if len(new_owner_messages()) >= HARVEST_AFTER_MESSAGES:
        return harvest()
    return None


def parse_items(text: str) -> list[dict[str, str]]:
    m = re.search(r"\{.*\}", (text or "").strip(), re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    raw = data.get("items") if isinstance(data, dict) else None
    out = []
    for it in raw if isinstance(raw, list) else []:
        if isinstance(it, dict) and isinstance(it.get("title"), str) and it["title"].strip():
            out.append({"title": " ".join(it["title"].split())[:160],
                        "detail": " ".join(str(it.get("detail") or "").split())[:800]})
    return out


def _request(convo: str, existing: list[str]) -> Any:
    from assistant import runtime

    s = get_assistant_settings()
    system = [SYSTEM_PROMPT.format(name=s.name, owner=s.owner_name or "the owner")]
    user = ("Already on the backlog:\n" + "\n".join(f"- {t}" for t in existing[:80]) + "\n\n"
            + runtime.wrap_untrusted("his messages", convo))
    return runtime.ModelRequest(AGENT, s.quick_model, system, [{"role": "user", "content": user}], [], MAX_TOKENS,
                                "low")


def _deferred_prompt(convo: str) -> str:
    return ("Background backlog pass (not from him directly; do not message him). Read his messages below and add "
            "each concrete thing he wants you to do, build or do better with backlog_add (source owner); skip "
            "one-off requests already answered and anything already on backlog_list.\n\n"
            f"<messages>\n{convo}\n</messages>")


def _from_model(msgs: list[store.Message], result: dict[str, Any]) -> None:
    from assistant import auto_memory, runtime, usage

    if not msgs:
        return
    convo = "\n".join(f"- {' '.join(auto_memory._redact(m.body or '').split())[:MAX_LINE_CHARS]}" for m in msgs)
    if usage.should_defer("initiative_harvest", _deferred_prompt(convo), agent="main") is not None:
        result["deferred"] = True
        return
    existing = [it.title for it in store.list_backlog(None)]
    try:
        text, cost = auto_memory._ask_model(_request(convo, existing), agent=AGENT)
    except runtime.AssistantBudgetExceeded as exc:
        result["error"] = f"budget ({exc.reason})"
        raise _KeepCursor() from None
    except Exception as exc:
        log.warning("backlog harvest model call failed: %s", exc)
        result["error"] = repr(exc)[:200]
        raise _KeepCursor() from None
    result["cost_usd"] = round(cost, 6)
    for it in parse_items(text)[:10]:
        if auto_memory.looks_secret(it["title"] + " " + it["detail"]):
            continue
        _add(it["title"], it["detail"], "owner", result)


class _KeepCursor(Exception):
    """The model call failed: leave the cursor so the next harvest reads these messages again."""


def _add(title: str, detail: str, source: str, result: dict[str, Any]) -> None:
    iid, added = add_item(title, detail, source=source)
    (result["added"] if added else result["duplicates"]).append({"id": iid, "title": title, "source": source})


def harvest() -> dict[str, Any]:
    """Job: new backlog items from his messages (cheap model), failed capability gaps, repeated failures and
    proposed lessons since the last harvest. Returns {status, added, duplicates, ...}."""
    from assistant import runtime

    s = get_assistant_settings()
    result: dict[str, Any] = {"status": "ok", "added": [], "duplicates": []}
    if not s.enabled or not s.initiative:
        return {**result, "status": "off"}
    if runtime.is_stopped():
        return {**result, "status": "stopped"}
    seed()
    cur = _harvest_cursor()
    since = cur.get("at")
    msgs = new_owner_messages()
    try:
        _from_model(msgs, result)
        keep = False
    except _KeepCursor:
        keep = True
    _from_capabilities(since, result)
    _from_failures(since, result)
    _from_lessons(since, result)
    last_id = max([m.id for m in msgs], default=int(cur.get("last_id") or 0))
    db.kv_set(KV_HARVEST, {"last_id": int(cur.get("last_id") or 0) if keep else last_id, "at": db.now_iso()})
    if result["added"]:
        db.log_event(MODULE, f"backlog harvest: {len(result['added'])} new item(s)",
                     data={"added": [a["title"] for a in result["added"]][:10]})
    if keep:
        result["status"] = "error"
    return result


def _from_capabilities(since: str | None, result: dict[str, Any]) -> None:
    try:
        from assistant import capabilities

        entries = capabilities.log()
    except Exception:
        return
    for e in entries:
        if e.get("outcome") in ("failed", "gave_up") and (not since or str(e.get("at") or "") > since):
            need = " ".join(str(e.get("need") or "").split())
            if need:
                _add(f"Capability: {need}"[:160], f"Tried before and {str(e['outcome']).replace('_', ' ')}: "
                     f"{e.get('how') or 'no notes'}", "capability", result)


def _from_failures(since: str | None, result: dict[str, Any]) -> None:
    rows = [r for r in store.list_agent_runs(300, since=since) if r.status == "error"] if since else \
        [r for r in store.list_agent_runs(300) if r.status == "error"]
    by_agent: dict[str, list[Any]] = {}
    for r in rows:
        by_agent.setdefault(r.agent, []).append(r)
    for agent, rs in by_agent.items():
        if len(rs) >= 2:
            sample = "; ".join(" ".join(str(r.summary or r.task or "").split())[:120] for r in rs[:3])
            _add(f"Fix repeated failures of the {agent} agent", f"{len(rs)} failed runs recently. E.g.: {sample}",
                 "failure", result)


def _from_lessons(since: str | None, result: dict[str, Any]) -> None:
    for les in store.list_lessons("proposed", limit=50):
        if since and str(les.ts) <= since:
            continue
        text = " ".join(str(les.lesson or "").split())
        if text:
            _add(f"Lesson: {text}"[:160], f"Area {les.area}. {les.evidence or ''}".strip(), "lesson", result)


# ---------------------------------------------------------------------------
# telling him
# ---------------------------------------------------------------------------

def _short(it: store.BacklogItem) -> str:
    meta = it.meta if isinstance(it.meta, dict) else {}
    text = str(meta.get("built") or "")
    if not text or len(text) > 90:
        text = it.title
    text = text.strip().rstrip(".")
    if len(text) > 1 and text[0].isupper() and not text[1].isupper():
        text = text[0].lower() + text[1:]
    return text


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def announcement() -> str:
    """One casual line, once, for items built since the last time it was said; '' otherwise."""
    reported = db.kv_get(KV_REPORTED)
    since = reported if isinstance(reported, str) else db.to_iso(db.utcnow() - timedelta(days=1))
    items = store.list_backlog_finished(since)
    if not items:
        return ""
    newest = items[0].finished_at or db.now_iso()
    if not db.kv_set_if_absent(f"{KV_REPORTED}:{newest}", db.now_iso()):
        return ""
    db.kv_set(KV_REPORTED, newest)
    names = [_short(it) for it in reversed(items[:3])]
    more = len(items) - len(names)
    tail = f", plus {more} more thing{'s' if more != 1 else ''}" if more > 0 else ""
    return (f"While you were out I built {_join(names)}{tail}. It's all on the Initiative page, with an undo for "
            "each.")


def send_digest(now: datetime | None = None) -> dict[str, Any] | None:
    """Text him once per away period (quiet for :data:`DIGEST_AFTER`) if something was built meanwhile."""
    now = now or db.utcnow()
    last = last_interaction()
    if last is None or now - last < DIGEST_AFTER:
        return None
    items = store.list_backlog_finished(db.to_iso(last))
    if not items:
        return None
    if not db.kv_set_if_absent(KV_DIGEST.format(away=db.to_iso(last)), db.now_iso()):
        return None
    names = [_short(it) for it in reversed(items[:5])]
    more = len(items) - len(names)
    text = (f"While you were out I built: {'; '.join(names)}" + (f"; plus {more} more" if more > 0 else "")
            + ". Details and undo on the Initiative page.")
    from assistant import tools

    try:
        res = tools.send_to_owner(text, kind="initiative", agent="main")
    except tools.ToolError as exc:
        db.log_event(MODULE, "initiative digest not texted", level="warning", data={"error": str(exc)[:200]})
        return {"sent": False, "error": str(exc)}
    db.log_event(MODULE, "initiative digest texted", data={"items": [it.id for it in items]})
    return {"sent": True, **res}


# ---------------------------------------------------------------------------
# undo, status, the page
# ---------------------------------------------------------------------------

def undo_item(item_id: int, *, via: str = "dashboard") -> dict[str, Any]:
    """Revert every commit made for this item (newest first) and restart. Refused while anything is busy."""
    from assistant import selfedit

    it = store.get_backlog(int(item_id))
    if it is None:
        return {"ok": False, "message": "No such item."}
    if it.status == "in_progress":
        return {"ok": False, "message": "I'm still working on that one; undo it once it has finished."}
    why = busy()
    if why:
        return {"ok": False, "message": f"Not now: {why}. Try again in a few minutes."}
    shas = list(it.commits or [])
    if not shas:
        return {"ok": False, "message": "That item has no code changes to undo."}
    res = selfedit.revert_commits(shas, label=it.title)
    if res.get("ok"):
        meta = it.meta if isinstance(it.meta, dict) else {}
        store.update_backlog(it.id, status="blocked", result="Undone by you; reopen it if you want me to try again.",
                             meta={**meta, "undone_at": db.now_iso(), "undone_via": via, "reverted": res["reverted"]})
        db.log_event(MODULE, f"Undone: {it.title}"[:200], level="warning", data={"id": it.id, "via": via})
    return res


def _when(dt: datetime | None, now: datetime) -> str:
    if dt is None:
        return ""
    from assistant import schedule

    return schedule.when_words(dt, now=now)


def status(now: datetime | None = None) -> dict[str, Any]:
    """For the page, the tool and the Scheduled page: gate, share used per window, running item, last runs."""
    s = get_assistant_settings()
    now = now or db.utcnow()
    ok, why, when = gate(now)
    last = last_interaction()
    share = []
    for k, w in windows().items():
        share.append({"window": _label(k), "used_pct": round(100 * share_used(k, w, now)),
                      "share_pct": round(100 * s.initiative_share), "window_used_pct": round(100 * w["util"]),
                      "resets": _when(db.parse_iso(w["resets_at"]), now) if w.get("resets_at") else ""})
    running = store.list_backlog("in_progress")
    counts: dict[str, int] = {}
    for it in store.list_backlog(None):
        counts[it.status] = counts.get(it.status, 0) + 1
    if ok:
        nxt = "at the next check (every 20 min)"
    elif when is not None:
        nxt = _when(when, now)
    else:
        nxt = "when that clears"
    tick_info = db.kv_get(KV_LAST_TICK)
    return {"enabled": s.enabled and s.initiative, "can_start": ok, "waiting_for": why, "next": nxt,
            "idle_min": s.initiative_idle_min, "share_pct": round(100 * s.initiative_share),
            "last_interaction": db.to_iso(last) if last else None,
            "quiet_for": _when(last, now) if last else "never", "share_used": share,
            "usage_known": bool(share), "fallback": f"at most {FALLBACK_RUNS} runs per "
                                                     f"{int(FALLBACK_SPAN.total_seconds() // 3600)} hours",
            "running": [{"id": it.id, "title": it.title} for it in running], "counts": counts,
            "last_tick": tick_info if isinstance(tick_info, dict) else None,
            "runs": [{k: r.get(k) for k in ("item_id", "title", "started", "finished", "status", "commits", "edit")}
                     for r in reversed(runs()[-10:])]}


def item_dict(it: store.BacklogItem) -> dict[str, Any]:
    meta = it.meta if isinstance(it.meta, dict) else {}
    return {"id": it.id, "title": it.title, "detail": it.detail, "source": it.source, "priority": it.priority,
            "position": it.position, "status": it.status, "attempts": it.attempts, "result": it.result or "",
            "commits": list(it.commits or []), "finished_at": it.finished_at, "built": meta.get("built") or "",
            "why": meta.get("why") or "", "tests": meta.get("tests") or "", "left": meta.get("left") or "",
            "edit": meta.get("edit") or "", "undone": bool(meta.get("undone_at")), "files": meta.get("files") or []}


def work_log(limit: int = 40) -> list[dict[str, Any]]:
    """Items worked on (done, or with code changes), most recently finished first."""
    rows = [it for it in store.list_backlog(None) if it.finished_at and (it.status == "done" or it.commits)]
    rows.sort(key=lambda it: it.finished_at or "", reverse=True)
    return [item_dict(it) for it in rows[:limit]]


def snapshot() -> dict[str, Any]:
    """The Initiative page and its JSON twin."""
    seed()
    now = db.utcnow()
    active = store.list_backlog(store.BACKLOG_ACTIVE)
    return {"now": db.to_iso(now), "status": status(now), "backlog": [item_dict(it) for it in active],
            "work_log": work_log(), "dropped": [item_dict(it) for it in store.list_backlog("dropped", limit=50)]}


def schedule_summary() -> dict[str, Any] | None:
    """One line for the Scheduled page."""
    try:
        st = status()
    except Exception as exc:
        log.debug("initiative summary unavailable: %s", exc)
        return None
    if not st["enabled"]:
        return None
    running = st["running"][0]["title"] if st["running"] else ""
    return {"running": running, "next": st["next"], "waiting_for": st["waiting_for"],
            "open": st["counts"].get("open", 0), "done": st["counts"].get("done", 0)}


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

PRIORITIES = {"high": 1, "normal": 0, "low": -1}


def _tool_list(ctx: Any, a: dict[str, Any]) -> Any:
    seed()
    which = str(a.get("status") or "open")
    if which == "all":
        rows = store.list_backlog(None)
    elif which == "active":
        rows = store.list_backlog(store.BACKLOG_ACTIVE)
    else:
        from assistant.tools import ToolError

        if which not in store.BACKLOG_STATUSES:
            raise ToolError(f"status must be all, active or one of {', '.join(store.BACKLOG_STATUSES)}")
        rows = store.list_backlog(which)
    keep = ("id", "title", "detail", "source", "priority", "status", "attempts", "result", "built", "left")
    return {"items": [{k: item_dict(it)[k] for k in keep} for it in rows[:60]],
            "note": "Top of the list is worked on first while he is away (backlog_update move up/down/top)."}


def _tool_add(ctx: Any, a: dict[str, Any]) -> Any:
    from assistant.tools import ToolError

    title = " ".join(str(a.get("title") or "").split())
    if not title:
        raise ToolError("title is required")
    source = str(a.get("source") or "owner")
    if source not in ("owner", "cecil"):
        raise ToolError("source must be owner or cecil")
    iid, added = add_item(title, str(a.get("detail") or ""), source=source,
                          priority=PRIORITIES.get(str(a.get("priority") or "normal"), 0))
    if not added:
        dup = store.get_backlog(iid)
        return {"added": False, "duplicate_of": {"id": iid, "title": dup.title if dup else "", "status":
                                                 dup.status if dup else ""}}
    return {"added": True, "id": iid, "note": "On the backlog; worked on while he is away (Initiative page)."}


def _tool_update(ctx: Any, a: dict[str, Any]) -> Any:
    from assistant.tools import ToolError

    try:
        iid = int(a.get("id"))
    except (TypeError, ValueError):
        raise ToolError("id must be the number from backlog_list") from None
    it = store.get_backlog(iid)
    if it is None:
        raise ToolError(f"no backlog item #{iid}")
    cols: dict[str, Any] = {}
    for k in ("title", "detail"):
        if a.get(k) is not None:
            cols[k] = str(a[k])
    if a.get("priority") is not None:
        cols["priority"] = PRIORITIES.get(str(a["priority"]), 0)
    if a.get("status") is not None:
        st = str(a["status"])
        if st not in ("open", "done", "blocked", "dropped"):
            raise ToolError("status must be open, done, blocked or dropped")
        if it.status == "in_progress":
            raise ToolError("that item is being worked on right now")
        cols["status"] = st
        if st == "open":
            cols["attempts"] = 0
    try:
        if cols:
            store.update_backlog(iid, **cols)
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    move = str(a.get("move") or "")
    if move:
        store.move_backlog(iid, {"up": -1, "down": 1, "top": -10_000, "bottom": 10_000}.get(move, 0))
    return {"updated": iid, "item": item_dict(store.get_backlog(iid))}  # type: ignore[arg-type]


def _tool_status(ctx: Any, a: dict[str, Any]) -> Any:
    return status()


def register_tools(register: Callable[..., None]) -> None:
    from assistant.tools import Tool, _obj

    register(Tool("backlog_list", "Your initiative backlog (his wants + your improvements), in work order. status: "
                  "open (default) | active | all | done | blocked | dropped.",
                  _obj({"status": {"type": "string"}}, []), _tool_list, owner="assistant.initiative"), replace=True)
    register(Tool("backlog_add", "Add a want he stated (or your own idea, source cecil) to the backlog you build "
                  "while he is away. Deduplicated. priority high = top.",
                  _obj({"title": {"type": "string"}, "detail": {"type": "string"},
                        "source": {"type": "string", "enum": ["owner", "cecil"]},
                        "priority": {"type": "string", "enum": ["high", "normal", "low"]}}, ["title"]),
                  _tool_add, owner="assistant.initiative", side_effects=True), replace=True)
    register(Tool("backlog_update", "Change a backlog item: title, detail, priority, status (dropped = he does not "
                  "want it), move up|down|top|bottom.",
                  _obj({"id": {"type": "integer"}, "title": {"type": "string"}, "detail": {"type": "string"},
                        "priority": {"type": "string", "enum": ["high", "normal", "low"]},
                        "status": {"type": "string", "enum": ["open", "done", "blocked", "dropped"]},
                        "move": {"type": "string", "enum": ["up", "down", "top", "bottom"]}}, ["id"]),
                  _tool_update, owner="assistant.initiative", side_effects=True), replace=True)
    register(Tool("initiative_status", "Your own-initiative work: may you start now (why not), usage share used, "
                  "running item, recent runs.", _obj({}, []), _tool_status, owner="assistant.initiative"), replace=True)
