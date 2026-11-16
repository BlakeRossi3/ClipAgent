"""Retry policy for unfinished background work (owner's rule: nothing is left sitting unfinished).

Any background run that ends in ``budget`` (plan limit / spending cap), ``error`` (incl. a Claude Code
timeout), ``stopped`` (kill switch) or ``rounds`` (ran out of tool rounds) is tried again, with no
attempt limit, until it succeeds. Only ``denied`` (a refusal) is final.

* When: plan limit -> just after the reset; daily dollar cap -> after local midnight; anything else
  -> backoff 5, 15, 30 then every 60 minutes.
* Blockers he can fix (sign-in, Claude Code not starting, spending paused, Cecil switched off, a task
  failing again and again) are texted to him once: :func:`_error_signature` names the blocker and one
  signature is one text per :data:`NOTIFY_EVERY`.

Used by ``assistant.tasks`` for conversation tasks (no ``on_finish`` owner, not a self-edit). A plan limit
mid-task is not retried there: ``assistant.handoff`` already resumes that work after the reset. Plain
code, no model calls.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import db

log = logging.getLogger(__name__)

RETRY_STATUSES = ("budget", "error", "stopped", "rounds")
BACKOFF_MINUTES = (5, 15, 30, 60)          # then every 60 minutes, forever
RESET_GRACE = timedelta(minutes=5)
WAIT_FALLBACK = timedelta(minutes=30)      # budget/stopped with no known end time
NAG_AFTER = 3                              # repeated failures: tell him at attempt 3, then every 10th
NOTIFY_EVERY = timedelta(hours=6)
KV_NOTIFIED = "assistant:retry:notified:{key}"


def should_retry(status: str) -> bool:
    return status in RETRY_STATUSES


def describe(status: str, kind: str = "") -> str:
    """Why a run did not finish, in a few plain words."""
    if kind == "timeout":
        return "it ran out of time"
    if kind in ("auth", "not_installed"):
        return "the Claude sign-in needs renewing"
    if kind == "blocked":
        return "Claude Code could not start"
    if status == "budget":
        return {"paused": "spending is paused", "daily": "the daily spending cap was reached",
                "turn": "it hit the per-task spending cap", "turns": "today's run allowance was used up",
                }.get(kind, "the Claude plan limit was reached")
    if status == "stopped":
        return "I am switched off"
    if status == "rounds":
        return "it needed more steps than one run allows"
    return "it failed with an error"


def next_try(status: str, attempt: int, kind: str = "", now: datetime | None = None) -> datetime:
    """When to try again after ``attempt`` (1-based) ended in ``status``."""
    now = now or db.utcnow()
    if status == "budget":
        if kind == "daily":   # the dollar cap resets at local midnight
            tomorrow = datetime.combine(db.local_today() + timedelta(days=1), datetime.min.time(), db.local_tz())
            return max(now + timedelta(minutes=5), tomorrow.astimezone(UTC) + RESET_GRACE)
        if kind == "paused":
            return now + WAIT_FALLBACK
        try:
            from assistant import claude_code, usage

            until = claude_code.backoff_until() or (usage.next_reset() if kind in ("limit", "") else None)
        except Exception:
            until = None
        if until is not None and until > now:
            return until + RESET_GRACE
        return now + WAIT_FALLBACK
    if status == "stopped" or kind == "paused":
        return now + WAIT_FALLBACK
    i = max(0, min(attempt - 1, len(BACKOFF_MINUTES) - 1))
    return now + timedelta(minutes=BACKOFF_MINUTES[i])


def retry_note(attempt: int, last_error: str) -> str:
    """Added to the brief of a retried run."""
    return (f"\n\n<retry>\nThis is attempt {attempt}; the previous attempt did not finish ({last_error or 'unknown'}). "
            "Check what is already done before redoing anything, carry on from there, and work in smaller steps so "
            "it finishes. If something only the owner can fix is blocking you, say exactly what in your final "
            "answer.\n</retry>")


def _error_signature(status: str, kind: str, attempt: int, task: str) -> str | None:
    """The blocker's identity for deduplication, or None when it is not one he can fix.

    Account-level blockers (sign-in, Claude Code not starting, paused, switched off) share one signature
    whatever the task, so ten waiting tasks are still one text. A task that keeps failing is its own
    signature at attempt 3 and every 10th after."""
    if kind in ("auth", "not_installed"):
        return "auth"
    if kind == "blocked":
        return "blocked"
    if kind == "paused":
        return "paused"
    if status == "stopped":
        return "stopped"
    if status in ("error", "rounds") and (attempt == NAG_AFTER or (attempt > NAG_AFTER and attempt % 10 == 0)):
        return f"task:{' '.join(task.split())[:60]}:{attempt}"
    return None


def blocker(status: str, kind: str, attempt: int, task: str) -> tuple[str, str] | None:
    """(signature, text for the owner) when he can unblock this, else None."""
    sig = _error_signature(status, kind, attempt, task)
    if sig is None:
        return None
    from assistant.settings import get_assistant_settings

    h = get_assistant_settings().owner_honorific
    short = " ".join(task.split())[:120]
    texts = {
        "auth": (f"I can't reach Claude, {h}: the sign-in needs renewing (Settings, The brain, Sign in). "
                 f"Tasks are waiting, e.g. {short}"),
        "blocked": (f"Claude Code won't start on the laptop, {h} (possibly antivirus or permissions). "
                    f"Tasks are waiting, e.g. {short}"),
        "paused": f"Spending is paused, {h}, so tasks are waiting. Text RESUME when you want them done.",
        "stopped": (f"I'm switched off, {h}, and tasks are waiting. Text START CECIL and I'll finish "
                    f"them, e.g. {short}"),
    }
    text = texts.get(sig) or (f"A task has failed {attempt} times, {h} ({describe(status, kind)}): {short}. "
                              "I'm still retrying; if you can see what's blocking it, tell me.")
    return sig, text


def report_blocker(status: str, kind: str, attempt: int, task: str) -> bool:
    """Text him about a blocker he can fix, at most once per signature every few hours. True if sent."""
    b = blocker(status, kind, attempt, task)
    if b is None:
        return False
    sig, text = b
    kv = KV_NOTIFIED.format(key=sig)
    last = db.kv_get(kv)
    if isinstance(last, dict) and last.get("at") and db.parse_iso(last["at"]) > db.utcnow() - NOTIFY_EVERY:
        return False
    from assistant import tools

    try:
        tools.send_to_owner(text, kind="task_blocked", agent="main")
    except Exception as exc:   # cap reached, no SMS set up...: it still shows in the events log
        log.info("blocker not texted: %s", exc)
        db.log_event("assistant", "task blocker not texted", level="warning", data={"error": str(exc)[:200]})
        return False
    db.kv_set(kv, {"at": db.now_iso()})
    db.log_event("assistant", "task blocker sent to owner", data={"key": sig})
    return True
