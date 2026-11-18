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
MAX_PER_RUN = 8               # deferred rows claimed per tick
MAX_PARALLEL = 4              # deferred prompts running at once, each in its own thread (never one after another)
MAX_ATTEMPTS = 6              # a background pass that keeps failing stops after this many tries ...
FOREVER_KINDS = ("owner_request",)   # ... but his own requests are retried until they are done
KV_WAS_TIGHT = "assistant:usage:was_tight"
KIND_TITLES = {"auto_memory": "Memory pass", "initiative_harvest": "Backlog pass", "owner_request": "Your request",
               "prompt": "Queued prompt", "reel": "Reel", "history": "History import", "evals": "Evals",
               "improve": "Weekly improvement", "reflection": "Reflection", "distill": "Distil"}
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
    _record_overage(info)
    _after_reading(kind, status, peak, resets_iso)
    _check_freed()


KV_OVERAGE = "assistant:usage:overage"
OVERAGE_OK = ("allowed", "allowed_warning")


def _record_overage(info: dict[str, Any]) -> None:
    """Paid extra usage on his Claude plan: ``overageStatus`` allowed / allowed_warning means the overflow is
    covered (Claude Code keeps working past the 5-hour limit). Remember it, and lift any plan-limit backoff so
    Cecil keeps using the plan instead of switching to the API key."""
    ov = str(info.get("overageStatus") or info.get("overage_status") or "").strip().lower()
    if not ov:
        return
    db.kv_set(KV_OVERAGE, {"status": ov, "resets_at": _iso_of(info.get("overageResetsAt") or info.get("overage_resets_at")),
                           "disabled_reason": info.get("overageDisabledReason") or info.get("overage_disabled_reason"),
                           "at": db.now_iso()})
    if ov in OVERAGE_OK:
        from assistant import claude_code

        if claude_code.backoff_until() is not None:
            claude_code.clear_backoff()
            db.log_event("assistant", "plan limit reached, but extra usage covers it: staying on the Claude plan")


def extra_usage_active() -> bool:
    """His paid extra usage is covering the plan right now (a fresh reading, under 6 hours old)."""
    v = db.kv_get(KV_OVERAGE)
    if not isinstance(v, dict) or v.get("status") not in OVERAGE_OK:
        return False
    try:
        return (db.utcnow() - db.parse_iso(v["at"])).total_seconds() < 6 * 3600
    except Exception as exc:
        log.debug("ignored error (best effort): %r", exc)
        return False


def _after_reading(kind: str, status: str, peak: float, resets_iso: str | None, *, set_backoff: bool = True) -> None:
    """A limit hit sets the backoff (unless paid extra usage covers it); nearly out writes the handoff."""
    if set_backoff and status == "rejected" and resets_iso and not extra_usage_active():
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
            except Exception as exc:
                log.debug("ignored error (best effort): %r", exc)

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
        # A poll only reports numbers: 100% can still be covered by paid extra usage, so it never pauses
        # Cecil (only a real refusal from Claude does); it may still write the handoff for background work.
        _after_reading(top[0], top[1], peak, top[2], set_backoff=False)
    return n


def poll_plan_usage(*, force: bool = False) -> dict[str, Any]:
    """Scheduler job (every 5 min): refresh every plan window straight from Claude Code, zero tokens.
    Skipped when the assistant is off, not on Claude Code, fake, or polled < 4 min ago."""
    s = get_assistant_settings()
    if not s.enabled or s.backend != "claude_code" or (s.fake and _poller is None):
        return {"status": "skipped", "reason": "assistant off, not on Claude Code, or fake"}
    _check_freed()   # also notices a plan limit that simply ran out, with no new reading
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
    _check_freed()
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
            "runs_cap": 0, "limited": until is not None,
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
    return False, None


def wait_reason(*, optional: bool = True) -> str:
    """THE gate for work nobody is waiting on right now: "" = go ahead, else why it must wait.

    Every background runner asks this one function (the deferred queue, task retries, the heartbeat,
    initiative, self-repair, the morning surprise, memory/backlog passes, history import, the Shorts
    pipeline on the plan) instead of combining ``is_stopped`` / ``backoff_until`` / ``constrained`` its
    own way - one rule, one wording, no part waiting while another runs. ``optional=False`` (work he asked
    for) only waits for a real limit or the kill switch, not for high usage. Never raises."""
    from assistant import claude_code, runtime

    try:
        if runtime.is_stopped():
            return "I am switched off"
        until = claude_code.backoff_until()
        if until is not None:
            return f"the Claude plan is at its limit until {claude_code.friendly_time(until)}"
        if optional:
            tight, until = constrained()
            if tight:
                return "Claude plan usage is high" + (f" until {claude_code.friendly_time(until)}" if until else "")
    except Exception as exc:   # no reading = no reason to wait
        log.debug("usage gate unreadable: %s", exc)
    return ""


def should_defer(kind: str, prompt: str, *, agent: str = "main", channel: str = "system") -> int | None:
    """For a non-urgent *scheduler job* (``kind`` in :data:`JOB_FUNCS`): if the plan is tight, queue it to run
    its own function after the reset and return the deferred id (the caller then skips the work); else None.

    Deferral reruns the SAME code later; it never turns work into a free-text prompt for a full-power run
    (that is how the memory and backlog passes ended up "failing after one second"). Work that can simply
    try again on its next tick should check :func:`wait_reason` and keep its place instead."""
    if kind not in JOB_FUNCS:
        raise ValueError(f"should_defer({kind!r}): only scheduler jobs in JOB_FUNCS can be deferred; "
                         "check usage.wait_reason() and retry on the next tick instead")
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
    for d in store.list_deferred("queued", limit=500):
        if d.after_reset:
            if not tight:
                out.append(d)
        elif d.run_after and db.parse_iso(d.run_after) <= now:
            out.append(d)
    return out


# -- running the queue: parallel, retried, explained -----------------------------------

def _spawn_thread(fn: Callable[[], Any]) -> None:
    import threading

    threading.Thread(target=fn, name="deferred", daemon=True).start()


_spawn: Callable[[Callable[[], Any]], None] = _spawn_thread


def set_spawner(fn: Callable[[Callable[[], Any]], None] | None) -> None:
    """Tests: run the queue inline (``lambda f: f()``) instead of in threads."""
    global _spawn
    _spawn = fn or _spawn_thread


def reset() -> None:
    """Tests: forget this process's live rows and start time."""
    global _boot
    _live.clear()
    _boot = None


def kick(reason: str = "") -> None:
    """Start whatever is waiting, right now and without blocking: due deferred rows and due task retries.
    Called when plan usage frees up again (:func:`_check_freed`) and when he asks for a retry."""
    from assistant import tasks

    def go() -> None:
        from assistant import auto_memory

        for fn in (run_due, tasks.retry_due, auto_memory.catch_up):
            try:
                fn()
            except Exception as exc:   # one queue's trouble must not stop the other
                log.warning("kick: %s failed: %s", getattr(fn, "__name__", fn), exc)

    if reason:
        db.log_event("assistant", f"starting waiting work ({reason})")
    _spawn(go)


def _check_freed() -> None:
    """Plan usage just went from limited/tight to free: start everything that waited for it at once."""
    try:
        tight = constrained()[0]
    except Exception as exc:
        log.debug("ignored error (best effort): %r", exc)
        return
    was = bool(db.kv_get(KV_WAS_TIGHT, False))
    if tight != was:
        db.kv_set(KV_WAS_TIGHT, tight)
    if was and not tight:
        kick("Claude plan usage is free again")


def _meta(d: store.Deferred) -> dict[str, Any]:
    return dict(d.meta) if isinstance(d.meta, dict) else {}


def title_of(d: store.Deferred) -> str:
    if d.title:
        return d.title
    base = KIND_TITLES.get(d.kind, d.kind.replace("_", " ").capitalize())
    if d.kind in ("owner_request", "prompt"):
        first = " ".join((d.prompt or "").split())[:120]
        return f"{base}: {first}" if first else base
    return base


def retry_later(d: store.Deferred, status: str, error: str, kind: str = "") -> bool:
    """A claimed (``running``) row did not finish: back in the queue with a backoff (or until the plan
    resets, for a plan limit), its real error kept. False when it has used its attempts (then ``failed``)."""
    from assistant import retry, tasks

    meta = _meta(store.get_deferred(d.id) or d)   # the freshest copy: never act on a stale one
    attempts = int(meta.get("attempts") or 0) + 1
    why = error or retry.describe(status, kind)
    meta.update(attempts=attempts, last_error=why[:300], last_status=status)
    store.merge_deferred_meta(d.id, attempts=attempts, last_error=why[:300], last_status=status)
    if d.kind not in FOREVER_KINDS and status != "budget" and attempts >= MAX_ATTEMPTS:
        store.set_deferred_status(d.id, "failed", result=f"Gave up after {attempts} attempts: {why}"[:500])
        db.log_event("assistant", f"deferred {d.kind} #{d.id} gave up after {attempts} attempts", level="warning",
                     data={"error": why[:300]})
        tid = meta.get("track_id")
        if tid:
            tasks.track_done(tid, "", "error", summary=f"Gave up after {attempts} attempts: {why}"[:300], error=why)
        return False
    nxt = retry.next_try(status, attempts, kind)
    plan = status == "budget"
    store.update_deferred(d.id, run_after=None if plan else db.to_iso(nxt), after_reset=plan)
    store.requeue_deferred(d.id)
    tasks.track_retrying(meta.get("track_id"), nxt, why)
    db.log_event("assistant", f"deferred {d.kind} #{d.id} will be retried", level="warning",
                 data={"attempt": attempts, "status": status, "error": why[:300],
                       "next_try": "after the plan resets" if plan else db.to_iso(nxt)})
    return True


def retry_deferred(did: int) -> bool:
    """Run this deferred row again now (he asked): a failed one is reopened; a queued one is due at once."""
    d = store.get_deferred(did)
    if d is None:
        return False
    if d.status == "running":
        return True
    if d.status in ("failed", "cancelled"):
        store.reopen_deferred(did)
    elif d.status != "queued":
        return False
    store.update_deferred(did, run_after=db.now_iso(), after_reset=False)
    kick()
    return True


def run_due(limit: int = MAX_PER_RUN) -> int:
    """Scheduler job: run due deferred prompts and scheduled tasks, oldest first. Returns how many started.

    Also: hands work over to the next usage window when the plan is nearly used up
    (``assistant.handoff.on_usage_signal``) and requeues rows lost to a restart (:func:`sweep_orphans`)."""
    from assistant import schedule

    s = get_assistant_settings()
    if not s.enabled or wait_reason(optional=False) == "I am switched off":
        return 0
    _note_boot()
    for fn in ("handoff.on_usage_signal", "usage.sweep_orphans"):
        try:
            mod, name = fn.split(".")
            getattr(__import__(f"assistant.{mod}", fromlist=["x"]), name)()
        except Exception as exc:
            log.warning("%s failed: %s", fn, exc)
    if wait_reason(optional=False):           # at the plan's limit: nothing starts
        return 0
    n = 0
    room = MAX_PARALLEL - sum(1 for d in store.list_deferred("running", limit=200) if d.kind not in schedule.TASK_KINDS)
    for d in due()[:limit]:
        if wait_reason(optional=False):
            break
        if d.kind in schedule.TASK_KINDS:
            if not schedule.can_start_task():
                continue                   # the scheduled-task slots are full: next tick (or the next kick)
        elif room <= 0:
            continue
        if not store.set_deferred_status(d.id, "running"):
            continue
        n += 1
        if d.kind in schedule.TASK_KINDS:
            run_claimed(d)                 # starts its own background worker and returns at once
        else:
            room -= 1
            _live.add(d.id)                # live from the moment it is claimed, not when its thread gets going
            _spawn(lambda d=d: _run_safely(d))
    return n


_live: set[int] = set()          # deferred rows this process is running right now
_boot: str | None = None         # when this process first ran the queue (rows "running" from before = orphans)


def _note_boot() -> str:
    global _boot
    if _boot is None:
        _boot = db.now_iso()
    return _boot


def sweep_orphans(now: datetime | None = None) -> int:
    """Rows left ``running`` by a run that no longer exists (the app restarted, a thread died) go back to the
    queue with that reason - retried, never silently failed - and stale ``running`` staff runs are closed.
    A row is an orphan when no live thread or background task here owns it and it started before this
    process did, or longer ago than any run can take. Returns how many rows were requeued."""
    from assistant import schedule, tasks

    now = now or db.utcnow()
    boot = _note_boot()
    limit = timedelta(seconds=get_assistant_settings().cc_task_timeout_sec) + schedule.STALE_AFTER
    live_tasks = {t["id"] for t in tasks.running_tasks() if tasks._alive(t["id"])}
    n = 0
    for d in store.list_deferred("running", limit=500):
        if d.id in _live or _meta(d).get("task_id") in live_tasks:
            continue
        started = d.started_at or d.created_at
        if started < boot or now - db.parse_iso(started) > limit:
            retry_later(d, "error", "interrupted: the app restarted (or the run died) while it ran")
            n += 1
    try:
        store.close_stale_runs(db.to_iso(now - limit))
    except Exception as exc:
        log.warning("stale staff runs not closed: %s", exc)
    return n


def _run_safely(d: store.Deferred) -> None:
    _live.add(d.id)
    try:
        run_claimed(d)
    except Exception as exc:   # never leave a row stuck as "running"
        log.exception("deferred row failed")
        current = store.get_deferred(d.id) or d
        if current.status == "running":
            retry_later(current, "error", f"{type(exc).__name__}: {exc}"[:300])
    finally:
        _live.discard(d.id)


def run_claimed(d: store.Deferred) -> str:
    """Run one row already claimed (status ``running``). Returns started | done | failed | limited."""
    from assistant import runtime, schedule

    if d.kind in schedule.TASK_KINDS:      # real work: a background worker session, reports back by itself
        try:
            schedule.start_row(d)
        except Exception as exc:
            retry_later(d, "error", f"could not start: {type(exc).__name__}: {exc}"[:300])
            return "failed"
        return "started"
    if d.kind in JOB_FUNCS:  # a deferred scheduler job: run its plain function, not a prompt
        import importlib

        mod, fn = JOB_FUNCS[d.kind].split(":")
        try:
            getattr(importlib.import_module(mod), fn)()
            store.set_deferred_status(d.id, "done", result="ran")
        except Exception as exc:
            retry_later(d, "error", f"{type(exc).__name__}: {exc}"[:300])
            return "failed"
        return "done"
    sms = d.channel == "sms"
    meta = _meta(d)
    task = (f"Earlier request from the owner (queued {d.created_at[:16]}Z; do it now and report the "
            f"result):\n{d.prompt}" if d.kind == "owner_request" else d.prompt)
    res = runtime.run_task(d.agent, task, channel=d.channel if d.channel in ("sms", "dashboard") else "system",
                           deliver=sms, kind="deferred", title=title_of(d), track_meta={"deferred_id": d.id},
                           track_id=meta.get("track_id"))
    if res.track_id and res.track_id != meta.get("track_id"):
        store.merge_deferred_meta(d.id, track_id=res.track_id)
        d = store.get_deferred(d.id) or d
    if res.status == "ok":
        mid = res.message_out_id
        if d.channel == "dashboard":
            mid = store.add_message("out", "dashboard", res.text, kind="deferred", agent=d.agent)
        store.set_deferred_status(d.id, "done", result=res.text[:500], message_id=mid)
        schedule.book_next(d)
        return "done"
    if res.status in ("cancelled", "denied"):   # he stopped it / a refusal: final
        store.set_deferred_status(d.id, "cancelled" if res.status == "cancelled" else "failed",
                                  result=(res.error or res.text)[:500])
        return "failed"
    retry_later(d, res.status, res.error, res.error_kind)
    return "limited" if res.status == "budget" else "failed"


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
