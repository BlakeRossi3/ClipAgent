"""Texting channel choice: SMS or WhatsApp (both through Twilio, same account and credentials).

Every text to the owner goes through ``notify._deliver`` (``notify.sms`` / ``notify.notify`` /
``assistant.tools.send_to_owner`` / check-ins), which asks :func:`route_text` which transport to
use. Quiet hours, the daily cap and de-dup stay in ``notify``; this module only picks the pipe.

``MESSAGING_CHANNEL``:

* ``sms`` - always SMS.
* ``whatsapp`` - WhatsApp; outside the 24-hour window SMS if it is known to work, else hold.
* ``auto`` (default) - WhatsApp while SMS is not known to work (A2P 10DLC campaign not approved
  yet, or a recent 30034 carrier block), otherwise SMS. With WhatsApp not set up: SMS.

WhatsApp rules (Twilio docs, checked 2026-11 via search; see docs/TWILIO_SETUP.md):

* free-form messages only inside the 24-hour customer-service window that opens each time the
  owner messages us; outside it only an approved template goes out (error 63016);
* the Sandbox only reaches numbers that joined it ("join <code>"); a join lasts 3 days, then he
  must send the join message again (error 63015).

When WhatsApp is chosen and the window is closed and SMS is not usable, the message is *held*:
its ``notifications`` row stays ``queued`` with error ``held: <reason>`` and goes out as soon as
the owner's next WhatsApp message opens the window (``notify`` runs ``send_queued``).
Held rows older than :data:`HOLD_MAX_HOURS` are dropped (status ``skipped``) so a long silence
never ends in a flood.

State lives in ``kv`` (``messaging:*``). No import side effects.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import config
import db

log = logging.getLogger(__name__)

MODULE = "messaging"
CHANNEL_MODES = ("auto", "sms", "whatsapp")
TRANSPORTS = ("sms", "whatsapp")
WINDOW = timedelta(hours=24)
WINDOW_MARGIN = timedelta(minutes=10)      # do not start a send that may land after the window shuts
SANDBOX_JOIN_TTL = timedelta(days=3)       # Twilio: "The Sandbox session expires three days after joining"
SMS_BLOCK_CODES = frozenset({30034, 30007, 30032})
WA_WINDOW_CODES = frozenset({63016})       # free-form outside the 24-hour window
WA_NOT_JOINED_CODES = frozenset({63015})   # sandbox recipient has not joined (or the 3 days lapsed)
A2P_REFRESH = timedelta(hours=6)
HOLD_MAX_HOURS = 48
SANDBOX_NUMBER_HINT = "+14155238886"       # Twilio's shared sandbox number as widely documented: verify in console
MAX_MEDIA_BYTES = 16 * 1024 * 1024
MEDIA_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif",
             "audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/amr": "amr", "audio/mp4": "m4a", "audio/aac": "aac",
             "video/mp4": "mp4", "application/pdf": "pdf"}

K_WA_LAST_IN = "messaging:wa_last_in"
K_WA_JOINED = "messaging:wa_joined_at"
K_WA_ERROR = "messaging:wa_error"
K_SMS_BLOCK = "messaging:sms_blocked"
K_SMS_OK = "messaging:sms_last_ok"
K_A2P = "messaging:a2p"
K_A2P_CHECK = "messaging:a2p_checked"
K_HELD = "messaging:held:{nid}"
K_TRANSPORT = "messaging:transport:{nid}"
K_REQUEUED = "messaging:requeued:{nid}"


# ---------------------------------------------------------------------------
# addresses
# ---------------------------------------------------------------------------

def normalize(raw: str | None) -> str:
    """E.164 without any ``whatsapp:`` prefix (US default)."""
    import notify

    return notify.normalize_phone(strip_prefix(raw))


def strip_prefix(raw: str | None) -> str:
    s = (raw or "").strip()
    return s.split(":", 1)[1] if s.lower().startswith("whatsapp:") else s


def is_whatsapp_address(raw: str | None) -> bool:
    return (raw or "").strip().lower().startswith("whatsapp:")


def wa(number: str | None) -> str:
    n = normalize(number)
    return f"whatsapp:{n}" if n else ""


def whatsapp_configured(s: config.Settings | None = None) -> bool:
    s = s or config.get_settings()
    return bool(s.whatsapp_from and s.twilio_account_sid and s.twilio_auth_token and s.owner_phone)


# ---------------------------------------------------------------------------
# kv state helpers
# ---------------------------------------------------------------------------

def _jget(key: str) -> dict[str, Any] | None:
    raw = db.kv_get(key)
    if not raw:
        return None
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    return v if isinstance(v, dict) else None


def _jset(key: str, value: dict[str, Any]) -> None:
    db.kv_set(key, json.dumps(value))


def _ts(key: str) -> datetime | None:
    raw = db.kv_get(key)
    if isinstance(raw, str) and raw:
        try:
            return db.parse_iso(raw)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# WhatsApp window / sandbox state
# ---------------------------------------------------------------------------

def record_inbound_whatsapp(body: str = "") -> None:
    """The owner messaged us on WhatsApp: the 24-hour window is (re)opened."""
    db.kv_set(K_WA_LAST_IN, db.now_iso())
    if (body or "").strip().lower().startswith("join "):
        db.kv_set(K_WA_JOINED, db.now_iso())
    err = _jget(K_WA_ERROR)
    if err and int(err.get("code") or 0) in WA_WINDOW_CODES | WA_NOT_JOINED_CODES:
        db.kv_delete(K_WA_ERROR)


def window_closes_at() -> datetime | None:
    last = _ts(K_WA_LAST_IN)
    return last + WINDOW if last else None


def window_open(now: datetime | None = None) -> bool:
    closes = window_closes_at()
    return bool(closes) and (now or db.utcnow()) < closes - WINDOW_MARGIN  # type: ignore[operator]


def whatsapp_state() -> dict[str, Any]:
    """For the wizard/dashboard: configured, joined?, window open until, last error."""
    s = config.get_settings()
    closes = window_closes_at()
    joined = _ts(K_WA_JOINED)
    err = _jget(K_WA_ERROR)
    last_in = _ts(K_WA_LAST_IN)
    if not whatsapp_configured(s):
        state, text = "off", "Not set up"
    elif err and int(err.get("code") or 0) in WA_NOT_JOINED_CODES:
        state, text = "not_joined", "Not joined (or the sandbox's 3 days ran out): send the join message again"
    elif window_open():
        state, text = "open", f"Joined, window open until {_local(closes)}"
    elif last_in:
        state, text = "closed", "Joined, but the 24-hour window is closed: send any WhatsApp message to reopen it"
    else:
        state, text = "not_joined", "Not joined yet"
    rejoin = joined + SANDBOX_JOIN_TTL if joined else None
    return {"state": state, "text": text, "configured": whatsapp_configured(s), "from": s.whatsapp_from,
            "join_code": s.whatsapp_join_code, "window_closes_at": db.to_iso(closes) if closes else None,
            "window_closes_local": _local(closes) if closes else None,
            "joined_at": db.to_iso(joined) if joined else None,
            "rejoin_by_local": _local(rejoin) if rejoin else None, "last_error": err,
            "sandbox": _is_sandbox(s.whatsapp_from)}


def _is_sandbox(num: str | None) -> bool:
    return normalize(num) == SANDBOX_NUMBER_HINT


def _local(dt: datetime | None) -> str:
    if dt is None:
        return ""
    loc = dt.astimezone(db.local_tz())   # no "%-I": Windows strftime does not support it
    return f"{loc.strftime('%a')} {loc.hour % 12 or 12}:{loc.minute:02d} {'AM' if loc.hour < 12 else 'PM'}"


# ---------------------------------------------------------------------------
# SMS health (A2P 10DLC)
# ---------------------------------------------------------------------------

def record_sms_error(code: int, *, sid: str | None = None) -> None:
    _jset(K_SMS_BLOCK, {"code": int(code), "at": db.now_iso(), "sid": sid})
    db.log_event(MODULE, f"SMS blocked by carriers (error {code}); texts switch to WhatsApp when set up",
                 level="warning", data={"code": code, "sid": sid})


def record_sms_ok() -> None:
    db.kv_set(K_SMS_OK, db.now_iso())
    if db.kv_get(K_SMS_BLOCK):
        db.kv_delete(K_SMS_BLOCK)
        db.log_event(MODULE, "SMS delivered again; SMS is usable")


def record_whatsapp_error(code: int, *, sid: str | None = None) -> None:
    _jset(K_WA_ERROR, {"code": int(code), "at": db.now_iso(), "sid": sid})


def set_a2p_status(status: str | None) -> None:
    """Campaign status seen by the doctor or :func:`refresh_a2p` (VERIFIED = approved)."""
    old = (_jget(K_A2P) or {}).get("status")
    st = (status or "NONE").upper()
    _jset(K_A2P, {"status": st, "at": db.now_iso()})
    if st == "VERIFIED" and old != "VERIFIED":
        db.kv_delete(K_SMS_BLOCK)
        db.log_event(MODULE, "A2P 10DLC campaign approved: SMS is on")


def a2p_status() -> str | None:
    return (_jget(K_A2P) or {}).get("status")


def sms_state() -> str:
    """ok | blocked | waiting_a2p | unknown."""
    block = _jget(K_SMS_BLOCK)
    if block:
        return "blocked"
    st = a2p_status()
    if st == "VERIFIED":
        return "ok"
    if st and st != "NOT_US_LOCAL":
        return "waiting_a2p"
    if st == "NOT_US_LOCAL" or db.kv_get(K_SMS_OK):
        return "ok"
    return "unknown"


SMS_STATE_TEXT = {"ok": "Working", "blocked": "Blocked by US carriers (error 30034) until the A2P campaign is approved",
                  "waiting_a2p": "Waiting for carrier approval (A2P 10DLC campaign)", "unknown": "Not checked yet"}


def _a2p_reader(client: Any, number: str) -> str | None:
    """Campaign status for the Messaging Service holding ``number`` (twilio 9.x field names)."""
    if not (number.startswith("+1") and number[2:5] not in ("800", "833", "844", "855", "866", "877", "888")):
        return "NOT_US_LOCAL"
    for svc in client.messaging.v1.services.list(limit=20):
        nums = [getattr(p, "phone_number", None) for p in client.messaging.v1.services(svc.sid).phone_numbers.list(limit=50)]
        if number in nums:
            sts = [str(getattr(c, "campaign_status", "") or "").upper()
                   for c in client.messaging.v1.services(svc.sid).us_app_to_person.list(limit=10)]
            for want in ("VERIFIED", "IN_PROGRESS", "PENDING", "FAILED"):
                if want in sts:
                    return want
            return "NO_CAMPAIGN"
    return "NO_SERVICE"


def refresh_a2p(*, force: bool = False) -> str | None:
    """Re-read the campaign status at most every :data:`A2P_REFRESH` (live mode only)."""
    s = config.get_settings()
    if s.dry_run or not s.twilio_configured:
        return a2p_status()
    last = _ts(K_A2P_CHECK)
    if not force and last and db.utcnow() - last < A2P_REFRESH:
        return a2p_status()
    db.kv_set(K_A2P_CHECK, db.now_iso())
    try:
        import notify

        set_a2p_status(_a2p_reader(notify._get_client(s), normalize(s.twilio_from_number)))
    except Exception as exc:  # never break a send over a status read
        log.warning("A2P status check failed: %s", exc)
    return a2p_status()


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Route:
    transport: str          # sms | whatsapp
    hold: bool = False
    reason: str = ""


def mode(s: config.Settings | None = None) -> str:
    m = (s or config.get_settings()).messaging_channel
    return m if m in CHANNEL_MODES else "auto"


def _whatsapp_or_hold(sms_ok: bool, why: str) -> Route:
    if window_open():
        return Route("whatsapp", reason=why)
    err = _jget(K_WA_ERROR) or {}
    closed = ("you have not joined the WhatsApp sandbox (or the 3 days ran out)"
              if int(err.get("code") or 0) in WA_NOT_JOINED_CODES else "the WhatsApp 24-hour window is closed")
    if sms_ok:
        return Route("sms", reason=f"{closed}; SMS works")
    return Route("whatsapp", hold=True, reason=f"{closed} and SMS is not usable yet; sent after your next WhatsApp message")


def route_text(kind: str = "", *, transport: str | None = None, refresh: bool = True) -> Route:
    """Which pipe a text to the owner takes now. ``transport`` forces one (test buttons)."""
    s = config.get_settings()
    if transport in TRANSPORTS:
        return Route(transport, reason="chosen")
    m = mode(s)
    if m == "sms" or not whatsapp_configured(s):
        return Route("sms", reason="MESSAGING_CHANNEL=sms" if m == "sms" else "WhatsApp not set up")
    if m == "auto" and refresh:
        refresh_a2p()
    st = sms_state()
    if m == "whatsapp":
        return _whatsapp_or_hold(st == "ok", "MESSAGING_CHANNEL=whatsapp")
    if st == "ok":
        return Route("sms", reason="auto: SMS works")
    return _whatsapp_or_hold(False, f"auto: SMS {st.replace('_', ' ')}")


def current_channel_text() -> str:
    r = route_text(refresh=False)
    return f"{r.transport.upper()}" + (" (held until you message on WhatsApp)" if r.hold else "") + f" - {r.reason}"


# ---------------------------------------------------------------------------
# hold / transport bookkeeping (notify calls these)
# ---------------------------------------------------------------------------

def hold(notification_id: int, reason: str) -> None:
    """Keep a claimed row ``queued`` for later; :func:`notify.send_queued` retries it."""
    db.kv_set(K_HELD.format(nid=notification_id), db.now_iso())
    db.kv_delete(f"notify:claim:{notification_id}")
    db.update_notification(notification_id, status="queued", error=f"held: {reason}"[:500])
    db.log_event(MODULE, "text held: " + reason, data={"notification_id": notification_id})


def is_held(notification_id: int) -> bool:
    return bool(db.kv_get(K_HELD.format(nid=notification_id)))


def held_since(notification_id: int) -> datetime | None:
    return _ts(K_HELD.format(nid=notification_id))


def release(notification_id: int) -> None:
    db.kv_delete(K_HELD.format(nid=notification_id))


def expire_if_stale(notification_id: int) -> bool:
    """Drop a held row older than :data:`HOLD_MAX_HOURS`. Returns True when dropped."""
    since = held_since(notification_id)
    if since and db.utcnow() - since > timedelta(hours=HOLD_MAX_HOURS):
        if db.kv_set_if_absent(f"notify:claim:{notification_id}", db.now_iso()):
            release(notification_id)
            db.update_notification(notification_id, status="skipped",
                                   error=f"dropped: WhatsApp window stayed closed for {HOLD_MAX_HOURS} h")
        return True
    return False


def set_transport(notification_id: int, transport: str) -> None:
    db.kv_set(K_TRANSPORT.format(nid=notification_id), transport)


def transport_of(notification_id: int) -> str:
    return str(db.kv_get(K_TRANSPORT.format(nid=notification_id)) or "sms")


def requeue_once(notification_id: int, reason: str) -> bool:
    """A sent text failed later (status callback): put it back once for re-routing."""
    if not db.kv_set_if_absent(K_REQUEUED.format(nid=notification_id), db.now_iso()):
        return False
    hold(notification_id, reason)
    return True


# ---------------------------------------------------------------------------
# inbound media (MediaUrlN): download with Twilio auth into data/assistant/uploads
# ---------------------------------------------------------------------------

Fetcher = Callable[[str, tuple[str, str]], tuple[bytes, str]]


def _default_fetcher(url: str, auth: tuple[str, str]) -> tuple[bytes, str]:
    import httpx

    with httpx.Client(follow_redirects=True, timeout=30) as c:
        with c.stream("GET", url, auth=auth) as r:
            r.raise_for_status()
            chunks, size = [], 0
            for chunk in r.iter_bytes():
                size += len(chunk)
                if size > MAX_MEDIA_BYTES:
                    raise ValueError("media too large")
                chunks.append(chunk)
            return b"".join(chunks), r.headers.get("content-type", "")


_fetcher: Fetcher = _default_fetcher


def set_media_fetcher(fn: Fetcher | None) -> None:
    """Tests: replace the HTTP download (``None`` = httpx)."""
    global _fetcher
    _fetcher = fn or _default_fetcher


def uploads_dir() -> Path:
    p = config.get_settings().data_dir / "assistant" / "uploads"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _media_url_ok(url: str) -> bool:
    p = urlparse(url)
    host = (p.hostname or "").lower()
    return p.scheme == "https" and (host == "twilio.com" or host.endswith(".twilio.com"))


def download_media(params: dict[str, str]) -> list[dict[str, str]]:
    """Save each ``MediaUrlN`` of an (already signature-checked) owner message.

    Only https URLs on twilio.com are fetched (Basic auth = Account SID + Auth Token, which Twilio
    accepts for media; redirects to its CDN are followed). Returns ``[{"path", "type"}]``.
    """
    s = config.get_settings()
    try:
        n = min(int(params.get("NumMedia") or 0), 10)
    except ValueError:
        n = 0
    out: list[dict[str, str]] = []
    for i in range(n):
        url = params.get(f"MediaUrl{i}") or ""
        ctype = (params.get(f"MediaContentType{i}") or "").split(";")[0].strip().lower()
        if not _media_url_ok(url):
            db.log_event(MODULE, "inbound media URL refused (not twilio.com https)", level="warning",
                         data={"url": url[:200]})
            continue
        try:
            data, got_type = _fetcher(url, (s.twilio_account_sid, s.twilio_auth_token))
        except Exception as exc:
            db.log_event(MODULE, "inbound media download failed", level="warning", data={"error": str(exc)[:300]})
            continue
        ctype = ctype or (got_type or "").split(";")[0].strip().lower()
        ext = MEDIA_EXT.get(ctype) or (mimetypes.guess_extension(ctype) or ".bin").lstrip(".")
        if not re.fullmatch(r"[a-z0-9]{1,5}", ext):
            ext = "bin"
        path = uploads_dir() / f"wa-{uuid.uuid4().hex}.{ext}"
        path.write_bytes(data)
        out.append({"path": str(path), "type": ctype})
    if out:
        db.log_event(MODULE, f"saved {len(out)} attachment(s) from the owner", data={"types": [m["type"] for m in out]})
    return out


def media_note(media: list[dict[str, str]]) -> str:
    """Text added to the owner's message for non-image attachments (voice notes, files)."""
    lines = [f"- {m['type'] or 'file'}: {m['path']}" for m in media if not m["type"].startswith("image/")]
    if not lines:
        return ""
    return ("[The owner also sent these files (voice notes are audio; transcribe them if you have a tool for it):\n"
            + "\n".join(lines) + "]")


# ---------------------------------------------------------------------------
# test messages (wizard buttons)
# ---------------------------------------------------------------------------

def send_test(transport: str, text: str) -> dict[str, Any]:
    import notify

    nid = notify.sms(text, kind="setup_test", transport=transport)
    row = db.get_notification(nid)
    return {"notification_id": nid, "status": row.status if row else "failed", "error": row.error if row else None,
            "transport": transport}
