"""Standing approvals ("always yes"): Blake approves a kind of request once, later identical ones go through.

When he approves a request he can say "always allow this kind": ``YES ALWAYS <id>`` (or ``ALWAYS <id>``) by
text, the dashboard's *Always allow* button, keypad 3 or saying "always" on the approval call. That stores a
rule keyed by an action type plus a normalised target (the fingerprint), e.g. ``("twilio_fix",
"set_sms_webhook")`` or, for the generic ``request_approval``
tool, the ``action_type`` / ``target`` the agent supplied (``("desktop_mode", "gaming")``).

:func:`auto_approve` is called by ``notify.request_approval`` right after the decision row is written: a
request matching an active rule is approved (``resolved_via='standing'``), applied, logged and announced in
one line instead of phoning him.

Some things can NEVER become standing, whatever he answers: spending or budget increases, posting publicly
(a YouTube upload going public, social posts), messaging anyone but him, deleting his files, installing
third-party code (any skill install, trusted source or not), strategy major changes (every phase-1 strategy decision) and edits
whose content differs each time (improvement proposals, his heartbeat checklist). :func:`classify` says why.

Rules live in kv ``approvals:rules`` (``{id: rule}``); Settings -> Approvals (``/dashboard/approvals``) and the
admin tools ``approval_rules_list`` / ``approval_rule_revoke`` review and revoke them.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable

import db

log = logging.getLogger(__name__)

MODULE = "approvals"
KV_RULES = "approvals:rules"
KV_SEQ = "approvals:rule_seq"
MAX_RULES = 100

# Handlers whose approval covers one specific piece of content: approving one does not approve the next.
CONTENT_HANDLERS = {
    "improvement": "each improvement edits different text, so each one is shown to him",
    "heartbeat_checklist": "his heartbeat checklist is his own note; each change is shown to him",
}

# Categories that always ask. Checked against the fingerprint and (for free-text requests) the summary.
_NEVER: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("spending money or raising a budget always asks",
     re.compile(r"\b(spend\w*|budget\w*|pay|paying|payment\w*|purchas\w*|buy\w*|order\w*|subscri\w*|charg\w*|"
                r"invoice\w*|price\w*|pricing|cost\w*|money|dollars?|usd|refund\w*|billing|upgrade plan)\b|\$\s?\d",
                re.I)),
    ("posting publicly always asks",
     re.compile(r"\b(publish\w*|public\w*|post|posts|posting|posted|tweet\w*|upload\w*|go live|going live|"
                r"broadcast\w*|instagram|tiktok|facebook|twitter|linkedin|reddit|youtube|threads|bluesky|"
                r"social)\b", re.I)),
    ("messaging anyone else always asks",
     re.compile(r"\b(e-?mail\w*|messag\w*|text|texts|texting|sms|whatsapp|dm|dms|call|calls|calling|phone|"
                r"contact\w*|reply|replies|invite\w*|send to|send it to|forward\w*)\b", re.I)),
    ("deleting files always asks",
     re.compile(r"\b(delet\w*|remov\w*|eras\w*|wipe\w*|rm|trash\w*|purg\w*|overwrit\w*|destroy\w*|format)\b",
                re.I)),
    ("installing third-party code always asks",
     re.compile(r"\b(install\w*|pip|npm|winget|choco|executable|exe|script from|download and run|run code)\b",
                re.I)),
    ("strategy changes always ask",
     re.compile(r"\b(strategy|niche|allowlist|blocklist|source type)\b", re.I)),
)

_lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class Fingerprint:
    action_type: str
    target: str
    label: str

    @property
    def key(self) -> str:
        return f"{self.action_type}|{self.target}"


Fingerprinter = Callable[[dict[str, Any]], "Fingerprint | str"]   # a str = why this one cannot be standing
FINGERPRINTERS: dict[str, Fingerprinter] = {}


def register_fingerprinter(handler: str, fn: Fingerprinter) -> None:
    """Let an approval handler opt in to standing rules by naming what a request acts on."""
    FINGERPRINTERS[handler] = fn


def norm(value: Any) -> str:
    """Lower-case, single-spaced, path/URL-ish noise trimmed: the comparable form of a target."""
    t = " ".join(str(value or "").lower().split())
    t = re.sub(r"^https?://(www\.)?", "", t).strip(" /.")
    t = t.removesuffix(".git")
    return t[:200]


def _never(text: str) -> str | None:
    for reason, rx in _NEVER:
        if rx.search(text.replace("_", " ")):
            return reason
    return None


# -- built-in fingerprints ---------------------------------------------------------

def _fp_twilio(data: dict[str, Any]) -> Fingerprint | str:
    action = norm(data.get("action"))
    if not action:
        return "no fix named"
    return Fingerprint("twilio_fix", action, f"Twilio fix: {action.replace('_', ' ')}")


def _fp_skill(data: dict[str, Any]) -> Fingerprint | str:
    """Skill installs never become standing: every skill (and every new version of one) is different code,
    so each is reviewed by him (Settings -> Skills, or YES <id> for an unknown source)."""
    return "each skill install is different third-party code, so every one is shown to you"


register_fingerprinter("twilio_fix", _fp_twilio)
register_fingerprinter("skill_install", _fp_skill)


# -- classification -------------------------------------------------------------------

def classify(decision: db.Decision) -> tuple[Fingerprint | None, str]:
    """(fingerprint, "") when this request may become a standing rule, else (None, why not)."""
    payload = decision.payload if isinstance(decision.payload, dict) else {}
    if decision.kind != "other" or payload.get("action") != "assistant_action":
        return None, "strategy changes always ask"
    if payload.get("requested_kind") in db.DECISION_KINDS[:4]:
        return None, "strategy changes always ask"
    handler = str(payload.get("handler") or "note")
    if handler in CONTENT_HANDLERS:
        return None, CONTENT_HANDLERS[handler]
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if handler in FINGERPRINTERS:
        try:
            got = FINGERPRINTERS[handler](data)
        except Exception as exc:
            return None, f"could not identify the request ({type(exc).__name__})"
        if isinstance(got, str):
            return None, got
        return got, ""   # plain-code fingerprinters do their own safety checks (e.g. trusted skill sources)
    if handler != "note":
        return None, "this kind of request has no stable fingerprint"
    action_type = norm(payload.get("action_type") or data.get("action_type"))
    target = norm(payload.get("target") or data.get("target"))
    if not action_type or not target:
        return None, "the request did not name an action type and target"
    reason = _never(f"{action_type} {target} {decision.summary}")
    if reason:
        return None, reason
    return Fingerprint(action_type, target, f"{action_type.replace('_', ' ')}: {target}"), ""


def eligible(decision: db.Decision | None) -> bool:
    return decision is not None and classify(decision)[0] is not None


# -- rules ------------------------------------------------------------------------------

def _rules() -> dict[str, dict[str, Any]]:
    v = db.kv_get(KV_RULES, {}) or {}
    return v if isinstance(v, dict) else {}


def list_rules(*, include_revoked: bool = False) -> list[dict[str, Any]]:
    rows = [r for r in _rules().values() if include_revoked or not r.get("revoked_at")]
    return sorted(rows, key=lambda r: int(r["id"]), reverse=True)


def get_rule(rule_id: int) -> dict[str, Any] | None:
    return _rules().get(str(int(rule_id)))


def find_rule(fp: Fingerprint) -> dict[str, Any] | None:
    for r in _rules().values():
        if not r.get("revoked_at") and r.get("action_type") == fp.action_type and r.get("target") == fp.target:
            return r
    return None


def add_rule(fp: Fingerprint, *, via: str, decision_id: int | None = None) -> dict[str, Any]:
    """Store (or return the existing) active rule for ``fp``."""
    with _lock:
        existing = find_rule(fp)
        if existing is not None:
            return existing
        rules = _rules()
        if sum(1 for r in rules.values() if not r.get("revoked_at")) >= MAX_RULES:
            raise ValueError(f"at most {MAX_RULES} standing approvals; revoke some first")
        rid = db.kv_incr(KV_SEQ)
        rule = {"id": rid, "action_type": fp.action_type, "target": fp.target, "label": fp.label,
                "created_at": db.now_iso(), "created_via": via, "decision_id": decision_id, "uses": 0,
                "last_used_at": None, "revoked_at": None, "revoked_via": None}
        rules[str(rid)] = rule
        db.kv_set(KV_RULES, rules)
    db.log_event(MODULE, f"standing approval added: {fp.label}",
                 data={"rule_id": rid, "via": via, "decision_id": decision_id})
    return rule


def revoke_rule(rule_id: int, *, via: str) -> bool:
    with _lock:
        rules = _rules()
        r = rules.get(str(int(rule_id)))
        if r is None or r.get("revoked_at"):
            return False
        r.update(revoked_at=db.now_iso(), revoked_via=via)
        db.kv_set(KV_RULES, rules)
    db.log_event(MODULE, f"standing approval revoked: {r['label']}", data={"rule_id": r["id"], "via": via})
    return True


def _touch(rule_id: int) -> None:
    with _lock:
        rules = _rules()
        r = rules.get(str(rule_id))
        if r is not None:
            r["uses"] = int(r.get("uses") or 0) + 1
            r["last_used_at"] = db.now_iso()
            db.kv_set(KV_RULES, rules)


def _records_only(d: db.Decision) -> bool:
    """A plain request_approval note: nothing to apply, the agent just proceeds once approved."""
    p = d.payload if isinstance(d.payload, dict) else {}
    return str(p.get("handler") or "note") == "note"


def match(decision: db.Decision) -> dict[str, Any] | None:
    fp, _why = classify(decision)
    return find_rule(fp) if fp is not None else None


# -- the two entry points ----------------------------------------------------------------

def auto_approve(decision_id: int) -> bool:
    """Called by ``notify.request_approval``: approve + apply now if a standing rule covers it."""
    d = db.get_decision(decision_id)
    if d is None or d.status != "pending":
        return False
    rule = match(d)
    if rule is None:
        return False
    import notify

    out = notify.decide(decision_id, True, via="standing")
    if not out.resolved:
        return False
    _touch(int(rule["id"]))
    applied = None if _records_only(d) else out.applied
    line = (f"Approved #{decision_id} under your standing rule ({rule['label']}): {d.summary}"[:300]
            + ("" if applied in (True, None) else " It did not apply; see the dashboard."))
    db.log_event(MODULE, line, data={"decision_id": decision_id, "rule_id": rule["id"], "applied": applied})
    try:
        from assistant import store

        store.add_message("out", "dashboard", line, kind="standing_approval", agent="main",
                          meta={"decision_id": decision_id, "rule_id": rule["id"]})
    except Exception as exc:
        log.debug("ignored error (best effort): %r", exc)
    return True


def approve_always(decision_id: int, *, via: str) -> dict[str, Any]:
    """Approve ``decision_id`` (if still pending) and make its kind standing when allowed.

    Works on an already-approved decision too (he says "always" after "yes"). Returns
    ``{ok, resolved, applied, rule, reason, text}``; ``text`` is the one-line answer for him.
    """
    import notify

    d = db.get_decision(decision_id)
    if d is None:
        return {"ok": False, "rule": None, "text": f"No request #{decision_id}."}
    applied = None
    resolved = False
    if d.status == "pending":
        out = notify.decide(decision_id, True, via=via)
        resolved, applied = out.resolved, out.applied
        d = db.get_decision(decision_id) or d
        if _records_only(d):
            applied = None
    if d.status != "approved":
        return {"ok": False, "resolved": resolved, "rule": None,
                "text": f"#{decision_id} is {d.status}, so there is nothing to allow from now on."}
    head = f"#{decision_id} approved" + (" and applied" if applied else (", but applying it failed"
                                                                        if applied is False else ""))
    fp, why = classify(d)
    if fp is None:
        return {"ok": True, "resolved": resolved, "applied": applied, "rule": None, "reason": why,
                "text": f"{head}. I will keep asking about this kind: {why}."}
    try:
        rule = add_rule(fp, via=via, decision_id=decision_id)
    except ValueError as exc:
        return {"ok": True, "resolved": resolved, "applied": applied, "rule": None, "reason": str(exc),
                "text": f"{head}. Not made standing: {exc}."}
    return {"ok": True, "resolved": resolved, "applied": applied, "rule": rule, "reason": "",
            "text": f"{head}. From now on I approve {fp.label} automatically and tell you. "
                    "Revoke it under Settings, Approvals."}


def offer_text(decision: db.Decision | None) -> str:
    """The one-line "always" offer for an approval message ('' when this kind can never be standing)."""
    if decision is None or not eligible(decision):
        return ""
    return f" Reply YES ALWAYS {decision.id} to allow this kind from now on."


# -- admin tools -------------------------------------------------------------------------

def _tool_list(ctx: Any, a: dict[str, Any]) -> Any:
    rows = list_rules(include_revoked=bool(a.get("include_revoked")))
    return [{k: r.get(k) for k in ("id", "label", "action_type", "target", "created_at", "created_via", "uses",
                                   "last_used_at", "revoked_at")} for r in rows] or "no standing approvals"


def _tool_revoke(ctx: Any, a: dict[str, Any]) -> Any:
    from assistant.tools import ToolError

    rid = int(a["rule_id"])
    if get_rule(rid) is None:
        raise ToolError(f"no standing approval #{rid}")
    return {"revoked": revoke_rule(rid, via=f"assistant:{getattr(ctx, 'agent', 'main')}")}


def register_tools(register: Callable[..., None]) -> None:
    from assistant.tools import Tool, _obj

    register(Tool("approval_rules_list",
                  "List the owner's standing approvals ('always allow this kind' rules): what each covers, when it "
                  "was made and how often it was used.",
                  _obj({"include_revoked": {"type": "boolean"}}), _tool_list, owner="assistant.approvals"))
    register(Tool("approval_rule_revoke",
                  "Revoke one standing approval by id (when the owner asks, or it no longer looks right). Revoking "
                  "only ever makes you ask more; you cannot create rules - only he can.",
                  _obj({"rule_id": {"type": "integer"}}, ["rule_id"]), _tool_revoke, owner="assistant.approvals",
                  side_effects=True))
