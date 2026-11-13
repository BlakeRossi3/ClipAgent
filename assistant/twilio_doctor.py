"""Twilio doctor: find out why texts/calls fail and say exactly how to fix it.

:func:`diagnose` reads (never writes) with the owner's saved credentials:

* local config: credentials present, number formats (E.164), ``PUBLIC_BASE_URL`` reachable from
  Twilio (https, not localhost), practice mode;
* account: trial vs full, active/suspended; on trial, whether his phone is a verified caller ID;
* the From number: on this account, SMS/voice capable, its SMS webhook vs ours
  (``{PUBLIC_BASE_URL}/twilio/sms``), whether a Messaging Service overrides it;
* US A2P 10DLC: brand registration status and campaign (``us_app_to_person.campaign_status``)
  for the Messaging Service holding the number (field names checked against twilio 9.x source);
* recent messages and calls: status, error code -> plain-English cause + exact fix
  (:data:`ERROR_CODES`, with links to Twilio's docs).

:func:`apply_fix` performs only safe, reversible changes and only when the owner clicks
"Fix it" on the dashboard or approves (``YES <id>``) a request Sterling made with ``twilio_fix``:
``set_sms_webhook`` (old value kept for ``revert_sms_webhook``), ``service_use_number_webhook``,
``normalise_numbers`` (rewrites the two phone settings in config.env to E.164).

Tools ``twilio_diagnose`` / ``twilio_fix``; CLI ``python -m assistant.twilio_doctor [check|fix <action>] [--json]``.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

import config
import db

log = logging.getLogger(__name__)

MODULE = "assistant.twilio_doctor"
DOCS = "https://www.twilio.com/docs/api/errors/"
E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")
KV_PREV = "twilio_doctor:previous:{what}"
FIX_ACTIONS = ("set_sms_webhook", "revert_sms_webhook", "service_use_number_webhook", "normalise_numbers",
               # WhatsApp / texting channel (assistant/messaging.py); all reversible via the same buttons
               "channel_auto", "channel_whatsapp", "channel_sms", "use_detected_whatsapp_number")

A2P_URL = "https://console.twilio.com/us1/develop/sms/regulatory-compliance/a2p-10dlc-overview"
# code -> (cause, fix). Fixes are written for the owner: what to click / change.
ERROR_CODES: dict[int, tuple[str, str]] = {
    20003: ("Twilio refused the Account SID / Auth Token.",
            "Copy the Account SID and Auth Token again from console.twilio.com (Account info) into Settings → Your phone. "
            "If you rotated the token, use the new primary token."),
    20404: ("Twilio could not find that resource (wrong SID, or the number was released).",
            "Check the From number is still under Phone Numbers → Active numbers in the console."),
    11200: ("Twilio could not reach ClipAgent's webhook (HTTP retrieval failure).",
            "PUBLIC_BASE_URL must be a public https address that reaches this PC (e.g. your tunnel). Check the tunnel "
            "is running, then press Fix it to point the number's SMS webhook at {base}/twilio/sms."),
    11205: ("Twilio could not connect to ClipAgent's webhook.", "Same as 11200: make PUBLIC_BASE_URL reachable from the internet."),
    11750: ("ClipAgent's webhook answered with too large a response.", "Report this as a bug; it should not happen."),
    12300: ("ClipAgent's webhook answered with the wrong content type.", "Report this as a bug; update ClipAgent."),
    13224: ("Twilio cannot call that number (invalid or unsupported).", "Check OWNER_PHONE is your mobile in +1XXXXXXXXXX form."),
    21211: ("The To number is not a valid phone number.", "Fix OWNER_PHONE in Settings → Your phone (use +1 and 10 digits)."),
    21212: ("The From number is not valid.", "Set TWILIO_FROM_NUMBER to your Twilio number in +1XXXXXXXXXX form."),
    21215: ("Your Twilio voice geo permissions block calls to that country.",
            "Console → Voice → Settings → Geo permissions: tick United States (or the owner's country)."),
    21219: ("Trial accounts can only call verified numbers.",
            "Verify your mobile at Console → Phone Numbers → Verified Caller IDs, or upgrade the account."),
    21408: ("Your SMS geo permissions block that country.",
            "Console → Messaging → Settings → Geo permissions: tick the owner's country."),
    21606: ("The From number cannot send SMS to this destination (not SMS-capable or not on this account).",
            "Use an SMS-capable number you own (Phone Numbers → Active numbers) as TWILIO_FROM_NUMBER."),
    21608: ("Trial accounts can only text verified numbers.",
            "Verify your mobile at Console → Phone Numbers → Verified Caller IDs, or upgrade (Billing → Upgrade)."),
    21610: ("You replied STOP to this number, so Twilio blocks texts to you.",
            "Text START (or UNSTOP) from your phone to the Twilio number, then try again."),
    21612: ("Twilio cannot route a text between these two numbers.", "Check both numbers; a US local number can text US mobiles."),
    21614: ("The To number is not a mobile number (texts need a mobile).", "Set OWNER_PHONE to your mobile number."),
    21617: ("The message was too long.", "ClipAgent keeps texts short; update ClipAgent if you see this."),
    30001: ("Too many messages queued at once.", "Wait a few minutes; nothing to change."),
    30002: ("Your Twilio account is suspended.", "Check Console → Billing and your e-mail from Twilio."),
    30003: ("Your phone was unreachable (off, no signal).", "Nothing to fix in ClipAgent; it will reach you next time."),
    30004: ("The message was blocked (you or your carrier blocked the number).",
            "Unblock the Twilio number on your phone; check your carrier's spam blocking."),
    30005: ("Unknown destination: the number does not exist or is not in service.", "Check OWNER_PHONE."),
    30006: ("The destination is a landline or cannot receive texts.", "Set OWNER_PHONE to a mobile number."),
    30007: ("Your carrier filtered the message as spam.",
            "Usually fixed by an approved A2P 10DLC campaign (US local numbers). Avoid links/shorteners in texts."),
    30008: ("Unknown delivery error from the carrier.", "Try again later; if it repeats, contact Twilio support."),
    30032: ("Toll-free number not verified.", "Console → Messaging → Regulatory compliance → Toll-free verification."),
    30034: ("Your US local number is not registered for A2P 10DLC, so carriers block its texts.",
            "Finish A2P 10DLC registration (brand + campaign) in the console and attach the number to that "
            "campaign's Messaging Service, then wait for the campaign to be approved (days to weeks). "
            f"Start here: {A2P_URL}"),
    # --- WhatsApp (Twilio docs via search, 2026-11) ---
    63007: ("Twilio has no WhatsApp sender with that From address.",
            "Set WHATSAPP_FROM to the sandbox number shown in Console > Messaging > Try it out > Send a WhatsApp "
            "message (or your approved WhatsApp sender), in +1XXXXXXXXXX form. ClipAgent adds the 'whatsapp:' part."),
    63015: ("The WhatsApp sandbox only messages phones that joined it, and a join lasts 3 days.",
            "On your phone, WhatsApp the sandbox number the message 'join <your code>' (Settings > Your phone shows "
            "it). Held messages go out right after."),
    63016: ("WhatsApp only allows free-form messages within 24 hours of your last message to it.",
            "Send any WhatsApp message to the number (e.g. 'hi'); held messages go out at once. ClipAgent holds texts "
            "while the window is closed, or uses SMS once the A2P campaign is approved."),
    63018: ("WhatsApp rate limit for this sender was exceeded.", "Wait a while; ClipAgent sends few messages, so "
            "this should clear by itself. If it repeats, check nothing else uses this sender."),
}

STATUS_PASS, STATUS_WARN, STATUS_FAIL, STATUS_INFO = "pass", "warn", "fail", "info"


@dataclass
class Check:
    id: str
    status: str
    title: str
    detail: str = ""
    fix: str = ""
    action: str | None = None          # a FIX_ACTIONS entry the owner can click
    docs: str | None = None


@dataclass
class Diagnosis:
    ok: bool
    checks: list[Check]
    account: dict[str, Any] = field(default_factory=dict)
    number: dict[str, Any] = field(default_factory=dict)
    a2p: dict[str, Any] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    checked_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        bad = [c for c in self.checks if c.status == STATUS_FAIL]
        warn = [c for c in self.checks if c.status == STATUS_WARN]
        if not bad and not warn:
            return "Twilio looks healthy."
        return "; ".join(f"{c.title}: {c.fix}" for c in (bad + warn)[:4])


def explain(code: int | str | None) -> dict[str, str] | None:
    try:
        c = int(code)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    cause, fix = ERROR_CODES.get(c, ("Twilio error " + str(c) + ".", "See Twilio's page for this error code."))
    return {"code": str(c), "cause": cause, "fix": fix.replace("{base}", _base()), "docs": f"{DOCS}{c}"}


# ---------------------------------------------------------------------------
# client seam
# ---------------------------------------------------------------------------

_client: Any = None


def set_client(client: Any | None) -> None:
    """Tests: inject a fake Twilio client (``None`` = real client from settings)."""
    global _client
    _client = client


def _get_client() -> Any:
    if _client is not None:
        return _client
    s = config.get_settings()
    from twilio.rest import Client  # optional heavy import

    return Client(s.twilio_account_sid, s.twilio_auth_token)


def _base() -> str:
    return config.get_settings().public_base_url.rstrip("/")


def expected_sms_url() -> str:
    return f"{_base()}/twilio/sms"


def normalise(raw: str | None) -> str | None:
    """E.164 (US default) or None if it cannot be made valid."""
    if not raw:
        return None
    s = raw.strip()
    digits = re.sub(r"\D", "", s)
    if s.startswith("+"):
        cand = "+" + digits
    elif len(digits) == 10:
        cand = "+1" + digits
    elif len(digits) == 11 and digits.startswith("1"):
        cand = "+" + digits
    else:
        cand = "+" + digits
    return cand if E164_RE.match(cand) else None


def _err_code(exc: Exception) -> int | None:
    code = getattr(exc, "code", None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def _iso(v: Any) -> str | None:
    try:
        return db.to_iso(v) if v is not None and hasattr(v, "tzinfo") else (str(v) if v else None)
    except Exception:
        return str(v)


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def _local_checks(s: config.Settings) -> list[Check]:
    out: list[Check] = []
    missing = [n for n, v in (("TWILIO_ACCOUNT_SID", s.twilio_account_sid), ("TWILIO_AUTH_TOKEN", s.twilio_auth_token),
                              ("TWILIO_FROM_NUMBER", s.twilio_from_number), ("OWNER_PHONE", s.owner_phone)) if not v]
    if missing:
        out.append(Check("config", STATUS_FAIL, "Twilio settings missing", ", ".join(missing),
                         "Fill them in under Settings → Your phone (see the Twilio help page there)."))
    elif not re.match(r"^AC[0-9a-fA-F]{32}$", s.twilio_account_sid):
        out.append(Check("config", STATUS_FAIL, "Account SID looks wrong", "It should start with AC and have 34 characters.",
                         "Copy the Account SID from console.twilio.com → Account info."))
    else:
        out.append(Check("config", STATUS_PASS, "Twilio settings present"))
    bad = []
    for name, v in (("TWILIO_FROM_NUMBER", s.twilio_from_number), ("OWNER_PHONE", s.owner_phone)):
        if v and v != normalise(v):
            bad.append(f"{name}={v!s}")
    if bad:
        fixable = all(normalise(x.split("=", 1)[1]) for x in bad)
        out.append(Check("formats", STATUS_WARN if fixable else STATUS_FAIL, "Phone numbers not in +1XXXXXXXXXX form",
                         ", ".join(b.split("=")[0] for b in bad),
                         "Press Fix it to rewrite them in E.164 form." if fixable else "Re-enter them in Settings → Your phone.",
                         "normalise_numbers" if fixable else None))
    p = urlparse(s.public_base_url)
    host = (p.hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(".local") or not host:
        out.append(Check("public_url", STATUS_FAIL, "Twilio cannot reach this PC",
                         f"PUBLIC_BASE_URL is {s.public_base_url}, which only works on this computer.",
                         "Run a tunnel (e.g. Cloudflare Tunnel or ngrok) and set PUBLIC_BASE_URL to its https address "
                         "in Settings → Your phone. Replies by text and call buttons need it.", docs=f"{DOCS}11200"))
    elif p.scheme != "https":
        out.append(Check("public_url", STATUS_WARN, "Webhook address is not https", s.public_base_url,
                         "Use the https address of your tunnel; Twilio signatures and many carriers expect https."))
    else:
        out.append(Check("public_url", STATUS_PASS, "Public address set", s.public_base_url))
    if s.dry_run:
        out.append(Check("dry_run", STATUS_INFO, "Practice mode is on",
                         "DRY_RUN=true: ClipAgent records texts and calls but sends nothing.",
                         "Turn practice mode off in Settings → Power & privacy when you want real texts."))
    return out


def _account(client: Any, s: config.Settings, d: Diagnosis) -> bool:
    try:
        acc = client.api.v2010.accounts(s.twilio_account_sid).fetch()
    except Exception as exc:  # TwilioRestException carries .code / .status
        code = _err_code(exc) or (20003 if getattr(exc, "status", None) == 401 else None)
        e = explain(code) if code else None
        d.checks.append(Check("auth", STATUS_FAIL, "Twilio rejected the credentials" if code == 20003 else "Could not reach Twilio",
                              e["cause"] if e else f"{type(exc).__name__}: {str(exc)[:200]}",
                              e["fix"] if e else "Check the internet connection and try again.", docs=e["docs"] if e else None))
        return False
    d.account = {"type": str(getattr(acc, "type", "") or ""), "status": str(getattr(acc, "status", "") or ""),
                 "name": getattr(acc, "friendly_name", None)}
    if d.account["status"] and d.account["status"].lower() != "active":
        d.checks.append(Check("account", STATUS_FAIL, f"Twilio account is {d.account['status']}", "",
                              "Check Console → Billing and your e-mail from Twilio.", docs=f"{DOCS}30002"))
    else:
        d.checks.append(Check("account", STATUS_PASS, f"Account active ({d.account['type'] or 'unknown type'})"))
    if d.account["type"].lower() == "trial":
        verified = None
        try:
            verified = bool(client.outgoing_caller_ids.list(phone_number=normalise(s.owner_phone) or s.owner_phone, limit=1))
        except Exception:
            pass
        if verified is False:
            d.checks.append(Check("trial_verified", STATUS_FAIL, "Trial account: your phone is not verified",
                                  "Trial accounts can only text and call verified numbers.",
                                  explain(21608)["fix"], docs=f"{DOCS}21608"))  # type: ignore[index]
        else:
            d.checks.append(Check("trial", STATUS_WARN, "Trial account",
                                  "Texts start with 'Sent from your Twilio trial account' and only reach verified numbers.",
                                  "Upgrade under Console → Billing when you are ready (adds a small balance)."))
    return True


def _number(client: Any, s: config.Settings, d: Diagnosis) -> Any:
    num = normalise(s.twilio_from_number) or s.twilio_from_number
    try:
        found = client.incoming_phone_numbers.list(phone_number=num, limit=5)
    except Exception as exc:
        d.checks.append(Check("number", STATUS_WARN, "Could not read the phone number", f"{type(exc).__name__}"))
        return None
    if not found:
        d.checks.append(Check("number", STATUS_FAIL, "The From number is not on this Twilio account", num,
                              "Set TWILIO_FROM_NUMBER to a number listed under Phone Numbers → Active numbers.",
                              docs=f"{DOCS}21606"))
        return None
    n = found[0]
    caps = getattr(n, "capabilities", None) or {}
    if isinstance(caps, str):
        try:
            caps = json.loads(caps)
        except ValueError:
            caps = {}
    d.number = {"sid": n.sid, "phone_number": getattr(n, "phone_number", num), "capabilities": caps,
                "sms_url": getattr(n, "sms_url", None), "sms_method": getattr(n, "sms_method", None),
                "sms_application_sid": getattr(n, "sms_application_sid", None),
                "voice_url": getattr(n, "voice_url", None), "expected_sms_url": expected_sms_url()}
    if caps and not caps.get("sms", caps.get("SMS")):
        d.checks.append(Check("number_sms", STATUS_FAIL, "This number cannot send texts", num,
                              "Buy an SMS-capable number (Phone Numbers → Buy a number, tick SMS).", docs=f"{DOCS}21606"))
    if caps and not caps.get("voice", caps.get("Voice")):
        d.checks.append(Check("number_voice", STATUS_WARN, "This number cannot make calls", num,
                              "Approval calls fall back to texts; buy a voice-capable number if you want calls."))
    if d.number["sms_application_sid"]:
        d.checks.append(Check("sms_webhook", STATUS_WARN, "Inbound texts go to a TwiML App, not ClipAgent",
                              "The number's messaging is configured with an application.",
                              "Press Fix it to send inbound texts to ClipAgent instead.", "set_sms_webhook"))
    elif (d.number["sms_url"] or "").rstrip("/") != expected_sms_url():
        d.checks.append(Check("sms_webhook", STATUS_WARN if d.number["sms_url"] else STATUS_FAIL,
                              "Replies by text do not reach ClipAgent",
                              f"The number sends incoming texts to {d.number['sms_url'] or 'nowhere'}; "
                              f"it should be {expected_sms_url()}.",
                              "Press Fix it to set the number's SMS webhook (you can undo it).", "set_sms_webhook",
                              f"{DOCS}11200"))
    else:
        d.checks.append(Check("sms_webhook", STATUS_PASS, "Incoming texts reach ClipAgent", expected_sms_url()))
    return n


def _is_us_local(num: str) -> bool:
    return num.startswith("+1") and num[2:5] not in ("800", "833", "844", "855", "866", "877", "888")


def _a2p(client: Any, s: config.Settings, d: Diagnosis, number: Any) -> None:
    num = d.number.get("phone_number") or normalise(s.twilio_from_number) or ""
    info: dict[str, Any] = {"brands": [], "service": None, "campaigns": []}
    try:
        for b in client.messaging.v1.brand_registrations.list(limit=5):
            info["brands"].append({"sid": b.sid, "status": str(getattr(b, "status", "") or ""),
                                   "failure_reason": getattr(b, "failure_reason", None),
                                   "brand_type": getattr(b, "brand_type", None)})
    except Exception as exc:
        info["brands_error"] = type(exc).__name__
    service = None
    try:
        for svc in client.messaging.v1.services.list(limit=20):
            nums = [getattr(p, "phone_number", None) for p in client.messaging.v1.services(svc.sid).phone_numbers.list(limit=50)]
            if num in nums:
                service = svc
                break
    except Exception as exc:
        info["services_error"] = type(exc).__name__
    if service is not None:
        info["service"] = {"sid": service.sid, "name": getattr(service, "friendly_name", None),
                           "us_app_to_person_registered": getattr(service, "us_app_to_person_registered", None),
                           "use_inbound_webhook_on_number": getattr(service, "use_inbound_webhook_on_number", None),
                           "inbound_request_url": getattr(service, "inbound_request_url", None)}
        try:
            for c in client.messaging.v1.services(service.sid).us_app_to_person.list(limit=10):
                info["campaigns"].append({"sid": c.sid, "campaign_status": str(getattr(c, "campaign_status", "") or ""),
                                          "usecase": getattr(c, "us_app_to_person_usecase", None),
                                          "errors": getattr(c, "errors", None)})
        except Exception as exc:
            info["campaigns_error"] = type(exc).__name__
        if info["service"]["use_inbound_webhook_on_number"] is False and \
                (info["service"]["inbound_request_url"] or "").rstrip("/") != expected_sms_url():
            d.checks.append(Check("service_webhook", STATUS_WARN, "The Messaging Service overrides the number's webhook",
                                  f"Service '{info['service']['name']}' sends incoming texts to "
                                  f"{info['service']['inbound_request_url'] or 'nowhere'}.",
                                  "Press Fix it to make the service use the number's own webhook (ClipAgent).",
                                  "service_use_number_webhook"))
    d.a2p = info
    if not num or not _is_us_local(num):
        return
    statuses = [c["campaign_status"].upper() for c in info["campaigns"]]
    brand = [b["status"].upper() for b in info["brands"]]
    if "VERIFIED" in statuses:
        d.checks.append(Check("a2p", STATUS_PASS, "A2P 10DLC campaign approved"))
    elif any(st in ("PENDING", "IN_PROGRESS") for st in statuses):
        d.checks.append(Check("a2p", STATUS_WARN, "A2P 10DLC campaign is waiting for approval",
                              "Until it is approved, US carriers block texts from this number (error 30034).",
                              "Nothing to do but wait (often 1-3 weeks); watch for Twilio's e-mail.", docs=f"{DOCS}30034"))
    elif "FAILED" in statuses:
        errs = "; ".join(str(e) for c in info["campaigns"] for e in (c.get("errors") or []))[:300]
        d.checks.append(Check("a2p", STATUS_FAIL, "A2P 10DLC campaign was rejected", errs or "see the console",
                              "Fix what the rejection says (often the opt-in description or privacy policy link) and "
                              f"resubmit the campaign: {A2P_URL}", docs=f"{DOCS}30034"))
    else:
        why = "No Messaging Service holds this number" if service is None else "No campaign on its Messaging Service"
        bstate = f" Brand: {', '.join(brand)}." if brand else " No brand registered yet."
        d.checks.append(Check("a2p", STATUS_FAIL, "Number not registered for A2P 10DLC",
                              why + "." + bstate + " US carriers block texts from unregistered local numbers (30034).",
                              explain(30034)["fix"], docs=f"{DOCS}30034"))  # type: ignore[index]


def _history(client: Any, s: config.Settings, d: Diagnosis) -> None:
    num = d.number.get("phone_number") or normalise(s.twilio_from_number)
    try:
        msgs = client.messages.list(limit=20)
    except Exception:
        msgs = []
    codes: dict[int, int] = {}
    for m in msgs:
        frm = getattr(m, "from_", None) or getattr(m, "_from", None)
        code = getattr(m, "error_code", None)
        row = {"date": _iso(getattr(m, "date_sent", None) or getattr(m, "date_created", None)),
               "direction": getattr(m, "direction", None), "status": str(getattr(m, "status", "") or ""),
               "to": getattr(m, "to", None), "from": frm, "error_code": code,
               "error_message": getattr(m, "error_message", None)}
        if code:
            row["explain"] = explain(code)
            if not num or frm == num:
                codes[int(code)] = codes.get(int(code), 0) + 1
        d.messages.append(row)
    try:
        for c in client.calls.list(limit=10):
            d.calls.append({"date": _iso(getattr(c, "start_time", None) or getattr(c, "date_created", None)),
                            "status": str(getattr(c, "status", "") or ""), "to": getattr(c, "to", None),
                            "direction": getattr(c, "direction", None)})
    except Exception:
        pass
    if codes:
        top = sorted(codes.items(), key=lambda kv: -kv[1])
        for code, n in top[:3]:
            e = explain(code)
            assert e is not None
            d.checks.append(Check(f"err_{code}", STATUS_FAIL, f"Recent texts failed with error {code} ({n}x)",
                                  e["cause"], e["fix"], "set_sms_webhook" if code in (11200, 11205) else None, e["docs"]))
    elif msgs:
        d.checks.append(Check("recent", STATUS_PASS, "Recent texts delivered without errors"))
    failed_calls = [c for c in d.calls if c["status"] in ("failed", "busy", "no-answer")]
    if failed_calls and len(failed_calls) == len(d.calls):
        d.checks.append(Check("calls", STATUS_WARN, "Recent calls were not answered or failed",
                              f"{len(failed_calls)} of the last {len(d.calls)} calls.",
                              "Check your phone is not silencing unknown callers (iPhone: Settings → Phone → Silence "
                              "Unknown Callers); save the Twilio number as a contact."))


# ---------------------------------------------------------------------------
# WhatsApp + texting channel (assistant/messaging.py) - added alongside the SMS checks
# ---------------------------------------------------------------------------

def _detected_whatsapp_from(d: Diagnosis) -> str | None:
    """The WhatsApp number he wrote to (inbound ``to=whatsapp:+...``), e.g. after 'join <code>'."""
    for m in d.messages:
        to = str(m.get("to") or "")
        if str(m.get("direction") or "").startswith("inbound") and to.lower().startswith("whatsapp:"):
            return normalise(to.split(":", 1)[1])
    return None


def _whatsapp(client: Any, s: config.Settings, d: Diagnosis) -> None:
    from assistant import messaging

    wa_codes: dict[int, int] = {}
    for m in d.messages:
        if str(m.get("to") or "").lower().startswith("whatsapp:") and m.get("error_code"):
            wa_codes[int(m["error_code"])] = wa_codes.get(int(m["error_code"]), 0) + 1
    for code, n in sorted(wa_codes.items(), key=lambda kv: -kv[1])[:3]:
        e = explain(code)
        assert e is not None
        d.checks.append(Check(f"wa_err_{code}", STATUS_WARN, f"Recent WhatsApp messages failed with error {code} ({n}x)",
                              e["cause"], e["fix"], None, e["docs"]))
    seen = _detected_whatsapp_from(d)
    if seen:
        db.kv_set("twilio_doctor:detected_whatsapp", seen)
    if seen and normalise(s.whatsapp_from) != seen:
        d.checks.append(Check("wa_from", STATUS_WARN, "You messaged a WhatsApp number ClipAgent is not using",
                              f"Your phone wrote to {seen}; WHATSAPP_FROM is {s.whatsapp_from or 'empty'}.",
                              "Press Fix it to use that number for WhatsApp.", "use_detected_whatsapp_number"))
    # SMS health for the auto channel: newest outbound SMS from our number
    num = d.number.get("phone_number") or normalise(s.twilio_from_number)
    for m in d.messages:
        if m.get("from") == num and str(m.get("direction") or "").startswith("outbound"):
            code = int(m.get("error_code") or 0)
            if code in messaging.SMS_BLOCK_CODES:
                messaging.record_sms_error(code)
            elif m.get("status") == "delivered":
                messaging.record_sms_ok()
            break
    camps = [str(c.get("campaign_status") or "").upper() for c in d.a2p.get("campaigns", [])]
    if num and not _is_us_local(num):
        messaging.set_a2p_status("NOT_US_LOCAL")
    elif camps:
        messaging.set_a2p_status(next((w for w in ("VERIFIED", "IN_PROGRESS", "PENDING", "FAILED") if w in camps), camps[0]))
    elif "campaigns_error" not in d.a2p and "services_error" not in d.a2p:
        messaging.set_a2p_status("NO_CAMPAIGN" if d.a2p.get("service") else "NO_SERVICE")


def _texting_checks(s: config.Settings, d: Diagnosis) -> None:
    """Which channel texts take now, and what to do so at least one works."""
    from assistant import messaging

    wa = messaging.whatsapp_state()
    sms = messaging.sms_state()
    mode = messaging.mode(s)
    route = messaging.route_text(refresh=False)
    d.number.setdefault("texting", {"mode": mode, "sms": sms, "whatsapp": wa["state"], "route": route.transport,
                                    "hold": route.hold, "reason": route.reason})
    if mode == "sms" and sms in ("blocked", "waiting_a2p"):
        d.checks.append(Check("channel", STATUS_FAIL, "Texts are set to SMS, but SMS is blocked until A2P approval",
                              messaging.SMS_STATE_TEXT[sms],
                              "Press Fix it to switch to Auto (WhatsApp now, SMS once approved). Set up WhatsApp first "
                              "under Settings > Your phone.", "channel_auto"))
    if not wa["configured"]:
        if sms != "ok":
            d.checks.append(Check("whatsapp", STATUS_WARN, "WhatsApp is not set up",
                                  "Until the A2P campaign is approved, WhatsApp is the way to get texts today.",
                                  "Settings > Your phone > Texting: enter the sandbox number and join code, then send "
                                  "the join message from WhatsApp."))
        return
    if wa["state"] == "not_joined":
        d.checks.append(Check("whatsapp", STATUS_WARN, "WhatsApp sandbox not joined (or the 3 days ran out)", wa["text"],
                              explain(63015)["fix"], docs=f"{DOCS}63015"))  # type: ignore[index]
    elif wa["state"] == "closed":
        d.checks.append(Check("whatsapp", STATUS_INFO, "WhatsApp window closed",
                              "Free-form WhatsApp messages only go out within 24 hours of your last message.",
                              "Send any WhatsApp message to reopen it; held messages go out at once."))
    else:
        d.checks.append(Check("whatsapp", STATUS_PASS, "WhatsApp ready", wa["text"]))
    if route.hold:
        d.checks.append(Check("held", STATUS_INFO, "New texts are being held", route.reason,
                              "Send a WhatsApp message to the sandbox number to receive them."))


def diagnose(*, remote: bool = True) -> Diagnosis:
    s = config.get_settings()
    d = Diagnosis(ok=True, checks=_local_checks(s), checked_at=db.now_iso())
    creds = bool(s.twilio_account_sid and s.twilio_auth_token)
    if remote and creds:
        client = _get_client()
        if _account(client, s, d):
            number = _number(client, s, d)
            _a2p(client, s, d, number)
            _history(client, s, d)
            _whatsapp(client, s, d)
    _texting_checks(s, d)
    d.ok = not any(c.status == STATUS_FAIL for c in d.checks)
    db.kv_set("twilio_doctor:last", json.dumps({"at": d.checked_at, "ok": d.ok,
                                                "fails": [c.id for c in d.checks if c.status == STATUS_FAIL]}))
    db.log_event("assistant", "twilio check: " + ("healthy" if d.ok else "problems found"),
                 level="info" if d.ok else "warning", data={"fails": [c.id for c in d.checks if c.status == STATUS_FAIL]})
    return d


# ---------------------------------------------------------------------------
# fixes (safe, reversible, owner-approved)
# ---------------------------------------------------------------------------

class FixError(Exception):
    pass


def _remember_prev(what: str, value: Any) -> None:
    db.kv_set(KV_PREV.format(what=what), json.dumps(value))


def apply_fix(action: str) -> str:
    """Apply one fix. Returns a sentence saying what changed. Raises :class:`FixError`."""
    s = config.get_settings()
    if action not in FIX_ACTIONS:
        raise FixError(f"unknown fix {action!r}")
    if action in ("channel_auto", "channel_whatsapp", "channel_sms", "use_detected_whatsapp_number"):
        return _texting_fix(action, s)
    if action == "normalise_numbers":
        import config_store

        changes = {}
        for key, v in (("TWILIO_FROM_NUMBER", s.twilio_from_number), ("OWNER_PHONE", s.owner_phone)):
            n = normalise(v)
            if v and n and n != v:
                changes[key] = n
        if not changes:
            return "The numbers were already in the right form."
        _remember_prev("numbers", {"TWILIO_FROM_NUMBER": s.twilio_from_number, "OWNER_PHONE": s.owner_phone})
        config_store.update(changes)
        config_store.reload()
        db.log_event("assistant", "twilio fix: phone numbers normalised", data={"keys": sorted(changes)})
        return f"Rewrote {', '.join(sorted(changes))} in +1XXXXXXXXXX form."
    client = _get_client()
    num = normalise(s.twilio_from_number) or s.twilio_from_number
    found = client.incoming_phone_numbers.list(phone_number=num, limit=1)
    if not found:
        raise FixError("The From number is not on this Twilio account, so there is nothing to update.")
    n = found[0]
    if action == "set_sms_webhook":
        host = (urlparse(s.public_base_url).hostname or "").lower()
        if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or not host:
            raise FixError("PUBLIC_BASE_URL is a local address; set your tunnel's https address first.")
        _remember_prev("sms_webhook", {"sms_url": getattr(n, "sms_url", None), "sms_method": getattr(n, "sms_method", None),
                                       "sms_application_sid": getattr(n, "sms_application_sid", None)})
        kw: dict[str, Any] = {"sms_url": expected_sms_url(), "sms_method": "POST"}
        if getattr(n, "sms_application_sid", None):
            kw["sms_application_sid"] = ""
        client.incoming_phone_numbers(n.sid).update(**kw)
        db.log_event("assistant", "twilio fix: SMS webhook set", data={"url": expected_sms_url()})
        return f"Incoming texts to {num} now go to {expected_sms_url()} (undo: revert_sms_webhook)."
    if action == "revert_sms_webhook":
        raw = db.kv_get(KV_PREV.format(what="sms_webhook"))
        if not raw:
            raise FixError("There is no earlier webhook setting to go back to.")
        prev = json.loads(raw) if isinstance(raw, str) else raw
        kw = {"sms_url": prev.get("sms_url") or "", "sms_method": prev.get("sms_method") or "POST"}
        if prev.get("sms_application_sid"):
            kw["sms_application_sid"] = prev["sms_application_sid"]
        client.incoming_phone_numbers(n.sid).update(**kw)
        db.log_event("assistant", "twilio fix reverted: SMS webhook")
        return "Put the number's SMS webhook back the way it was."
    # service_use_number_webhook
    svc_sid = None
    for svc in client.messaging.v1.services.list(limit=20):
        nums = [getattr(p, "phone_number", None) for p in client.messaging.v1.services(svc.sid).phone_numbers.list(limit=50)]
        if num in nums:
            svc_sid = svc.sid
            _remember_prev("service_webhook", {"sid": svc.sid,
                                               "use_inbound_webhook_on_number": getattr(svc, "use_inbound_webhook_on_number", None)})
            break
    if not svc_sid:
        raise FixError("The number is not in a Messaging Service; nothing to change.")
    client.messaging.v1.services(svc_sid).update(use_inbound_webhook_on_number=True)
    db.log_event("assistant", "twilio fix: messaging service uses the number's webhook")
    return "The Messaging Service now hands incoming texts to the number's own webhook (ClipAgent)."


def _texting_fix(action: str, s: config.Settings) -> str:
    import config_store

    if action.startswith("channel_"):
        new = action.split("_", 1)[1]
        _remember_prev("channel", {"MESSAGING_CHANNEL": s.messaging_channel})
        config_store.update({"MESSAGING_CHANNEL": new})
        config_store.reload()
        db.log_event("assistant", f"twilio fix: texting channel set to {new}", data={"was": s.messaging_channel})
        return f"Texts now use: {new} (was {s.messaging_channel})."
    raw = db.kv_get("twilio_doctor:detected_whatsapp")
    if not raw:
        raise FixError("Run the check first; no WhatsApp number was detected.")
    _remember_prev("whatsapp_from", {"WHATSAPP_FROM": s.whatsapp_from})
    config_store.update({"WHATSAPP_FROM": str(raw)})
    config_store.reload()
    db.log_event("assistant", "twilio fix: WHATSAPP_FROM set from detected number")
    return f"WhatsApp now sends from {raw}."


def _approval_handler(decision: Any, data: dict[str, Any]) -> bool:
    try:
        msg = apply_fix(str(data.get("action")))
    except Exception as exc:
        db.log_event("assistant", "twilio fix failed", level="warning", data={"error": str(exc)[:300]})
        return False
    log.info("twilio fix applied: %s", msg)
    return True


# ---------------------------------------------------------------------------
# tools + CLI
# ---------------------------------------------------------------------------

def _tool_diagnose(ctx: Any, a: dict[str, Any]) -> Any:
    d = diagnose()
    return {"ok": d.ok, "summary": d.summary(),
            "problems": [asdict(c) for c in d.checks if c.status in (STATUS_FAIL, STATUS_WARN)],
            "account": d.account, "a2p": {k: v for k, v in d.a2p.items() if k != "brands"} | {"brands": d.a2p.get("brands")},
            "recent_errors": [m for m in d.messages if m.get("error_code")][:5],
            "fix_buttons": "Dashboard → Settings → Twilio check has a Fix it button for each fixable problem."}


def _tool_fix(ctx: Any, a: dict[str, Any]) -> Any:
    import notify

    action = a["action"]
    if action not in FIX_ACTIONS:
        from assistant.tools import ToolError

        raise ToolError(f"action must be one of {', '.join(FIX_ACTIONS)}")
    did = notify.request_approval("other", f"Twilio fix: {action.replace('_', ' ')}",
                                  {"action": "assistant_action", "handler": "twilio_fix", "data": {"action": action}})
    return {"approval_requested": did, "note": f"He approves with YES {did} (or the dashboard button)."}


def register_tools(register: Callable[..., None]) -> None:
    from assistant.tools import Tool, _obj, register_approval_handler

    register(Tool("twilio_diagnose",
                  "Check why texts or calls to the owner fail: credentials, number formats, public webhook address, "
                  "trial vs full account, the number's SMS webhook, A2P 10DLC brand/campaign status, recent message "
                  "errors, WhatsApp sandbox join / 24-hour window, which channel texts use now. Each problem comes "
                  "with the exact fix. Read-only.", _obj({}), _tool_diagnose, owner=MODULE))
    register(Tool("twilio_fix",
                  "Ask the owner to approve one safe, reversible Twilio fix found by twilio_diagnose: set_sms_webhook, "
                  "revert_sms_webhook, service_use_number_webhook, normalise_numbers, channel_auto / channel_whatsapp / "
                  "channel_sms (which pipe texts use), use_detected_whatsapp_number. Nothing changes until he says YES.",
                  _obj({"action": {"type": "string", "enum": list(FIX_ACTIONS)}}, ["action"]), _tool_fix,
                  owner=MODULE, side_effects=True))
    register_approval_handler("twilio_fix", _approval_handler)


def format_text(d: Diagnosis) -> str:
    icon = {STATUS_PASS: "OK  ", STATUS_WARN: "WARN", STATUS_FAIL: "FAIL", STATUS_INFO: "INFO"}
    lines = []
    for c in d.checks:
        lines.append(f"[{icon[c.status]}] {c.title}" + (f" - {c.detail}" if c.detail else ""))
        if c.fix and c.status != STATUS_PASS:
            lines.append(f"       fix: {c.fix}" + (f"  (python -m assistant.twilio_doctor fix {c.action})" if c.action else ""))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m assistant.twilio_doctor", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("check")
    c.add_argument("--json", action="store_true")
    c.add_argument("--offline", action="store_true", help="local checks only")
    f = sub.add_parser("fix")
    f.add_argument("action", choices=FIX_ACTIONS)
    sub.add_parser("explain").add_argument("code", type=int)
    args = ap.parse_args(argv)
    if args.cmd == "fix":
        try:
            print(apply_fix(args.action))
        except FixError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return 0
    if args.cmd == "explain":
        print(json.dumps(explain(args.code), indent=2, ensure_ascii=False))
        return 0
    d = diagnose(remote=not getattr(args, "offline", False))
    print(json.dumps(d.to_dict(), indent=2, default=str) if getattr(args, "json", False) else format_text(d))
    return 0 if d.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
