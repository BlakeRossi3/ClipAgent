"""Phone the owner: Twilio voice calls + SMS, approval flow and webhook ``router``.

Public API (see CLAUDE.md):

* :func:`call` / :func:`sms` - send *now* (no quiet-hours policy), one ``notifications`` row each.
* :func:`notify` - policy-aware send: urgent -> call with SMS fallback, else SMS;
  quiet hours queue non-urgent messages; urgent problem calls are de-duplicated.
* :func:`request_approval` - create a ``decisions`` row and phone the owner for a keypad answer.
* :func:`handle_decision` - resolve a decision (first answer wins) and apply it via
  ``strategist.apply_decision`` when approved.
* :func:`send_queued` - deliver queued ``notifications`` rows (e.g. ``budget_cap`` from budget.py).
* :func:`daily_summary` - plain-code spoken summary of the day, sent as a call.
* :data:`router` - FastAPI routes under ``/twilio`` (voice, gather, say, status, sms).

DRY_RUN or unconfigured Twilio: the ``notifications`` row gets status ``dry_run`` and nothing
is sent. The Twilio SDK is imported lazily; tests inject a fake with :func:`set_client`.

Webhooks validate ``X-Twilio-Signature`` against ``settings.public_base_url`` + path + query
(the URL exactly as Twilio requested it, so this works behind a reverse proxy / tunnel).
Without an auth token they only work in DRY_RUN; otherwise they answer 403.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import time as dtime
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import Response

import budget
import config
import db

log = logging.getLogger(__name__)

MODULE = "notify"

# ---------------------------------------------------------------------------
# Tunables. Quiet hours, the urgent-call de-dup window and the Twilio voice are
# Settings (QUIET_HOURS_START/END, URGENT_CALL_DEDUP_HOURS, TWILIO_VOICE/LANGUAGE).
# ---------------------------------------------------------------------------

# Problem kinds whose *calls* ring even during quiet hours (de-duplicated). Producers:
# budget.py budget_cap, youtube_auth.py youtube_auth, analytics.py youtube_claim (all queued as
# calls). SMS kinds (youtube_quota, youtube_private_lock, job_error, weekly_report, ...) wait
# for the morning.
URGENT_KINDS = frozenset({"budget_cap", "youtube_auth", "youtube_claim"})
GATHER_TIMEOUT_SEC = 8
GATHER_MAX_ATTEMPTS = 3             # prompts per approval call before falling back to SMS
RING_TIMEOUT_SEC = 25               # how long the phone rings before "no-answer"
MAX_SPOKEN_CHARS = 700              # ~45 s of speech
MAX_SMS_CHARS = 1500                # Twilio hard limit is 1600
# Backstop against call loops / SMS storms: at most this many calls+texts reach the owner's phone
# per local day (sent or attempted). Further sends are recorded as ``skipped`` with a warning event.
MAX_NOTIFICATIONS_PER_DAY = 25

_MACHINE_PREFIXES = ("machine", "fax")
_FAILED_CALL_STATUSES = {"busy", "no-answer", "failed", "canceled"}

_client: Any = None


def set_client(client: Any | None) -> None:
    """Inject a Twilio client (tests); ``None`` = build ``twilio.rest.Client`` from settings."""
    global _client
    _client = client


def _get_client(settings: config.Settings) -> Any:
    if _client is not None:
        return _client
    from twilio.rest import Client  # heavy/optional import, only when actually sending

    return Client(settings.twilio_account_sid, settings.twilio_auth_token)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def normalize_phone(raw: str | None) -> str:
    """Best-effort E.164 (NANP default): ``(555) 123-4567`` -> ``+15551234567``."""
    if not raw:
        return ""
    raw = raw.strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""
    if raw.startswith("+"):
        return "+" + digits
    if raw.startswith("00"):
        return "+" + digits[2:]
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits


def _strip_channel(number: str | None) -> str:
    """``whatsapp:+1555...`` -> ``+1555...`` (WhatsApp webhooks prefix every address)."""
    s = (number or "").strip()
    return s.split(":", 1)[1] if s.lower().startswith("whatsapp:") else s


def _is_owner(number: str | None) -> bool:
    owner = normalize_phone(config.get_settings().owner_phone)
    return bool(owner) and normalize_phone(_strip_channel(number)) == owner


def _mask(number: str | None) -> str:
    n = normalize_phone(number)
    return f"***{n[-4:]}" if len(n) >= 4 else "unknown"


def _parse_hhmm(value: Any) -> dtime:
    if isinstance(value, dtime):
        return value
    h, m = str(value).split(":")
    return dtime(int(h), int(m))


def in_quiet_hours() -> bool:
    """True when local time (settings.timezone) is inside the quiet-hours window."""
    s = config.get_settings()
    start, end = _parse_hhmm(s.quiet_hours_start), _parse_hhmm(s.quiet_hours_end)
    if start == end:
        return False
    now = db.local_now().time().replace(tzinfo=None)
    if start < end:
        return start <= now < end
    return now >= start or now < end


def _base_url() -> str:
    return config.get_settings().public_base_url.rstrip("/")


def _url(path: str, **query: Any) -> str:
    q = {k: v for k, v in query.items() if v is not None}
    return f"{_base_url()}{path}" + (f"?{urlencode(q)}" if q else "")


_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]


def _money_words(amount: float) -> str:
    cents_total = int(round(amount * 100))
    dollars, cents = divmod(cents_total, 100)
    if dollars == 0:
        return f"{cents} cent" + ("" if cents == 1 else "s")
    d = f"{dollars} dollar" + ("" if dollars == 1 else "s")
    return d if cents == 0 else f"{d} {cents}"


def spoken_number(n: int | float) -> str:
    """Round big numbers the way people say them: 12345 -> '12.3 thousand'."""
    n = int(round(n))
    sign = "minus " if n < 0 else ""
    a = abs(n)
    if a >= 1_000_000:
        s = f"{a / 1_000_000:.1f}".rstrip("0").rstrip(".") + " million"
    elif a >= 10_000:
        s = f"{a / 1000:.1f}".rstrip("0").rstrip(".") + " thousand"
    else:
        s = str(a)
    return sign + s


_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_MONEY_RE = re.compile(r"\$\s?(\d+(?:,\d{3})*(?:\.\d+)?)")


def speakable(text: str, max_chars: int = MAX_SPOKEN_CHARS) -> str:
    """Make ``text`` safe and pleasant for text-to-speech.

    URLs become "a link" (never read aloud), money becomes words, symbols are spelled
    out or dropped, whitespace is collapsed, and long text is cut at a sentence boundary.
    """
    t = _URL_RE.sub("a link", text or "")
    t = _MONEY_RE.sub(lambda m: _money_words(float(m.group(1).replace(",", ""))), t)
    t = re.sub(r"(\d)\s?%", r"\1 percent", t)
    t = re.sub(r"#(\d+)", r"number \1", t)          # "#12" -> "number 12"
    t = re.sub(r"#(\w+)", r"\1", t)                 # hashtags: drop the '#'
    replacements = {"&": " and ", "@": " at ", "+": " plus ", "=": " equals ", "~": " about ",
                    "->": " to ", "=>": " to ", "/": " ", "\\": " ", "_": " ", "|": ", ", "\n": ". "}
    for k in ("->", "=>"):
        t = t.replace(k, replacements[k])
    for k, v in replacements.items():
        if len(k) == 1:
            t = t.replace(k, v)
    t = re.sub(r"[*<>\[\]{}^`\"]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)
    t = re.sub(r"([.!?])(\s*[.])+", r"\1", t)
    if len(t) > max_chars:
        cut = t[:max_chars]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        cut = cut[: end + 1] if end > max_chars // 3 else cut.rsplit(" ", 1)[0] + "."
        t = cut + " More details are on the dashboard."
    return t


def _sms_text(text: str) -> str:
    t = (text or "").strip()
    return t if len(t) <= MAX_SMS_CHARS else t[: MAX_SMS_CHARS - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Delivery claims
# ---------------------------------------------------------------------------

def _claim(notification_id: int, *, conn: Any = None) -> bool:
    """Atomic one-shot claim so a row is delivered at most once (send_queued may overlap)."""
    return db.kv_set_if_absent(f"notify:claim:{notification_id}", db.now_iso(), conn=conn)


# ---------------------------------------------------------------------------
# Delivery primitives
# ---------------------------------------------------------------------------

def _new_row(kind: str, text: str, channel: str, decision_id: int | None) -> int:
    with db.transaction() as c:
        nid = db.add_notification(kind, text, channel, decision_id=decision_id, conn=c)
        _claim(nid, conn=c)
    return nid


def _route(kind: str, transport: str | None) -> Any:
    """Texting channel for an ``sms``-channel row (assistant/messaging.py: SMS or WhatsApp)."""
    from assistant import messaging

    return messaging.route_text(kind, transport=transport)


def _deliver(notification_id: int, channel: str, kind: str, text: str, decision_id: int | None,
             transport: str | None = None) -> str:
    """Send an already-claimed row now. Returns the final status: sent | dry_run | failed | skipped | queued.

    ``channel="sms"`` means "a text": :func:`_route` picks SMS or WhatsApp (``MESSAGING_CHANNEL``);
    a WhatsApp text that cannot go out now (24-hour window closed, SMS not usable) is held
    (row back to ``queued``) and :func:`send_queued` sends it after the owner's next message.
    """
    s = config.get_settings()
    route = None
    if channel == "sms":
        try:
            route = _route(kind, transport)
        except Exception:  # routing must never stop a text: plain SMS as before
            log.exception("texting route failed; using SMS")
    if s.dry_run or not s.twilio_configured:
        why = "DRY_RUN" if s.dry_run else "Twilio not configured"
        db.update_notification(notification_id, status="dry_run", error=None)
        db.log_event(MODULE, f"[dry run] would {channel} owner ({kind})", data={
            "notification_id": notification_id, "reason": why, "decision_id": decision_id, "text": text[:300],
            **({"transport": route.transport, "route": route.reason, "would_hold": route.hold} if route else {})})
        return "dry_run"
    if route is not None and route.hold:
        from assistant import messaging

        messaging.hold(notification_id, route.reason)
        return "queued"
    day_start, day_end = db.local_day_bounds()
    used = db.count_notifications_between(day_start, day_end, status=("sent", "failed"), channels=("call", "sms"))
    if used >= MAX_NOTIFICATIONS_PER_DAY:
        db.update_notification(notification_id, status="skipped",
                               error=f"daily cap of {MAX_NOTIFICATIONS_PER_DAY} calls/texts reached")
        if db.kv_set_if_absent(f"notify:daily_cap:{db.local_today().isoformat()}", db.now_iso()):
            db.log_event(MODULE, f"daily cap of {MAX_NOTIFICATIONS_PER_DAY} calls/texts reached; "
                         "further notifications today are only recorded", level="warning",
                         data={"notification_id": notification_id, "kind": kind})
        return "skipped"
    try:
        client = _get_client(s)
        if channel == "call":
            url = (_url(f"/twilio/voice/{decision_id}") if decision_id is not None
                   else _url(f"/twilio/say/{notification_id}"))
            res = client.calls.create(
                to=normalize_phone(s.owner_phone), from_=normalize_phone(s.twilio_from_number),
                url=url, method="POST",
                status_callback=_url("/twilio/status", notification_id=notification_id),
                status_callback_method="POST",
                # Approvals: detect quickly so a machine never "answers"; info calls wait
                # for the beep so the message lands in voicemail.
                machine_detection="Enable" if decision_id is not None else "DetectMessageEnd",
                timeout=RING_TIMEOUT_SEC,
            )
            if decision_id is not None:
                db.set_decision_call_sid(decision_id, res.sid)
        else:
            res = _send_text(client, s, notification_id, text, route)
    except Exception as exc:  # Twilio REST errors, network, misconfiguration
        if channel == "sms" and route is not None and _text_failed_now(notification_id, kind, exc, route):
            return "queued"
        db.update_notification(notification_id, status="failed", error=str(exc)[:500])
        db.log_event(MODULE, f"{channel} to owner failed ({kind})", level="error",
                     data={"notification_id": notification_id, "error": str(exc)[:500]})
        return "failed"
    db.update_notification(notification_id, status="sent", sid=getattr(res, "sid", None))
    db.log_event(MODULE, f"{channel} sent to owner ({kind})",
                 data={"notification_id": notification_id, "sid": getattr(res, "sid", None),
                       "decision_id": decision_id})
    return "sent"


# ---------------------------------------------------------------------------
# Texting transports (SMS / WhatsApp) - see assistant/messaging.py
# ---------------------------------------------------------------------------

def _callback_ok() -> bool:
    """Twilio needs a public https host for status callbacks; skip them for localhost setups."""
    from urllib.parse import urlparse

    p = urlparse(_base_url())
    host = (p.hostname or "").lower()
    return p.scheme == "https" and bool(host) and "." in host and not host.endswith(".local")


def _send_text(client: Any, s: config.Settings, notification_id: int, text: str, route: Any) -> Any:
    transport = route.transport if route is not None else "sms"
    to, from_ = normalize_phone(s.owner_phone), normalize_phone(s.twilio_from_number)
    if transport == "whatsapp":
        to, from_ = f"whatsapp:{to}", f"whatsapp:{normalize_phone(_strip_channel(s.whatsapp_from))}"
    kw: dict[str, Any] = {"to": to, "from_": from_, "body": _sms_text(text)}
    if _callback_ok():
        kw["status_callback"] = _url("/twilio/message-status", notification_id=notification_id)
    res = client.messages.create(**kw)
    try:
        from assistant import messaging

        messaging.set_transport(notification_id, transport)
        messaging.release(notification_id)
    except Exception:
        log.exception("could not record the texting transport")
    if transport != "sms" or (route is not None and route.reason != "auto: SMS works"):
        db.log_event(MODULE, f"text goes by {transport}", data={"notification_id": notification_id,
                                                               "why": getattr(route, "reason", "")})
    return res


def _text_failed_now(notification_id: int, kind: str, exc: Exception, route: Any) -> bool:
    """A WhatsApp send refused at once (63016 window / 63015 not joined): hold it. True = handled."""
    from assistant import messaging

    try:
        code = int(getattr(exc, "code", None) or 0)
    except (TypeError, ValueError):
        code = 0
    if route.transport == "whatsapp" and code in messaging.WA_WINDOW_CODES | messaging.WA_NOT_JOINED_CODES:
        messaging.record_whatsapp_error(code)
        if code in messaging.WA_WINDOW_CODES:   # our window clock was wrong: treat it as closed
            db.kv_delete(messaging.K_WA_LAST_IN)
        messaging.hold(notification_id, f"WhatsApp refused it (error {code})")
        return True
    if route.transport == "sms" and code in messaging.SMS_BLOCK_CODES:
        messaging.record_sms_error(code)
    return False


def call(text: str, *, kind: str = "call", decision_id: int | None = None) -> int:
    """Phone the owner now (no quiet-hours policy). Returns the notification id.

    With ``decision_id`` the call runs the approval flow (``/twilio/voice/{decision_id}``),
    otherwise it reads ``text`` (``/twilio/say/{notification_id}``).
    """
    nid = _new_row(kind, text, "call", decision_id)
    _deliver(nid, "call", kind, text, decision_id)
    return nid


def sms(text: str, *, kind: str = "sms", decision_id: int | None = None, transport: str | None = None) -> int:
    """Text the owner now (no quiet-hours policy). Returns the notification id.

    The pipe (SMS or WhatsApp) follows ``MESSAGING_CHANNEL`` unless ``transport`` forces one.
    """
    nid = _new_row(kind, text, "sms", decision_id)
    _deliver(nid, "sms", kind, text, decision_id, transport)
    return nid


def _status_of(nid: int) -> str:
    row = db.get_notification(nid)
    return row.status if row else "failed"


def _approval_sms_text(d: db.Decision) -> str:
    summary = d.summary if len(d.summary) <= 600 else d.summary[:597].rstrip() + "..."
    return f"Approval needed (#{d.id}): {summary}\nReply YES {d.id} or NO {d.id}."


def _fallback_sms(notification_id: int, kind: str, text: str, decision_id: int | None, reason: str) -> int | None:
    """SMS after a call did not reach a human. At most once per call / decision."""
    if decision_id is not None:
        d = _current_decision(decision_id)
        if d is None or d.status != "pending":
            return None
        if not db.kv_set_if_absent(f"notify:fallback:decision:{decision_id}", db.now_iso()):
            return None
        db.log_event(MODULE, "approval call fell back to SMS", level="warning",
                     data={"decision_id": decision_id, "reason": reason})
        return sms(_approval_sms_text(d), kind="approval", decision_id=decision_id)
    if not db.kv_set_if_absent(f"notify:fallback:{notification_id}", db.now_iso()):
        return None
    db.log_event(MODULE, f"call fell back to SMS ({kind})", level="warning",
                 data={"notification_id": notification_id, "reason": reason})
    return sms(f"Missed call from your Shorts agent: {text}", kind=kind)


def _recently_called(kind: str) -> bool:
    last = db.kv_get(f"notify:last_call:{kind}")
    if not last:
        return False
    hours = config.get_settings().urgent_call_dedup_hours
    return db.utcnow() - db.parse_iso(last) < timedelta(hours=hours)


def _mark_called(kind: str) -> None:
    db.kv_set(f"notify:last_call:{kind}", db.now_iso())


def _queue(kind: str, text: str, channel: str, decision_id: int | None = None) -> int:
    nid = db.add_notification(kind, text, channel, status="queued", decision_id=decision_id)
    db.log_event(MODULE, f"{channel} queued for after quiet hours ({kind})", data={"notification_id": nid})
    return nid


def _dispatch(channel: str, kind: str, text: str, *, decision_id: int | None = None, urgent: bool = False) -> int:
    """Policy layer: quiet hours, urgent-call de-dup, call -> SMS fallback."""
    urgent_problem = channel == "call" and urgent and kind in URGENT_KINDS
    if urgent_problem and _recently_called(kind):
        db.log_event(MODULE, f"suppressed repeat call for {kind}; texting instead",
                     data={"dedup_hours": config.get_settings().urgent_call_dedup_hours})
        channel, urgent_problem = "sms", False
    if in_quiet_hours() and not urgent_problem:
        return _queue(kind, text, channel, decision_id)
    if channel == "sms":
        return sms(text, kind=kind, decision_id=decision_id)
    nid = call(text, kind=kind, decision_id=decision_id)
    status = _status_of(nid)
    if status in ("sent", "dry_run") and kind in URGENT_KINDS:
        _mark_called(kind)
    if status == "failed":
        _fallback_sms(nid, kind, text, decision_id, "call could not be placed")
    return nid


def notify(kind: str, text: str, *, urgent: bool = False) -> int:
    """Tell the owner something. Urgent: call (SMS fallback); otherwise SMS.

    Inside quiet hours non-urgent messages are queued for :func:`send_queued`; urgent
    calls of :data:`URGENT_KINDS` still go out but at most once per kind per
    ``settings.urgent_call_dedup_hours`` (repeats become SMS). Returns the notification id.
    """
    return _dispatch("call" if urgent else "sms", kind, text, urgent=urgent)


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------

def _current_decision(decision_id: int) -> db.Decision | None:
    """Fetch a decision, expiring it first if its deadline has passed."""
    d = db.get_decision(decision_id)
    if d is not None and d.status == "pending" and d.expires_at and db.parse_iso(d.expires_at) <= db.utcnow():
        db.resolve_decision(decision_id, "expired", via="timeout")
        d = db.get_decision(decision_id)
    return d


def request_approval(kind: str, summary: str, payload: dict, *, expires_hours: int = 48) -> int:
    """Create a pending decision and phone the owner for a keypad answer. Returns the decision id."""
    if kind not in db.DECISION_KINDS:
        log.warning("unknown decision kind %r; storing as 'other'", kind)
        payload = {**(payload or {}), "requested_kind": kind}
        kind = "other"
    expires_at = db.to_iso(db.utcnow() + timedelta(hours=expires_hours))
    decision_id = db.add_decision(kind, summary, payload, expires_at=expires_at)
    db.log_event(MODULE, "approval requested", data={"decision_id": decision_id, "kind": kind, "summary": summary})
    _dispatch("call", "approval", summary, decision_id=decision_id)
    return decision_id


@dataclass(frozen=True)
class DecisionOutcome:
    """What one answer did. ``applied`` is None unless this answer approved the decision."""

    resolved: bool                 # this answer resolved it (first answer wins)
    status: str | None             # decision status afterwards (None = unknown decision)
    applied: bool | None = None    # approved: did strategist.apply_decision save the change?


def decide(decision_id: int, approved: bool, *, via: str) -> DecisionOutcome:
    """Resolve a pending decision (first answer wins); approved -> ``strategist.apply_decision``.

    The outcome says whether the change was actually applied, so the owner is never told
    "applied" when the strategist refused (conflict, invalid proposal) or crashed.
    """
    d = _current_decision(decision_id)
    if d is None:
        db.log_event(MODULE, "answer for unknown decision ignored", level="warning",
                     data={"decision_id": decision_id, "via": via})
        return DecisionOutcome(False, None)
    status = "approved" if approved else "rejected"
    if d.status != "pending" or not db.resolve_decision(decision_id, status, via=via):
        current = db.get_decision(decision_id)
        db.log_event(MODULE, "answer for already-resolved decision ignored", level="warning",
                     data={"decision_id": decision_id, "via": via, "answer": status,
                           "status": current.status if current else None})
        return DecisionOutcome(False, current.status if current else None)
    db.log_event(MODULE, f"decision {decision_id} {status} via {via}",
                 data={"decision_id": decision_id, "kind": d.kind})
    if not approved:
        return DecisionOutcome(True, status)
    applied = False
    try:
        if isinstance(d.payload, dict) and d.payload.get("action") == "assistant_action":
            from assistant import tools as assistant_tools  # late import: assistant imports notify

            applied = bool(assistant_tools.apply_approved_decision(decision_id))
        else:
            import strategist  # late import: strategist imports notify

            applied = bool(strategist.apply_decision(decision_id))
        db.log_event(MODULE, f"decision {decision_id} applied" if applied
                     else f"decision {decision_id} approved but apply_decision returned False",
                     level="info" if applied else "warning", data={"decision_id": decision_id})
    except Exception as exc:
        db.log_event(MODULE, f"applying decision {decision_id} failed", level="error",
                     data={"decision_id": decision_id, "error": repr(exc)[:500]})
    return DecisionOutcome(True, status, applied)


def handle_decision(decision_id: int, approved: bool, *, via: str) -> bool:
    """Resolve a pending decision (first answer wins). Approved -> ``strategist.apply_decision``.

    Returns True if this answer resolved the decision; False if it was unknown, already
    resolved or expired (nothing is re-applied). :func:`decide` also reports whether it applied.
    """
    return decide(decision_id, approved, via=via).resolved


# ---------------------------------------------------------------------------
# Queue + daily summary
# ---------------------------------------------------------------------------

def send_queued() -> int:
    """Deliver queued ``notifications`` rows. Safe to run every few minutes; never double-sends.

    Quiet hours hold back everything except calls of :data:`URGENT_KINDS` (which are
    de-duplicated: a repeat within the window is texted instead). Rows tied to a decision
    that is no longer pending are marked ``skipped``. Returns rows delivered (sent or dry run).
    """
    delivered = 0
    quiet = in_quiet_hours()
    for row in db.list_notifications("queued", limit=None):
        urgent_call = row.channel == "call" and row.kind in URGENT_KINDS
        if quiet and not urgent_call:
            continue
        if row.channel == "sms" and _still_held(row):
            continue
        if row.decision_id is not None:
            d = _current_decision(row.decision_id)
            if d is None or d.status != "pending":
                if _claim(row.id):
                    db.update_notification(row.id, status="skipped",
                                           error=f"decision {row.decision_id} no longer pending")
                continue
        if not _claim(row.id):
            continue
        if row.channel == "log":
            db.update_notification(row.id, status="sent")
            db.log_event(MODULE, f"notification ({row.kind})", data={"text": row.body[:500]})
            delivered += 1
            continue
        if urgent_call and _recently_called(row.kind):
            db.update_notification(row.id, status="skipped", error="repeat call suppressed; texted instead")
            _dispatch("sms", row.kind, row.body)
            delivered += 1
            continue
        status = _deliver(row.id, row.channel, row.kind, row.body, row.decision_id)
        if status in ("sent", "dry_run"):
            delivered += 1
            if row.channel == "call" and row.kind in URGENT_KINDS:
                _mark_called(row.kind)
        elif row.channel == "call":
            _fallback_sms(row.id, row.kind, row.body, row.decision_id, "call could not be placed")
    return delivered


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{spoken_number(n)} {word if n == 1 else (plural or word + 's')}"


def compose_daily_summary() -> str:
    """Build the spoken daily summary from the DB (no LLM). ~20-30 seconds when read."""
    s = config.get_settings()
    today = db.local_today()
    start, end = db.local_day_bounds(today)
    parts: list[str] = [f"Hi {s.owner_name}, here's your Shorts update for today."]

    posted = db.count_uploads_on(today)
    if posted:
        parts.append(f"We posted {_plural(posted, 'short')} today"
                     + (", in dry run mode, so nothing actually went to YouTube." if s.dry_run else "."))
    else:
        parts.append("No shorts were posted today.")

    week_ago = today - timedelta(days=7)
    perf = db.clip_performance(week_ago) or db.clip_performance()
    if perf:
        top = max(perf, key=lambda r: (r["views"], r["clip_id"]))
        if top["views"] > 0:
            title = speakable(top["title"] or "an untitled short", 120).rstrip(".")
            parts.append(f"Top performer is \"{title}\", with {_plural(top['views'], 'view')}.")

    ch = {c.day: c for c in db.list_channel_stats(limit=10)}
    yday = (today - timedelta(days=1)).isoformat()
    cur, prev = ch.get(today.isoformat()), ch.get(yday)
    if cur and prev and cur.total_views is not None and prev.total_views is not None:
        dv = cur.total_views - prev.total_views
        line = (f"The channel gained {_plural(dv, 'view')} since yesterday" if dv >= 0
                else "Channel views are down slightly since yesterday")
        if cur.subscribers is not None and prev.subscribers is not None:
            ds = cur.subscribers - prev.subscribers
            if ds > 0:
                line += f", and {_plural(ds, 'new subscriber')}"
            elif ds < 0:
                line += f", and lost {_plural(-ds, 'subscriber')}"
            else:
                line += ", with no change in subscribers"
        parts.append(line + ".")

    spent, cap = budget.spent_today(today), budget.daily_cap_usd()
    parts.append(f"AI spend today was {_money_words(spent)} of your {_money_words(cap)} cap.")

    pending = db.list_decisions("pending")
    if pending:
        parts.append(f"{_plural(len(pending), 'approval')} {'is' if len(pending) == 1 else 'are'} "
                     "waiting for you; I'll text the details.")

    problems: list[str] = []
    failed = [u for u in db.list_uploads("failed") if start <= (u.updated_at or "") < end]
    if failed:
        problems.append(_plural(len(failed), "failed upload"))
    claims = sum(1 for r in db.clip_performance() if r["copyright_claim"])
    if claims:
        problems.append(_plural(claims, "copyright claim"))
    errors = db.list_events(level="error", since=start, limit=500)
    if errors and not failed:
        problems.append(_plural(len(errors), "error"))
    if budget.is_paused() and spent < cap:  # paused by hand, not just "cap reached"
        problems.append("AI spending is paused")
    elif spent >= cap:
        problems.append("the AI budget ran out")
    if problems:
        joined = problems[0] if len(problems) == 1 else ", ".join(problems[:-1]) + " and " + problems[-1]
        parts.append(f"Heads up: {joined}. Check the dashboard.")
    else:
        parts.append("No problems today.")
    parts.append("Have a good night.")
    return " ".join(parts)


def daily_summary() -> str:
    """Compose the day's summary and phone it to the owner (SMS fallback). Returns the text."""
    text = compose_daily_summary()
    _dispatch("call", "daily_summary", text)
    return text


def status_line() -> str:
    """Short SMS status: uploads today, spend vs cap, pending decisions, pause state."""
    import strategy as strategy_mod

    try:
        cap_uploads: Any = strategy_mod.upload_cap()
    except Exception:
        cap_uploads = config.get_settings().max_uploads_per_day
    pending = db.list_decisions("pending")
    pend = ("none" if not pending else
            ", ".join(f"#{d.id}" for d in pending[:5]) + (" ..." if len(pending) > 5 else ""))
    paused = "PAUSED" if budget.is_paused() else "running"
    dry = " [dry run]" if config.get_settings().dry_run else ""
    return (f"Uploads today {db.count_uploads_on()}/{cap_uploads}. "
            f"AI spend ${budget.spent_today():.2f} of ${budget.daily_cap_usd():.2f}. "
            f"Pending: {pend}. LLM {paused}.{dry}")


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

def _still_held(row: db.Notification) -> bool:
    """A held text waits until the route no longer says hold (window reopened, SMS approved...)."""
    try:
        from assistant import messaging

        if not messaging.is_held(row.id):
            return False
        if messaging.expire_if_stale(row.id):
            return True
        return _route(row.kind, None).hold
    except Exception:
        log.exception("held-text check failed")
        return False


router = APIRouter(prefix="/twilio", tags=["twilio"])

HELP_TEXT = ("Commands: YES <id> / NO <id> to answer an approval, "
             "STATUS, PAUSE (stop AI spending), RESUME, UNDO (assistant's last code change), STOP STERLING, "
             "START STERLING, HELP.")


def _xml(twiml: Any) -> Response:
    return Response(content=str(twiml), media_type="application/xml")


def _voice() -> Any:
    from twilio.twiml.voice_response import VoiceResponse

    return VoiceResponse()


def _say(resp: Any, text: str) -> None:
    s = config.get_settings()
    resp.say(text, voice=s.twilio_voice, language=s.twilio_language)


def _public_url(request: Request) -> str:
    """The URL as Twilio requested it: public base + path (minus any ASGI root_path) + query."""
    path = request.url.path
    root = request.scope.get("root_path") or ""
    if root and path.startswith(root):
        path = path[len(root):]
    query = request.url.query
    return _base_url() + path + (f"?{query}" if query else "")


_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient", "testserver"}
_FORWARD_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip", "x-forwarded-host", "cf-connecting-ip",
                    "true-client-ip")


def _is_direct_local(request: Request) -> bool:
    """Made on this machine to 127.0.0.1, not relayed by a reverse proxy / tunnel (those also
    connect from 127.0.0.1 but add forwarding headers and/or the public Host name)."""
    client = request.client.host if request.client else ""
    if client not in _LOCAL_HOSTS:
        return False
    if any(h in request.headers for h in _FORWARD_HEADERS):
        return False
    host = (request.headers.get("host") or "").strip().lower()
    hostname = host[1:host.index("]")] if host.startswith("[") and "]" in host else host.rsplit(":", 1)[0]
    return hostname in _LOCAL_HOSTS


async def _twilio_params(request: Request) -> dict[str, str]:
    """Read the form and enforce ``X-Twilio-Signature``. Raises 403 when it does not verify.

    Without ``TWILIO_AUTH_TOKEN`` nothing can be verified: unsigned posts are accepted only in
    DRY_RUN *and* only from this machine (local testing with curl), never from the internet -
    an unsigned ``YES <id>`` would otherwise approve a strategy change.
    """
    s = config.get_settings()
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    token = s.twilio_auth_token
    if not token:
        if s.dry_run and _is_direct_local(request):
            return params
        db.log_event(MODULE, "webhook refused: no TWILIO_AUTH_TOKEN to verify signatures", level="error",
                     data={"path": request.url.path})
        raise HTTPException(status_code=403, detail="webhook signature cannot be verified")
    from twilio.request_validator import RequestValidator

    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature or not RequestValidator(token).validate(_public_url(request), params, signature):
        db.log_event(MODULE, "webhook rejected: bad Twilio signature", level="warning",
                     data={"path": request.url.path, "url": _public_url(request)})
        raise HTTPException(status_code=403, detail="invalid Twilio signature")
    return params


def _is_machine(answered_by: str | None) -> bool:
    return bool(answered_by) and answered_by.lower().startswith(_MACHINE_PREFIXES)


def _gather(resp: Any, decision_id: int, attempt: int, text: str) -> None:
    action = _url(f"/twilio/gather/{decision_id}", attempt=attempt)
    g = resp.gather(num_digits=1, timeout=GATHER_TIMEOUT_SEC, action=action, method="POST")
    _say(g, text)
    resp.redirect(action, method="POST")  # no key pressed -> same handler without Digits


_PROMPT = "Press 1 to approve, 2 to reject, or 9 to hear this again."


def _approval_prompt(d: db.Decision) -> str:
    return speakable(f"I need your approval on request number {d.id}. {d.summary}") + " " + _PROMPT


@router.post("/voice/{decision_id}")
async def voice_webhook(decision_id: int, request: Request) -> Response:
    """TwiML for an approval call: read the summary, then gather one digit."""
    p = await _twilio_params(request)
    resp = _voice()
    s = config.get_settings()
    if p.get("CallSid"):
        db.kv_set(f"notify:answered_by:{p['CallSid']}", p.get("AnsweredBy") or "")
    if _is_machine(p.get("AnsweredBy")):
        _say(resp, f"Hi {s.owner_name}, this is your Shorts agent. I have an approval request. "
                   "I'll text you the details.")
        resp.hangup()
        n = db.get_notification_by_sid(p.get("CallSid", "")) if p.get("CallSid") else None
        _fallback_sms(n.id if n else 0, "approval", "", decision_id, f"answered by {p.get('AnsweredBy')}")
        return _xml(resp)
    if p.get("To") and not _is_owner(p.get("To")):
        db.log_event(MODULE, "approval call to non-owner number refused", level="warning",
                     data={"decision_id": decision_id, "to": _mask(p.get("To"))})
        _say(resp, "Sorry, this call cannot be used for approvals. Goodbye.")
        resp.hangup()
        return _xml(resp)
    d = _current_decision(decision_id)
    if d is None or d.status != "pending":
        state = "no longer available" if d is None else f"already {d.status}"
        _say(resp, f"Hi {s.owner_name}, this is your Shorts agent. That request is {state}, "
                   "so there is nothing to do. Goodbye.")
        resp.hangup()
        return _xml(resp)
    _gather(resp, decision_id, 1, f"Hi {s.owner_name}, this is your Shorts agent. " + _approval_prompt(d))
    return _xml(resp)


@router.post("/gather/{decision_id}")
async def gather_webhook(decision_id: int, request: Request, attempt: int = 1) -> Response:
    """Keypad answer for an approval call. 1 approve, 2 reject, 9 repeat; retries, then SMS."""
    p = await _twilio_params(request)
    resp = _voice()
    call_sid = p.get("CallSid", "")
    d = db.get_decision(decision_id)
    to = p.get("To") or p.get("Called")
    refuse = None
    if not _is_owner(to):
        refuse = "call was not to the owner number"
    elif d is not None and (not d.call_sid or not call_sid or call_sid != d.call_sid):
        refuse = "call sid does not match the approval call"
    elif _is_machine(p.get("AnsweredBy")) or _is_machine(db.kv_get(f"notify:answered_by:{call_sid}")):
        refuse = "call was answered by a machine"
    if refuse:
        db.log_event(MODULE, "keypad answer refused", level="warning",
                     data={"decision_id": decision_id, "reason": refuse, "to": _mask(to)})
        _say(resp, "Sorry, I can't accept an answer on this call. Goodbye.")
        resp.hangup()
        return _xml(resp)

    d = _current_decision(decision_id)
    if d is None or d.status != "pending":
        state = "no longer available" if d is None else f"already {d.status}"
        _say(resp, f"That request is {state}, so nothing was changed. Goodbye.")
        resp.hangup()
        return _xml(resp)

    digits = (p.get("Digits") or "").strip()
    if digits in ("1", "2"):
        approved = digits == "1"
        out = decide(decision_id, approved, via="call")
        if out.resolved:
            if not approved:
                _say(resp, "Rejected. Nothing will change. Thanks, goodbye.")
            elif out.applied:
                _say(resp, "Approved, and the change has been applied. Thanks, goodbye.")
            else:
                _say(resp, "Approved, but I could not apply the change automatically, so nothing has "
                           "changed yet. The details are on the dashboard. Goodbye.")
        else:
            current = db.get_decision(decision_id)
            _say(resp, f"That request was already {current.status if current else 'handled'}, "
                       "so nothing was changed. Goodbye.")
        resp.hangup()
        return _xml(resp)
    if attempt >= GATHER_MAX_ATTEMPTS:
        _say(resp, "I'll send you a text instead. Goodbye.")
        resp.hangup()
        n = db.get_notification_by_sid(call_sid) if call_sid else None
        _fallback_sms(n.id if n else 0, "approval", "", decision_id,
                      "no answer on keypad" if not digits else "invalid keypad answers")
        return _xml(resp)
    if digits == "9":
        _gather(resp, decision_id, attempt + 1, _approval_prompt(d))
    elif digits:
        _gather(resp, decision_id, attempt + 1, "Sorry, that's not an option. " + _PROMPT)
    else:
        _gather(resp, decision_id, attempt + 1, "I didn't hear a key press. " + _PROMPT)
    return _xml(resp)


@router.post("/say/{notification_id}")
async def say_webhook(notification_id: int, request: Request) -> Response:
    """TwiML for an informational call: read the stored notification text."""
    p = await _twilio_params(request)
    resp = _voice()
    n = db.get_notification(notification_id)
    s = config.get_settings()
    if n is None or n.channel != "call" or (p.get("To") and not _is_owner(p.get("To"))):
        _say(resp, "Sorry, there's no message here. Goodbye.")
        resp.hangup()
        return _xml(resp)
    _say(resp, f"Hi {s.owner_name}, this is your Shorts agent. " + speakable(n.body))
    if not _is_machine(p.get("AnsweredBy")):
        resp.pause(length=1)
        _say(resp, "That's all. Goodbye.")
    resp.hangup()
    return _xml(resp)


@router.post("/status")
async def status_webhook(request: Request, notification_id: int | None = None) -> Response:
    """Call status callback: unanswered / machine-answered calls fall back to SMS."""
    p = await _twilio_params(request)
    call_sid, status = p.get("CallSid", ""), (p.get("CallStatus") or "").lower()
    answered_by = p.get("AnsweredBy") or db.kv_get(f"notify:answered_by:{call_sid}") or ""
    n = db.get_notification(notification_id) if notification_id is not None else None
    if n is None and call_sid:
        n = db.get_notification_by_sid(call_sid)
    decision = db.get_decision_by_call_sid(call_sid) if call_sid else None
    decision_id = n.decision_id if n is not None else (decision.id if decision else None)
    if n is None and decision is None:
        db.log_event(MODULE, "status callback for unknown call", level="warning",
                     data={"call_sid": call_sid, "status": status})
        return _xml(_voice())
    db.log_event(MODULE, f"call {status or 'update'}", data={
        "notification_id": n.id if n else None, "decision_id": decision_id,
        "call_sid": call_sid, "answered_by": answered_by})
    reason = None
    if status in _FAILED_CALL_STATUSES:
        reason = status
        if n is not None:
            db.update_notification(n.id, status="failed", sid=n.sid or call_sid, error=f"call {status}")
    elif status == "completed" and _is_machine(answered_by):
        reason = f"answered by {answered_by}"
    elif status == "completed" and decision_id is not None:
        reason = "call ended without an answer"  # no-op if the decision was resolved
    if reason:
        _fallback_sms(n.id if n else 0, n.kind if n else "approval", n.body if n else "", decision_id, reason)
    return _xml(_voice())


@router.post("/message-status")
async def message_status_webhook(request: Request, notification_id: int | None = None) -> Response:
    """Status callback for texts (SMS and WhatsApp). Learns which pipe works:

    * SMS ``undelivered``/``failed`` with 30034 (unregistered 10DLC) -> SMS marked blocked; the text
      is re-queued once so ``auto`` resends it on WhatsApp;
    * SMS ``delivered`` -> SMS marked working;
    * WhatsApp 63016 (window closed) / 63015 (not joined) -> the text is held for his next message.
    """
    p = await _twilio_params(request)
    status = (p.get("MessageStatus") or p.get("SmsStatus") or "").lower()
    try:
        code = int(p.get("ErrorCode") or 0)
    except ValueError:
        code = 0
    via_wa = (p.get("To") or "").lower().startswith("whatsapp:")
    n = db.get_notification(notification_id) if notification_id is not None else None
    if n is None and p.get("MessageSid"):
        n = db.get_notification_by_sid(p["MessageSid"])
    from assistant import messaging

    if status == "delivered":
        if not via_wa:
            messaging.record_sms_ok()
    elif status in ("undelivered", "failed"):
        db.log_event(MODULE, f"{'WhatsApp' if via_wa else 'SMS'} {status} (error {code or '?'})", level="warning",
                     data={"notification_id": n.id if n else None, "code": code, "sid": p.get("MessageSid")})
        if via_wa and code in messaging.WA_WINDOW_CODES | messaging.WA_NOT_JOINED_CODES:
            messaging.record_whatsapp_error(code, sid=p.get("MessageSid"))
            if code in messaging.WA_WINDOW_CODES:
                db.kv_delete(messaging.K_WA_LAST_IN)
            if n is not None:
                messaging.requeue_once(n.id, f"WhatsApp error {code}; resent after your next WhatsApp message")
        elif not via_wa and code in messaging.SMS_BLOCK_CODES:
            messaging.record_sms_error(code, sid=p.get("MessageSid"))
            if n is not None and messaging.whatsapp_configured():
                messaging.requeue_once(n.id, f"SMS blocked (error {code}); resending by WhatsApp")
        elif n is not None:
            db.update_notification(n.id, status="failed", sid=n.sid, error=f"{status} (error {code})")
    return Response(status_code=204)


_ANSWER_RE = re.compile(r"^\s*(yes|y|approve|no|n|reject)\b[\s#:.,-]*([0-9]{1,9})?\s*[.!]?\s*$",
                        re.IGNORECASE | re.ASCII)


def handle_sms_command(body: str) -> str:
    """Execute an owner SMS command and return the reply text."""
    text = (body or "").strip()
    word = text.split()[0].upper() if text else ""
    m = _ANSWER_RE.match(text)
    if m:
        approved = m.group(1).lower() in ("yes", "y", "approve")
        if not m.group(2):
            # A bare YES is never bound to "whatever is pending now": the request the owner is
            # answering may have expired and a different one taken its place.
            pending = db.list_decisions("pending")
            pending = [d for d in pending if (c := _current_decision(d.id)) and c.status == "pending"]
            if not pending:
                return "Nothing is waiting for approval."
            ids = ", ".join(f"#{d.id}" for d in pending[:5])
            word = "YES" if approved else "NO"
            return (f"Please include the request number, e.g. {word} {pending[0].id}. "
                    f"Pending: {ids}. Nothing was changed.")
        decision_id = int(m.group(2))
        out = decide(decision_id, approved, via="sms")
        if out.resolved:
            if not approved:
                return f"#{decision_id} rejected. Nothing changes."
            return (f"#{decision_id} approved and applied." if out.applied else
                    f"#{decision_id} approved, but applying it failed, so nothing changed yet. See the dashboard.")
        d = db.get_decision(decision_id)
        if d is None:
            return f"No request #{decision_id}. Reply STATUS to see pending ones."
        return f"#{decision_id} is already {d.status}" + (f" (via {d.resolved_via})" if d.resolved_via else "") + \
            ". Nothing changed."
    phrase = " ".join(text.upper().split())
    if phrase in ASSISTANT_PHRASES:
        return _assistant_command(phrase)
    if word == "PAUSE":
        budget.set_paused(True, "paused by owner via SMS")
        return "Paused. No AI spending until you text RESUME."
    if word in ("RESUME", "UNPAUSE"):
        budget.set_paused(False, "resumed by owner via SMS")
        return "Resumed. " + status_line()
    if word == "STATUS":
        return status_line()
    return HELP_TEXT


@router.post("/sms")
async def sms_webhook(request: Request, background_tasks: BackgroundTasks) -> Response:
    """Inbound SMS from the owner: YES/NO <id>, PAUSE, RESUME, STATUS, HELP. With the assistant
    enabled, anything else is handed to it in the background (signature, owner-number and
    MessageSid checks above still apply to every message). WhatsApp messages posted here
    (``From=whatsapp:+1...``) are handled exactly like :func:`whatsapp_webhook`."""
    return await _inbound_message(request, background_tasks)


@router.post("/whatsapp")
async def whatsapp_webhook(request: Request, background_tasks: BackgroundTasks) -> Response:
    """Inbound WhatsApp (sandbox "When a message comes in" URL). Same checks and commands as SMS:
    Twilio signature, owner number only (``whatsapp:`` prefix stripped), MessageSid de-dup,
    YES/NO need the request id. Each message reopens the 24-hour window and releases held texts."""
    return await _inbound_message(request, background_tasks)


async def _inbound_message(request: Request, background_tasks: BackgroundTasks) -> Response:
    p = await _twilio_params(request)
    from twilio.twiml.messaging_response import MessagingResponse

    resp = MessagingResponse()
    sender = p.get("From")
    via_wa = (sender or "").strip().lower().startswith("whatsapp:")
    label = "WhatsApp" if via_wa else "SMS"
    if not _is_owner(sender):
        db.log_event(MODULE, f"ignored {label} from non-owner number", level="warning",
                     data={"from": _mask(_strip_channel(sender)), "body": (p.get("Body") or "")[:40]})
        return _xml(resp)
    body = p.get("Body") or ""
    msid = p.get("MessageSid") or p.get("SmsSid") or ""
    if msid and not db.kv_set_if_absent(f"notify:sms_in:{msid}", db.now_iso()):
        db.log_event(MODULE, f"duplicate inbound {label} ignored (replay or Twilio retry)", level="warning",
                     data={"message_sid": msid})
        return _xml(resp)
    if via_wa:
        try:
            from assistant import messaging

            messaging.record_inbound_whatsapp(body)
            background_tasks.add_task(send_queued)   # held texts can go now that the window is open
        except Exception:
            log.exception("could not record the WhatsApp window")
    has_media = (p.get("NumMedia") or "0").strip() not in ("", "0")
    if has_media and _assistant_enabled():
        db.log_event(MODULE, f"owner {label} with attachments handed to the assistant", data={"chars": len(body)})
        background_tasks.add_task(assistant_inbound, body, dict(p))
        return _xml(resp)
    if _assistant_enabled() and not is_pipeline_command(body):
        # Hand off: reply with empty TwiML now (Twilio times out after 15 s), answer in the
        # background - check-in answers in plain code first, else the conversational agent.
        db.log_event(MODULE, f"owner {label} handed to the assistant", data={"chars": len(body)})
        background_tasks.add_task(assistant_inbound, body)
        return _xml(resp)
    reply = handle_sms_command(body)
    if _assistant_enabled() and reply == HELP_TEXT:
        reply = HELP_TEXT + " " + ASSISTANT_HELP
    db.log_event(MODULE, "owner SMS command", data={"command": body[:80], "reply": reply[:200]})
    resp.message(reply)
    return _xml(resp)


ASSISTANT_HELP = ("Check-ins: SNOOZE [2h], QUIET (rest of today), STOP CHECK-INS, START CHECK-INS. "
                  "Anything else goes to your assistant.")
_COMMAND_WORDS = frozenset({"PAUSE", "RESUME", "UNPAUSE", "STATUS", "HELP"})


ASSISTANT_PHRASES = frozenset({"UNDO", "STOP STERLING", "START STERLING"})


def _assistant_command(phrase: str) -> str:
    """UNDO (revert the assistant's last change to its own code), STOP/START STERLING (kill switch)."""
    try:
        from assistant import runtime, selfedit

        if phrase == "UNDO":
            return selfedit.undo()["message"]
        runtime.set_stopped(phrase == "STOP STERLING", via="sms")
        return ("Stopped. Your assistant will not run until you text START STERLING."
                if phrase == "STOP STERLING" else "Started. Your assistant is back.")
    except Exception as exc:
        log.exception("assistant command failed")
        return f"That did not work: {type(exc).__name__}. See the dashboard."


def _assistant_enabled() -> bool:
    try:
        from assistant.settings import get_assistant_settings

        return get_assistant_settings().enabled
    except Exception:  # the assistant must never break the pipeline's own commands
        log.exception("assistant settings unreadable")
        return False


def is_pipeline_command(body: str) -> bool:
    """YES/NO <id>, PAUSE, RESUME, STATUS, HELP - exactly what :func:`handle_sms_command` acts on.

    A bare YES/NO stays a pipeline command while any approval is pending (so the id-required
    rule still answers it); with nothing pending it is ordinary conversation.
    """
    text = (body or "").strip()
    m = _ANSWER_RE.match(text)
    if m:
        return bool(m.group(2)) or bool(db.list_decisions("pending", limit=1))
    if " ".join(text.upper().split()) in ASSISTANT_PHRASES:
        return True
    return bool(text) and text.split()[0].upper() in _COMMAND_WORDS


def assistant_inbound(body: str, media_params: dict[str, str] | None = None) -> None:
    """Background: an owner text the pipeline does not handle (already authenticated + de-duplicated).

    ``media_params``: the webhook form of a message with ``MediaUrlN`` attachments; they are
    downloaded (Twilio auth) into data/assistant/uploads and handed to the assistant.
    """
    try:
        from assistant import checkins, runtime

        if media_params:
            from assistant import messaging

            media = messaging.download_media(media_params)
            images = [m["path"] for m in media if m["type"].startswith("image/")]
            note = messaging.media_note(media)
            text = "\n\n".join(x for x in (body.strip(), note) if x) or "(sent an attachment)"
            runtime.run_turn(text, channel="sms", deliver=True, attachments=images or None)
            return
        if checkins.handle_reply(body):
            return
        runtime.handle_inbound_sms(body)
    except Exception as exc:
        db.log_event(MODULE, "assistant failed to handle an owner SMS", level="error",
                     data={"error": repr(exc)[:500]})
