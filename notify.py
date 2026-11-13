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
from datetime import time as dtime
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
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


def _is_owner(number: str | None) -> bool:
    owner = normalize_phone(config.get_settings().owner_phone)
    return bool(owner) and normalize_phone(number) == owner


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


def _deliver(notification_id: int, channel: str, kind: str, text: str, decision_id: int | None) -> str:
    """Send an already-claimed row now. Returns the final status: sent | dry_run | failed."""
    s = config.get_settings()
    if s.dry_run or not s.twilio_configured:
        why = "DRY_RUN" if s.dry_run else "Twilio not configured"
        db.update_notification(notification_id, status="dry_run", error=None)
        db.log_event(MODULE, f"[dry run] would {channel} owner ({kind})", data={
            "notification_id": notification_id, "reason": why, "decision_id": decision_id, "text": text[:300]})
        return "dry_run"
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
            res = client.messages.create(to=normalize_phone(s.owner_phone),
                                         from_=normalize_phone(s.twilio_from_number), body=_sms_text(text))
    except Exception as exc:  # Twilio REST errors, network, misconfiguration
        db.update_notification(notification_id, status="failed", error=str(exc)[:500])
        db.log_event(MODULE, f"{channel} to owner failed ({kind})", level="error",
                     data={"notification_id": notification_id, "error": str(exc)[:500]})
        return "failed"
    db.update_notification(notification_id, status="sent", sid=getattr(res, "sid", None))
    db.log_event(MODULE, f"{channel} sent to owner ({kind})",
                 data={"notification_id": notification_id, "sid": getattr(res, "sid", None),
                       "decision_id": decision_id})
    return "sent"


def call(text: str, *, kind: str = "call", decision_id: int | None = None) -> int:
    """Phone the owner now (no quiet-hours policy). Returns the notification id.

    With ``decision_id`` the call runs the approval flow (``/twilio/voice/{decision_id}``),
    otherwise it reads ``text`` (``/twilio/say/{notification_id}``).
    """
    nid = _new_row(kind, text, "call", decision_id)
    _deliver(nid, "call", kind, text, decision_id)
    return nid


def sms(text: str, *, kind: str = "sms", decision_id: int | None = None) -> int:
    """Text the owner now (no quiet-hours policy). Returns the notification id."""
    nid = _new_row(kind, text, "sms", decision_id)
    _deliver(nid, "sms", kind, text, decision_id)
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


def handle_decision(decision_id: int, approved: bool, *, via: str) -> bool:
    """Resolve a pending decision (first answer wins). Approved -> ``strategist.apply_decision``.

    Returns True if this answer resolved the decision; False if it was unknown, already
    resolved or expired (nothing is re-applied).
    """
    d = _current_decision(decision_id)
    if d is None:
        db.log_event(MODULE, "answer for unknown decision ignored", level="warning",
                     data={"decision_id": decision_id, "via": via})
        return False
    status = "approved" if approved else "rejected"
    if d.status != "pending" or not db.resolve_decision(decision_id, status, via=via):
        current = db.get_decision(decision_id)
        db.log_event(MODULE, "answer for already-resolved decision ignored", level="warning",
                     data={"decision_id": decision_id, "via": via, "answer": status,
                           "status": current.status if current else None})
        return False
    db.log_event(MODULE, f"decision {decision_id} {status} via {via}",
                 data={"decision_id": decision_id, "kind": d.kind})
    if approved:
        try:
            import strategist  # late import: strategist imports notify

            applied = strategist.apply_decision(decision_id)
            db.log_event(MODULE, f"decision {decision_id} applied" if applied
                         else f"decision {decision_id} approved but apply_decision returned False",
                         level="info" if applied else "warning", data={"decision_id": decision_id})
        except Exception as exc:
            db.log_event(MODULE, f"applying decision {decision_id} failed", level="error",
                         data={"decision_id": decision_id, "error": repr(exc)[:500]})
    return True


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

router = APIRouter(prefix="/twilio", tags=["twilio"])

HELP_TEXT = ("Commands: YES <id> / NO <id> to answer an approval (bare YES/NO if only one is pending), "
             "STATUS, PAUSE (stop AI spending), RESUME, HELP.")


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


async def _twilio_params(request: Request) -> dict[str, str]:
    """Read the form and enforce ``X-Twilio-Signature``. Raises 403 when it does not verify."""
    s = config.get_settings()
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    token = s.twilio_auth_token
    if not token:
        if s.dry_run:
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
    elif d is not None and d.call_sid and call_sid != d.call_sid:
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
        if handle_decision(decision_id, approved, via="call"):
            _say(resp, "Approved. I'll apply the change now. Thanks, goodbye." if approved
                 else "Rejected. Nothing will change. Thanks, goodbye.")
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


_ANSWER_RE = re.compile(r"^\s*(yes|y|approve|no|n|reject)\b[\s#:.,-]*(\d+)?\s*[.!]?\s*$", re.IGNORECASE)


def handle_sms_command(body: str) -> str:
    """Execute an owner SMS command and return the reply text."""
    text = (body or "").strip()
    word = text.split()[0].upper() if text else ""
    m = _ANSWER_RE.match(text)
    if m:
        approved = m.group(1).lower() in ("yes", "y", "approve")
        if m.group(2):
            decision_id = int(m.group(2))
        else:
            pending = db.list_decisions("pending")
            pending = [d for d in pending if (c := _current_decision(d.id)) and c.status == "pending"]
            if not pending:
                return "Nothing is waiting for approval."
            if len(pending) > 1:
                ids = ", ".join(f"#{d.id}" for d in pending[:5])
                return f"Several approvals are pending ({ids}). Reply YES <id> or NO <id>."
            decision_id = pending[0].id
        if handle_decision(decision_id, approved, via="sms"):
            return f"#{decision_id} approved. Applying it now." if approved else f"#{decision_id} rejected. Nothing changes."
        d = db.get_decision(decision_id)
        if d is None:
            return f"No request #{decision_id}. Reply STATUS to see pending ones."
        return f"#{decision_id} is already {d.status}" + (f" (via {d.resolved_via})" if d.resolved_via else "") + \
            ". Nothing changed."
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
async def sms_webhook(request: Request) -> Response:
    """Inbound SMS from the owner: YES/NO <id>, PAUSE, RESUME, STATUS, HELP."""
    p = await _twilio_params(request)
    from twilio.twiml.messaging_response import MessagingResponse

    resp = MessagingResponse()
    sender = p.get("From")
    if not _is_owner(sender):
        db.log_event(MODULE, "ignored SMS from non-owner number", level="warning",
                     data={"from": _mask(sender), "body": (p.get("Body") or "")[:40]})
        return _xml(resp)
    body = p.get("Body") or ""
    reply = handle_sms_command(body)
    db.log_event(MODULE, "owner SMS command", data={"command": body[:80], "reply": reply[:200]})
    resp.message(reply)
    return _xml(resp)
