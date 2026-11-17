"""Claude plan usage awareness: record limits, report them, and defer work until the window resets.

What headless Claude Code tells us (verified against the installed CLI's SDK, claude_agent_sdk
``RateLimitEvent``): with ``--output-format stream-json --verbose`` the CLI emits
``{"type": "rate_limit_event", "rate_limit_info": {"status": "allowed|allowed_warning|rejected",
"resetsAt": <unix>, "rateLimitType": "five_hour|seven_day|seven_day_opus|seven_day_sonnet|overage",
"utilization": 0.0-1.0, ...}}`` *when the rate-limit state changes* (not necessarily every run).
``claude_code.run_prompt`` hands every such event to :func:`record_events`; the voice brain and its
background workers (``brain.run_worker``) hand theirs to :func:`record_window`. Newer CLIs (2.1.3xx)
also put every general window's reading in each event (``rate_limit_info.unifiedWindows``:
``{five_hour|seven_day: {utilization 0-1, resetsAt <unix>, status?}}``), so one event refreshes both.

Events only come while something talks to Claude, and only when a reading changes, so on their own the
meters went stale whenever the app was idle (the owner: "the Claude usage doesn't seem to be updating").
:func:`poll_plan_usage` (job ``assistant_usage_poll``, every 5 min, zero model tokens) asks the CLI
directly: the SDK control request ``{"subtype": "get_usage", "skip_behaviors": true}`` (what Claude
Code's own /usage dialog reads; verified in the bundled CLI 2.1.323) answers ``{rate_limits_available,
subscription_type, rate_limits: {five_hour|seven_day|seven_day_opus|seven_day_sonnet: {utilization
0-100, resets_at ISO-8601} | null}}``. The CLI marks it experimental; a CLI without it (or an API-key
login) leaves the event readings alone and the failure in kv ``assistant:usage:poll``.
When no reading at all exists we fall back to the daily run cap and to the reset time parsed from a
"You've hit your ... limit · resets 4pm" error (``claude_code.parse_reset`` / the backoff).

Deferred work lives in ``as_deferred`` (assistant/store.py): ``after_reset`` rows run a few minutes
after the plan's window resets, timed rows at ``run_after``. :func:`run_due` (scheduler, every
10 min) runs them oldest first: scheduled tasks (``assistant/schedule.py``) as background tasks, other
prompts through ``runtime.run_task`` (texted or posted on the dashboard). Near the limit it also writes a
usage handoff (``assistant/handoff.py``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import db
from assistant import store
from assistant.settings import get_assistant_settings

log = logging.getLogger(__name__)

KV_WINDOW = "assistant:usage:window:{kind}"
WINDOW_KINDS = ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet")
INFERABLE = ("five_hour", "seven_day")   # the general windows; one's reading bounds the other's (see _infer_siblings)
DEFER_THRESHOLD = 0.80        # non-urgent work waits once a window is this full
HANDOFF_THRESHOLD = 0.90      # work in flight is handed over to the next window (assistant/handoff.py)
RESET_GRACE = timedelta(minutes=5)
MAX_PER_RUN = 3               # deferred prompts run per scheduler tick (they cost plan usage too)
# Deferred kinds that are scheduler jobs (module:function), run as such when their time comes.
JOB_FUNCS = {"improve": "assistant.improve:weekly_improvement"}
NON_URGENT = ("history", "reflection", "evals", "reel", "improve", "distill", "prompt")
KV_POLL = "assistant:usage:poll"   # last get_usage poll: {at, ok, error, available, subscription}
POLL_MIN_GAP = timedelta(minutes=4)
POLL_TIMEOUT_SEC = 45.0
Poller = Callable[[], dict[str, Any]]
_poller: Poller | None = None


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


def _iso_of(v: Any) -> str | None:
    """A reset time as UTC ISO: unix seconds (events) or an ISO-8601 string (the usage endpoint)."""
    if v is None or v == "":
        return None
    try:
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().isdigit()):
            return db.to_iso(datetime.fromtimestamp(int(float(v)), UTC))
        dt = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
        return db.to_iso(dt if dt.tzinfo else dt.replace(tzinfo=UTC))
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _fraction(v: Any, *, percent: bool = False) -> float | None:
    try:
        f = None if v is None else float(v)
    except (TypeError, ValueError):
        return None
    if f is None or f != f:
        return None
    return max(0.0, min(1.0, f / 100.0 if percent else f))


def _store(kind: str, status: str, util: float | None, resets_iso: str | None, source: str) -> None:
    db.kv_set(KV_WINDOW.format(kind=kind), {"status": status, "utilization": util, "resets_at": resets_iso,
                                             "seen_at": db.now_iso(), "source": source})


def record_window(info: dict[str, Any]) -> None:
    """One ``rate_limit_info`` from a rate_limit_event (the binding window, plus ``unifiedWindows``)."""
    kind = str(info.get("rateLimitType") or "five_hour")
    resets_iso = _iso_of(info.get("resetsAt"))
    util = _fraction(info.get("utilization"))
    status = str(info["status"])
    _store(kind, status, util, resets_iso, "event")
    _infer_siblings(kind, status, util)
    peak = util or 0.0
    uw = info.get("unifiedWindows")
    for k, w in (uw.items() if isinstance(uw, dict) else ()):
        if k not in WINDOW_KINDS or not isinstance(w, dict) or (k == kind and util is not None):
            continue
        u = _fraction(w.get("utilization"))
        if u is None:
            continue
        st = str(w.get("status") or (status if k == kind else ("rejected" if u >= 1.0 else "allowed")))
        _store(k, st, u, _iso_of(w.get("resetsAt")), "event")
        peak = max(peak, u)
    _after_reading(kind, status, peak, resets_iso)


def _after_reading(kind: str, status: str, peak: float, resets_iso: str | None) -> None:
    """A limit hit sets the backoff; nearly out writes the handoff for work in flight (plain code)."""
    if status == "rejected" and resets_iso:
        from assistant import claude_code

        until = db.parse_iso(resets_iso)
        if until > db.utcnow():
            db.kv_set(claude_code.KV_BACKOFF, {"until": resets_iso, "reason": f"{kind} limit", "set_at": db.now_iso()})
    if status == "rejected" or peak >= HANDOFF_THRESHOLD:
        try:
            from assistant import handoff

            handoff.on_usage_signal()
        except Exception as exc:
            log.warning("usage handoff failed: %s", exc)


# -- polling (zero tokens) -------------------------------------------------------------

def set_poller(fn: Poller | None) -> None:
    """Tests: ``fn() -> get_usage response dict`` (or raise). None = ask the real CLI."""
    global _poller
    _poller = fn


def _sdk_get_usage() -> dict[str, Any]:
    """Start the CLI (no prompt, no tools, ClipAgent's plan profile), send the ``get_usage`` control
    request, disconnect. No model call happens, so it costs no tokens or plan usage."""
    import asyncio

    from assistant import brain, claude_code

    sdk = brain._sdk()
    kw: dict[str, Any] = dict(env=brain.build_env("plan"), setting_sources=[], strict_mcp_config=True,
                              mcp_servers={}, tools=[], cwd=str(claude_code.work_dir()),
                              stderr=lambda line: log.debug("claude (usage poll): %s", line))
    exe = claude_code.find_claude()
    if exe is not None:
        kw["cli_path"] = str(exe)

    async def go() -> dict[str, Any]:
        client = sdk.ClaudeSDKClient(options=sdk.ClaudeAgentOptions(**kw))
        await asyncio.wait_for(client.connect(), POLL_TIMEOUT_SEC)
        try:
            q = getattr(client, "_query", None)
            if q is None or not hasattr(q, "_send_control_request"):
                raise RuntimeError("this claude_agent_sdk cannot send control requests")
            return await q._send_control_request({"subtype": "get_usage", "skip_behaviors": True},
                                                 timeout=POLL_TIMEOUT_SEC)
        finally:
            try:
                await asyncio.wait_for(client.disconnect(), 10)
            except Exception:
                pass

    return asyncio.run(go())


def record_plan_usage(resp: dict[str, Any]) -> int:
    """Store the windows of a ``get_usage`` answer (utilization in percent). Returns how many."""
    limits = resp.get("rate_limits") if isinstance(resp, dict) else None
    if not isinstance(limits, dict):
        return 0
    n, peak, top = 0, 0.0, ("five_hour", "allowed", None)
    for k in WINDOW_KINDS:
        w = limits.get(k)
        u = _fraction(w.get("utilization"), percent=True) if isinstance(w, dict) else None
        if u is None:
            continue
        status = "rejected" if u >= 1.0 else "allowed"
        resets = _iso_of(w.get("resets_at"))
        _store(k, status, u, resets, "poll")
        n += 1
        if u >= peak:
            peak, top = u, (k, status, resets)
    if n:
        _after_reading(top[0], top[1], peak, top[2])
    return n


def poll_plan_usage(*, force: bool = False) -> dict[str, Any]:
    """Scheduler job (every 5 min): refresh every plan window straight from Claude Code, zero tokens.
    Skipped when the assistant is off, not on Claude Code, fake, or polled < 4 min ago."""
    s = get_assistant_settings()
    if not s.enabled or s.backend != "claude_code" or (s.fake and _poller is None):
        return {"status": "skipped", "reason": "assistant off, not on Claude Code, or fake"}
    last = db.kv_get(KV_POLL)
    if not force and isinstance(last, dict) and last.get("ok") and last.get("at") \
            and db.utcnow() - db.parse_iso(last["at"]) < POLL_MIN_GAP:
        return {"status": "skipped", "reason": "polled recently"}
    try:
        resp = (_poller or _sdk_get_usage)()
    except Exception as exc:
        err = str(exc)[:300] or type(exc).__name__
        if not (isinstance(last, dict) and last.get("error") == err):   # log a new failure once, not every 5 min
            db.log_event("assistant", f"Claude plan usage check failed: {err}", level="warning")
        db.kv_set(KV_POLL, {"at": db.now_iso(), "ok": False, "error": err})
        return {"status": "error", "error": err}
    available = bool((resp or {}).get("rate_limits_available"))
    n = record_plan_usage(resp or {}) if available else 0
    db.kv_set(KV_POLL, {"at": db.now_iso(), "ok": True, "error": None, "available": available, "windows": n,
                        "subscription": (resp or {}).get("subscription_type")})
    return {"status": "ok" if available else "unavailable", "windows": n}


def _infer_siblings(kind: str, status: str, util: float | None) -> None:
    """The CLI reports only the *binding* (most used) window per run, so once the weekly window is the
    tighter one the 5-hour window is never reported again and its last reading goes stale for good
    (the owner: "I've been sending messages and it hasn't refreshed"). A non-rejected reading of
    ``kind`` at ``util`` still tells us the other general window is not rejected and at most ``util``
    used, so a sibling whose reading has reset, was a rejection, or was itself inferred is replaced by
    that bound (``at_most``; ``utilization`` stays None: a bound, never a made-up figure). A real,
    current reading of the sibling is left alone."""
    if status == "rejected" or util is None or kind not in INFERABLE:
        return
    for other in INFERABLE:
        if other == kind:
            continue
        v = db.kv_get(KV_WINDOW.format(kind=other))
        if isinstance(v, dict):
            reset = bool(v.get("resets_at")) and db.parse_iso(v["resets_at"]) <= db.utcnow()
            if not (reset or v.get("status") == "rejected" or v.get("inferred_from")):
                continue
        db.kv_set(KV_WINDOW.format(kind=other), {"status": "allowed", "utilization": None, "at_most": util,
                                                  "inferred_from": kind, "resets_at": None, "seen_at": db.now_iso()})


def _window(kind: str) -> dict[str, Any] | None:
    v = db.kv_get(KV_WINDOW.format(kind=kind))
    if not isinstance(v, dict):
        return None
    if v.get("resets_at") and db.parse_iso(v["resets_at"]) <= db.utcnow():
        return {**v, "status": "allowed", "utilization": 0.0, "stale": True}   # window has reset since
    return v


def five_hour_used() -> float | None:
    """Share of the current 5-hour window used (0-1), or None when unknown/stale."""
    w = _window("five_hour")
    if not w or w.get("stale") or w.get("utilization") is None:
        return None
    return float(w["utilization"])


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
    """{windows: {kind: {left_pct|None, status, resets_at, resets_text, stale, reset_was_text, as_of,
    as_of_text, left_at_least_pct, inferred_from}}, runs_today, runs_cap, limited, limited_until, queued}.

    ``left_pct`` None = unknown: no utilization reported, or the window has reset since the last
    reading (Claude Code only reports on state changes, so a reset window is "no reading yet", never a
    made-up 100% left; ``reset_was_text`` says when it reset). ``left_at_least_pct`` is a bound
    inferred from the other general window (:func:`_infer_siblings`)."""
    from assistant import claude_code

    s = get_assistant_settings()
    windows = {}
    for k in WINDOW_KINDS:
        w = _window(k)
        if w is None:
            continue
        stale = bool(w.get("stale"))
        util = None if stale else w.get("utilization")
        resets = db.parse_iso(w["resets_at"]) if w.get("resets_at") and not stale else None
        was = db.parse_iso(w["resets_at"]) if w.get("resets_at") and stale else None
        seen = db.parse_iso(w["seen_at"]) if w.get("seen_at") else None
        windows[k] = {"left_pct": None if util is None else round(100 * (1 - util)), "status": w.get("status"),
                      "resets_at": db.to_iso(resets) if resets else None,
                      "resets_text": claude_code.friendly_time(resets) if resets else "",
                      "stale": stale, "reset_was_text": claude_code.friendly_time(was) if was else "",
                      "as_of": db.to_iso(seen) if seen else None,
                      "as_of_text": claude_code.friendly_time(seen) if seen else "",
                      "left_at_least_pct": (round(100 * (1 - w["at_most"]))
                                            if util is None and w.get("at_most") is not None else None),
                      "inferred_from": w.get("inferred_from"), "source": w.get("source") or "event"}
    until = claude_code.backoff_until()
    poll = db.kv_get(KV_POLL)
    poll = poll if isinstance(poll, dict) else {}
    return {"windows": windows, "known": bool(windows), "runs_today": claude_code.turns_today(),
            "polled_at": poll.get("at"), "poll_ok": bool(poll.get("ok") and poll.get("available")),
            "poll_error": poll.get("error"),
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
    """Scheduler job: run due deferred prompts and scheduled tasks, oldest first. Returns how many started.

    Also: hands work over to the next usage window when the plan is nearly used up
    (``assistant.handoff.on_usage_signal``) and fails rows lost to a restart (``schedule.sweep_stale``)."""
    from assistant import claude_code, runtime, schedule

    s = get_assistant_settings()
    if not s.enabled or runtime.is_stopped():
        return 0
    for fn in ("handoff.on_usage_signal", "schedule.sweep_stale"):
        try:
            mod, name = fn.split(".")
            getattr(__import__(f"assistant.{mod}", fromlist=["x"]), name)()
        except Exception as exc:
            log.warning("%s failed: %s", fn, exc)
    if claude_code.backoff_until() is not None:
        return 0
    n = 0
    for d in due()[:limit]:
        if n and claude_code.backoff_until() is not None:
            break
        if d.kind in schedule.TASK_KINDS and not schedule.can_start_task():
            continue                       # two scheduled tasks already running: next tick
        if not store.set_deferred_status(d.id, "running"):
            continue
        n += 1
        if run_claimed(d) == "limited":
            break
    return n


def run_claimed(d: store.Deferred) -> str:
    """Run one row already claimed (status ``running``). Returns started | done | failed | limited."""
    from assistant import runtime, schedule

    if d.kind in schedule.TASK_KINDS:      # real work: a background worker session, reports back by itself
        try:
            schedule.start_row(d)
        except Exception as exc:
            store.set_deferred_status(d.id, "failed", result=repr(exc)[:300])
            return "failed"
        return "started"
    if d.kind in JOB_FUNCS:  # a deferred scheduler job: run its plain function, not a prompt
        import importlib

        mod, fn = JOB_FUNCS[d.kind].split(":")
        try:
            getattr(importlib.import_module(mod), fn)()
            store.set_deferred_status(d.id, "done", result="ran")
        except Exception as exc:
            store.set_deferred_status(d.id, "failed", result=repr(exc)[:300])
            return "failed"
        return "done"
    sms = d.channel == "sms"
    task = (f"Earlier request from the owner (queued {d.created_at[:16]}Z because of the Claude plan limit; "
            f"do it now and report the result):\n{d.prompt}" if d.kind == "owner_request" else d.prompt)
    try:
        res = runtime.run_task(d.agent, task, channel=d.channel if d.channel in ("sms", "dashboard") else "system",
                               deliver=sms, kind="deferred")
    except Exception as exc:
        store.set_deferred_status(d.id, "failed", result=repr(exc)[:300])
        return "failed"
    if res.status == "budget":   # limited again: back in the queue
        store.requeue_deferred(d.id)
        return "limited"
    mid = res.message_out_id
    if d.channel == "dashboard":
        mid = store.add_message("out", "dashboard", res.text, kind="deferred", agent=d.agent)
    ok = res.status == "ok"
    store.set_deferred_status(d.id, "done" if ok else "failed", result=res.text[:500], message_id=mid)
    schedule.book_next(d)
    return "done" if ok else "failed"


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


def register_tools(register: Any) -> None:
    from assistant.tools import Tool, _obj

    register(Tool("usage_status", "How much of the owner's Claude plan is left: 5-hour and weekly windows "
                                  "(left % or unknown), when they reset, runs today vs the daily cap, queued tasks.",
                  _obj({}), _tool_usage_status), replace=True)
