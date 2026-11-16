"""What needs the owner: ONE list behind the "N need you" badge, Cecil's answers and the boot briefing.

Items (in this order):
* each pending approval (``db`` decisions)
* each due, unanswered check-in (meal preferences, hours, ...)
* pipeline failures (error events) since he last looked, as ONE item - looking clears it
* agent runs that failed since he last looked, as ONE item - looking clears it

"Looked" = he opened the failure log / the agent page from the item, said "dismiss" to Cecil
(tool ``needs_dismiss``), or Cecil showed it to him. Stored in kv ``needs:seen:<kind>`` (UTC ISO).
Failures older than :data:`WINDOW_HOURS` never count, seen or not.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable

import db

WINDOW_HOURS = 24
ACK_KINDS = ("failures", "run_errors")
_WORDS = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


def _seen_key(kind: str) -> str:
    return f"needs:seen:{kind}"


def _since(kind: str) -> str:
    floor = db.to_iso(db.utcnow() - timedelta(hours=WINDOW_HOURS))
    seen = db.kv_get(_seen_key(kind))
    return max(floor, seen) if isinstance(seen, str) else floor


def acknowledge(kind: str) -> bool:
    """He has seen the failures / failed runs: stop counting them (new ones count again)."""
    if kind not in ACK_KINDS:
        return False
    db.kv_set(_seen_key(kind), db.now_iso())
    return True


def failures() -> list[db.Event]:
    """Error events since he last looked (newest first, capped)."""
    since = _since("failures")
    return [e for e in db.list_events(limit=500, level="error", since=since) if e.ts > since]


def run_errors() -> list[Any]:
    from assistant import store

    since = _since("run_errors")
    return [r for r in store.list_agent_runs(50, since=since) if r.status == "error" and r.ts > since]


def data() -> dict[str, Any]:
    """The shape the home page renders (kept compatible with the old ``home.needs_data``)."""
    from assistant import store

    pending = db.list_decisions("pending", limit=10)
    now = db.now_iso()
    checkins = [c for c in store.list_checkins("pending", limit=200) if c.sent_at or c.scheduled_for <= now][:5]
    fails = failures()
    runs = run_errors()
    return {"decisions": pending, "checkins": checkins, "failures_24h": len(fails),
            "last_error": fails[0] if fails else None, "run_errors": runs[:3],
            "count": len(pending) + len(checkins) + (1 if fails else 0) + (1 if runs else 0)}


def items(d: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Every item, plain, numbered the way the badge counts them."""
    d = d or data()
    out: list[dict[str, Any]] = []
    for x in d["decisions"]:
        out.append({"kind": "approval", "id": x.id, "text": f"Approval #{x.id} ({x.kind.replace('_', ' ')}): {x.summary}",
                    "how": "approve or reject (say yes/no, or request_approval tools)"})
    for c in d["checkins"]:
        out.append({"kind": "checkin", "id": c.id, "text": f"Question #{c.id}: {c.question}",
                    "how": "ask him; record his answer with checkin_answer(checkin_id, answer)"})
    if d["failures_24h"]:
        last = d["last_error"]
        out.append({"kind": "failures", "count": d["failures_24h"],
                    "text": f"{d['failures_24h']} pipeline failure(s) since he last looked"
                            + (f"; latest: {last.module}: {last.message}" if last else ""),
                    "how": "explain the cause and the fix; needs_dismiss(kind=failures) once he has heard it"})
    if d["run_errors"]:
        r = d["run_errors"][0]
        out.append({"kind": "run_errors", "count": len(d["run_errors"]),
                    "text": f"{len(d['run_errors'])} staff task(s) failed; latest: {r.agent}: {(r.summary or r.task or '')[:160]}",
                    "how": "explain; needs_dismiss(kind=run_errors) once he has heard it"})
    return out


def _n(n: int) -> str:
    return _WORDS[n] if 0 <= n < len(_WORDS) else str(n)


def briefing(honorific: str = "sir") -> str:
    """What Cecil says when he opens the app (plain code, no tokens). Empty when nothing needs him."""
    its = items()
    if not its:
        return ""
    head = (f"One thing needs you, {honorific}." if len(its) == 1
            else f"{_n(len(its)).capitalize()} things need you, {honorific}.")
    parts = []
    for i, it in enumerate(its[:5], 1):
        t = it["text"]
        if it["kind"] == "checkin":
            t = t.split(": ", 1)[-1]
        elif it["kind"] == "approval":
            t = "An approval: " + t.split(": ", 1)[-1]
        parts.append((f"{_n(i).capitalize()}: " if len(its) > 1 else "") + t.rstrip(".") + ".")
    return " ".join([head, *parts])[:900]


def answer_checkin(checkin_id: int, answer: str, *, via: str = "voice") -> str:
    """Record his answer to a check-in however it arrived (voice, chat, dashboard form, text): the same
    handling as a texted answer (hours logged, meal preferences saved...), else stored as given."""
    from assistant import checkins, store
    from assistant.settings import get_assistant_settings

    chk = store.get_checkin(int(checkin_id))
    if chk is None:
        raise ValueError(f"no question #{checkin_id}")
    if chk.status != "pending":
        return "Already answered."
    text = " ".join((answer or "").split())[:500]
    if not text:
        raise ValueError("empty answer")
    reply = checkins._answer(chk, text, get_assistant_settings().owner_honorific)
    if reply is None:
        if chk.kind == "onboarding:meals":
            from assistant import meals

            meals.record_preferences(text, source=via)      # also closes this question
        if (store.get_checkin(chk.id) or chk).status == "pending":
            store.answer_checkin(chk.id, text, {"via": via})
        reply = "Noted."
    db.log_event("assistant", f"check-in {chk.id} answered", data={"kind": chk.kind, "via": via})
    return reply


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

def register_tools(register: Callable[..., None]) -> None:
    from assistant.tools import Tool, ToolError, _obj

    def _list(ctx: Any, a: dict[str, Any]) -> Any:
        its = items()
        return {"count": len(its), "items": its,
                "note": "This is exactly what the 'need you' badge counts. Go through them one at a time."}

    def _dismiss(ctx: Any, a: dict[str, Any]) -> Any:
        kind = str(a.get("kind") or "")
        if not acknowledge(kind):
            raise ToolError(f"kind must be one of {ACK_KINDS}; approvals and questions clear when answered")
        return {"dismissed": kind, "remaining": len(items())}

    def _answer(ctx: Any, a: dict[str, Any]) -> Any:
        try:
            return {"recorded": True, "reply": answer_checkin(a["checkin_id"], a["answer"],
                                                              via=getattr(ctx, "channel", "voice") or "voice")}
        except ValueError as exc:
            raise ToolError(str(exc)) from None

    register(Tool("checkin_answer", "Record his answer to one of my questions (check-in id from needs_list), "
                  "whichever way he gave it (voice, chat). Saves it where it belongs (hours, meal preferences, "
                  "profile) so I never ask again.",
                  _obj({"checkin_id": {"type": "integer"}, "answer": {"type": "string"}}, ["checkin_id", "answer"]),
                  _answer, owner="assistant.needs", side_effects=True), replace=True)
    register(Tool("needs_list", "Everything that needs the owner right now - the same list and count as the "
                  "'need you' badge (approvals, unanswered questions, failures, failed staff tasks).",
                  _obj({}, []), _list, owner="assistant.needs"), replace=True)
    register(Tool("needs_dismiss", "Mark the failures (kind=failures) or failed staff tasks (kind=run_errors) as "
                  "seen so they stop counting; new ones count again.",
                  _obj({"kind": {"type": "string", "enum": list(ACK_KINDS)}}, ["kind"]), _dismiss,
                  owner="assistant.needs", side_effects=True), replace=True)
