"""Claude plan usage awareness: record limits, report them, and defer work until the window resets.

What headless Claude Code tells us (verified against the installed CLI's SDK, claude_agent_sdk
``RateLimitEvent``): with ``--output-format stream-json --verbose`` the CLI emits
``{"type": "rate_limit_event", "rate_limit_info": {"status": "allowed|allowed_warning|rejected",
"resetsAt": <unix>, "rateLimitType": "five_hour|seven_day|seven_day_opus|seven_day_sonnet|overage",
"utilization": 0.0-1.0, ...}}`` *when the rate-limit state changes* (not necessarily every run).
``claude_code.run_prompt`` hands every such event to :func:`record_events`. When no event has been
seen we fall back to the daily run cap and to the reset time parsed from a "You've hit your ... limit
· resets 4pm" error (``claude_code.parse_reset`` / the backoff).

Deferred work lives in ``as_deferred`` (assistant/store.py): ``after_reset`` rows run a few minutes
after the plan's window resets, timed rows at ``run_after``. :func:`run_due` (scheduler, every
10 min) runs them oldest first and texts (sms) or posts (dashboard) the result.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import db
from assistant import store
from assistant.settings import get_assistant_settings

log = logging.getLogger(__name__)

KV_WINDOW = "assistant:usage:window:{kind}"
WINDOW_KINDS = ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet")
DEFER_THRESHOLD = 0.80        # non-urgent work waits once a window is this full
RESET_GRACE = timedelta(minutes=5)
MAX_PER_RUN = 3               # deferred prompts run per scheduler tick (they cost plan usage too)
# Deferred kinds that are scheduler jobs (module:function), run as such when their time comes.
JOB_FUNCS = {"improve": "assistant.improve:weekly_improvement"}
NON_URGENT = ("history", "reflection", "evals", "reel", "improve", "distill", "prompt")


# -- recording -------------------------------------------------------------------

def record_events(stdout: str) -> int:
    """Store every ``rate_limit_event`` in a stream-json transcript. Returns how many were seen."""
    n = 0
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{") or "rate_limit" not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        info = d.get("rate_limit_info") if isinstance(d, dict) and d.get("type") == "rate_limit_event" else None
        if isinstance(info, dict) and info.get("status"):
            record_window(info)
            n += 1
    return n


def record_window(info: dict[str, Any]) -> None:
    kind = str(info.get("rateLimitType") or "five_hour")
    resets = info.get("resetsAt")
    try:
        resets_iso = db.to_iso(datetime.fromtimestamp(int(resets), UTC)) if resets else None
    except (TypeError, ValueError, OSError):
        resets_iso = None
    util = info.get("utilization")
    try:
        util = None if util is None else max(0.0, min(1.0, float(util)))
    except (TypeError, ValueError):
        util = None
    db.kv_set(KV_WINDOW.format(kind=kind), {"status": str(info["status"]), "utilization": util,
                                             "resets_at": resets_iso, "seen_at": db.now_iso()})
    if info.get("status") == "rejected" and resets_iso:
        from assistant import claude_code

        until = db.parse_iso(resets_iso)
        if until > db.utcnow():
            db.kv_set(claude_code.KV_BACKOFF, {"until": resets_iso, "reason": f"{kind} limit", "set_at": db.now_iso()})


def _window(kind: str) -> dict[str, Any] | None:
    v = db.kv_get(KV_WINDOW.format(kind=kind))
    if not isinstance(v, dict):
        return None
    if v.get("resets_at") and db.parse_iso(v["resets_at"]) <= db.utcnow():
        return {**v, "status": "allowed", "utilization": 0.0, "stale": True}   # window has reset since
    return v


# -- reporting -------------------------------------------------------------------

def next_reset() -> datetime | None:
    """When the binding limit lifts: the backoff (a hit limit), else the fullest window's reset."""
    from assistant import claude_code

    until = claude_code.backoff_until()
    if until is not None:
        return until
    best: tuple[float, datetime] | None = None
    for k in WINDOW_KINDS:
        w = _window(k)
        if w and w.get("resets_at") and not w.get("stale"):
            u = w.get("utilization") or 0.0
            r = db.parse_iso(w["resets_at"])
            if best is None or u > best[0]:
                best = (u, r)
    return best[1] if best else None


def usage_status() -> dict[str, Any]:
    """{windows: {kind: {left_pct|None, status, resets_at, resets_text}}, runs_today, runs_cap,
    limited, limited_until, queued}. ``left_pct`` None = unknown (no event seen yet)."""
    from assistant import claude_code

    s = get_assistant_settings()
    windows = {}
    for k in WINDOW_KINDS:
        w = _window(k)
        if w is None:
            continue
        util = w.get("utilization")
        resets = db.parse_iso(w["resets_at"]) if w.get("resets_at") and not w.get("stale") else None
        windows[k] = {"left_pct": None if util is None else round(100 * (1 - util)), "status": w.get("status"),
                      "resets_at": db.to_iso(resets) if resets else None,
                      "resets_text": claude_code.friendly_time(resets) if resets else ""}
    until = claude_code.backoff_until()
    return {"windows": windows, "known": bool(windows), "runs_today": claude_code.turns_today(),
            "runs_cap": s.max_turns_per_day, "limited": until is not None,
            "limited_until": db.to_iso(until) if until else None,
            "limited_text": claude_code.friendly_time(until) if until else "",
            "queued": len(store.list_deferred("queued"))}


def constrained(threshold: float = DEFER_THRESHOLD) -> tuple[bool, datetime | None]:
    """Should non-urgent work wait? (limit hit, a window >= threshold, or the daily run cap reached)."""
    from assistant import claude_code

    until = claude_code.backoff_until()
    if until is not None:
        return True, until
    for k in WINDOW_KINDS:
        w = _window(k)
        if w and not w.get("stale") and ((w.get("utilization") or 0.0) >= threshold or w.get("status") == "rejected"):
            return True, db.parse_iso(w["resets_at"]) if w.get("resets_at") else None
    s = get_assistant_settings()
    if s.backend == "claude_code" and claude_code.turns_today() >= s.max_turns_per_day:
        tomorrow = datetime.combine(db.local_today() + timedelta(days=1), datetime.min.time(), db.local_tz())
        return True, tomorrow.astimezone(UTC)
    return False, None


def should_defer(kind: str, prompt: str, *, agent: str = "main", channel: str = "system") -> int | None:
    """For non-urgent jobs: if the plan is tight, queue ``prompt`` for after the reset and return the
    deferred id (the caller then skips the work); else None (go ahead)."""
    s = get_assistant_settings()
    if s.backend != "claude_code" or s.fake:
        return None
    tight, _when = constrained()
    if not tight:
        return None
    return defer(prompt, kind=kind, agent=agent, channel=channel, reason="plan usage high")


# -- the queue -------------------------------------------------------------------

def defer(prompt: str, *, kind: str = "prompt", channel: str = "system", agent: str = "main",
          at: datetime | None = None, reason: str = "") -> int:
    did = store.add_deferred(prompt, kind=kind, channel=channel, agent=agent,
                             run_after=db.to_iso(at) if at else None, after_reset=at is None, reason=reason or None)
    db.log_event("assistant", f"deferred {kind} #{did}", data={"channel": channel, "after_reset": at is None,
                                                                "at": db.to_iso(at) if at else None})
    return did


def defer_owner_request(message: str, channel: str) -> str:
    """The plan is limited right now: queue his request and say when it will run (once)."""
    s = get_assistant_settings()
    defer(message, kind="owner_request", channel=channel, reason="plan limit")
    when = next_reset()
    from assistant import claude_code

    w = claude_code.friendly_time(when) if when else "once it resets"
    return f"Your Claude plan is at its limit for now, {s.owner_honorific}; it resets {w}. I'll do this then."


def due(now: datetime | None = None) -> list[store.Deferred]:
    now = now or db.utcnow()
    tight, _ = constrained(threshold=1.01)   # only a real limit blocks after-reset items
    out = []
    for d in store.list_deferred("queued"):
        if d.after_reset:
            if not tight:
                out.append(d)
        elif d.run_after and db.parse_iso(d.run_after) <= now:
            out.append(d)
    return out


def run_due(limit: int = MAX_PER_RUN) -> int:
    """Scheduler job: run due deferred prompts, oldest first. Returns how many ran."""
    from assistant import claude_code, runtime

    s = get_assistant_settings()
    if not s.enabled or runtime.is_stopped():
        return 0
    until = claude_code.backoff_until()
    if until is not None:
        return 0
    n = 0
    for d in due()[:limit]:
        if until is None and n and claude_code.backoff_until() is not None:
            break
        if not store.set_deferred_status(d.id, "running"):
            continue
        if d.kind in JOB_FUNCS:  # a deferred scheduler job: run its plain function, not a prompt
            import importlib

            mod, fn = JOB_FUNCS[d.kind].split(":")
            try:
                getattr(importlib.import_module(mod), fn)()
                store.set_deferred_status(d.id, "done", result="ran")
            except Exception as exc:
                store.set_deferred_status(d.id, "failed", result=repr(exc)[:300])
            n += 1
            continue
        sms = d.channel == "sms"
        task = (f"Earlier request from the owner (queued {d.created_at[:16]}Z because of the Claude plan limit; "
                f"do it now and report the result):\n{d.prompt}" if d.kind == "owner_request" else d.prompt)
        try:
            res = runtime.run_task(d.agent, task, channel=d.channel if d.channel in ("sms", "dashboard") else "system",
                                   deliver=sms, kind="deferred")
        except Exception as exc:
            store.set_deferred_status(d.id, "failed", result=repr(exc)[:300])
            continue
        if res.status == "budget":   # limited again: back in the queue
            with store.transaction() as c:
                c.execute("UPDATE as_deferred SET status = 'queued', started_at = NULL WHERE id = ?", (d.id,))
            break
        mid = res.message_out_id
        if d.channel == "dashboard":
            mid = store.add_message("out", "dashboard", res.text, kind="deferred", agent=d.agent)
        store.set_deferred_status(d.id, "done" if res.status == "ok" else "failed", result=res.text[:500],
                                  message_id=mid)
        n += 1
    return n


def parse_at(at: str | None) -> datetime | None:
    """None/'after_reset' -> None (after reset); 'YYYY-MM-DD HH:MM' local; or ISO-8601."""
    if not at or at.strip().lower() in ("after_reset", "after reset", "reset"):
        return None
    txt = at.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(txt, fmt).replace(tzinfo=db.local_tz()).astimezone(UTC)
        except ValueError:
            pass
    dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
    return (dt if dt.tzinfo else dt.replace(tzinfo=db.local_tz())).astimezone(UTC)


# -- tools ---------------------------------------------------------------------------

def _tool_usage_status(ctx: Any, a: dict[str, Any]) -> Any:
    return usage_status()


def _tool_schedule_prompt(ctx: Any, a: dict[str, Any]) -> Any:
    from assistant.tools import ToolError

    text = str(a.get("text") or "").strip()
    if not text or len(text) > 4000:
        raise ToolError("text must be 1-4000 characters")
    try:
        at = parse_at(a.get("at"))
    except ValueError as exc:
        raise ToolError("at must be 'after_reset', 'YYYY-MM-DD HH:MM' (local) or ISO-8601") from exc
    if at is not None and at <= db.utcnow():
        raise ToolError("that time is in the past")
    channel = a.get("channel") if a.get("channel") in ("sms", "dashboard") else "dashboard"
    did = defer(text, kind="prompt", channel=channel, at=at, reason=f"scheduled by {ctx.agent}")
    when = next_reset() if at is None else at
    from assistant import claude_code

    return {"id": did, "runs": ("after the plan resets" + (f" ({claude_code.friendly_time(when)})" if when else ""))
            if at is None else claude_code.friendly_time(at)}


def register_tools(register: Any) -> None:
    from assistant.tools import Tool, _obj

    register(Tool("usage_status", "How much of the owner's Claude plan is left: 5-hour and weekly windows "
                                  "(left % or unknown), when they reset, runs today vs the daily cap, queued tasks.",
                  _obj({}), _tool_usage_status), replace=True)
    register(Tool("schedule_prompt", "Run a prompt later as yourself: at='after_reset' (just after the Claude plan's "
                                     "usage window resets) or a local time 'YYYY-MM-DD HH:MM'. The result is texted "
                                     "(channel sms) or posted on the dashboard.",
                  _obj({"text": {"type": "string"}, "at": {"type": "string"},
                        "channel": {"type": "string", "enum": ["sms", "dashboard"]}}, ["text"]),
                  _tool_schedule_prompt, side_effects=True), replace=True)
