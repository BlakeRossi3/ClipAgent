"""Web setup wizard + Settings page: everything the owner used to type into a terminal.

FastAPI ``router`` (prefix ``/setup``), templates ``setup.html``, assets ``static/setup.css|js``.
While setup is not finished (``SETUP_COMPLETE``), ``/`` and ``/dashboard/`` redirect here and the
pages run as a step-by-step wizard; afterwards ``/setup/`` is the permanent Settings page
(same forms + Update / Restart / Stop / health check buttons).

Steps: about, brain, money, phone, youtube, memory, power, done (see :data:`STEPS`).

Security (the pages can set secrets and update / restart the app):

* Every request needs the dashboard's auth (``dashboard._guard``). The "open" mode (no
  ``DASHBOARD_TOKEN``, or the Windows launcher's ``DASHBOARD_TRUST_LOOPBACK``) is accepted only for
  requests made directly on this machine (``dashboard._is_direct_local``); never
  ``DASHBOARD_ALLOW_UNAUTHENTICATED``.
* Every POST: CSRF field + same-origin ``Origin`` (``dashboard._csrf_ok``) and a required
  ``Content-Length`` under a per-route limit (uploads included).
* Secrets are written to ``config.env`` only (:mod:`config_store`); pages show "saved", never
  the value; events log key names only.
* "Check for updates" runs exactly ``git pull --ff-only origin <current branch>`` in the app
  folder and, when ``requirements.txt`` changed, ``<this python> -m pip install -r requirements.txt``
  (argv lists, never a shell), then restarts.
* YouTube: OAuth code flow with PKCE and a server-side ``state`` (kv, 15 min, single use),
  loopback redirect ``http://127.0.0.1:<port>/setup/youtube/callback``.
* Vault import: zip / tar(.gz) of ``.md`` notes; absolute paths, ``..``, links, special files,
  non-``.md`` files and oversized members are refused; existing notes are never overwritten.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Callable, Mapping
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse, Response

import config
import config_store
import db

log = logging.getLogger(__name__)

router = APIRouter(prefix="/setup", tags=["setup"])

REPO_ROOT = Path(__file__).resolve().parent
STEPS: list[tuple[str, str]] = [
    ("about", "About you"), ("brain", "The brain"), ("money", "Job & money"), ("phone", "Your phone"),
    ("youtube", "YouTube"), ("memory", "Memory"), ("power", "Power & privacy"), ("done", "All set"),
]
STEP_KEYS = [k for k, _ in STEPS]
MAX_FORM_BYTES = 16 * 1024
MAX_SECRET_FILE_BYTES = 64 * 1024
MAX_ARCHIVE_BYTES = 25 * 1024 * 1024       # the uploaded .zip / .tgz
MAX_ARCHIVE_TOTAL = 60 * 1024 * 1024       # uncompressed notes in it
MAX_ARCHIVE_MEMBERS = 5000
OAUTH_TTL = timedelta(minutes=15)
DOCS = {"twilio": "TWILIO_SETUP.md", "youtube": "YOUTUBE_SETUP.md", "windows": "WINDOWS.md"}
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,100}$")
E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")
API_KEY_RE = re.compile(r"^sk-ant-[A-Za-z0-9_\-]{10,300}$")

NOTES = {
    "saved": ("good", "Saved."),
    "skipped": ("info", "Skipped - you can come back to it any time from Settings."),
    "secret_saved": ("good", "Google sign-in file saved. Now press Connect YouTube."),
    "youtube_ok": ("good", "YouTube connected. The token is saved on this computer."),
    "finished": ("good", "Setup finished."),
    "restart_needed": ("warning", "Saved. Some of these changes apply after a Restart (button below)."),
    "signed_out": ("info", "Signed out. The assistant is back on practice mode (or the API key, if you saved one)."),
    "code_bad": ("warning", "That code was not accepted here. Copy the whole code from Claude's page and try again."),
}
KV_LAST_STEP = "setup:last_step"


# ===========================================================================
# auth + rendering (reuses the dashboard's security helpers)
# ===========================================================================

def _d():
    import dashboard

    return dashboard


def _local_only_msg() -> str:
    return ("The settings pages only open on the computer ClipAgent runs on, or with your dashboard "
            "password (DASHBOARD_TOKEN).")


def _guard(request: Request) -> tuple[Response | None, str]:
    d = _d()
    resp, method = d._guard(request)
    if resp is not None:
        return resp, method
    if method == "open" and not d._is_direct_local(request):
        return d._render(request, "error.html", {"title": "Settings locked", "message": _local_only_msg()},
                         status_code=403, nav="login"), method
    return None, method


async def _post_guard(request: Request, max_bytes: int = MAX_FORM_BYTES) -> tuple[Response | None, Any]:
    """Auth + size limit + CSRF/Origin for every POST. Returns (error response, form)."""
    d = _d()
    resp, method = _guard(request)
    if resp is not None:
        return resp, None
    length = request.headers.get("content-length", "")
    if not length.isdigit():
        return _error(request, "Length required", "The upload did not say how big it is.", 411), None
    if int(length) > max_bytes:
        return _error(request, "Too big", f"That is larger than the {max_bytes // 1024:,} KB this form accepts.",
                      413), None
    form = await request.form(max_files=1, max_fields=60)
    if not d._csrf_ok(request, method, form):
        return _error(request, "Forbidden", d._FLASH["csrf"][1], 403), None
    return None, form


def _error(request: Request, title: str, message: str, status: int) -> Response:
    return _d()._render(request, "error.html", {"title": title, "message": message}, status_code=status,
                        nav="settings")


def _redirect(url: str) -> Response:
    return _d()._security_headers(RedirectResponse(url, status_code=303))


def _asettings() -> Any:
    from assistant.settings import get_assistant_settings

    return get_assistant_settings()


def _salute() -> str:
    hour = db.local_now().hour
    part = "morning" if 5 <= hour < 12 else "afternoon" if 12 <= hour < 17 else "evening"
    return f"Good {part}, {_asettings().owner_honorific}."


def _render(request: Request, step: str, ctx: Mapping[str, Any] | None = None, *, status: int = 200) -> Response:
    import home

    s = config.get_settings()
    note = NOTES.get(request.query_params.get("msg", ""))
    idx = STEP_KEYS.index(step) if step in STEP_KEYS else -1
    base = {
        "step": step, "steps": STEPS, "idx": idx, "wizard": not s.setup_complete, "s": s, "a": _asettings(),
        "note": note, "errors": {}, "result": None, "salute": _salute(), "windows": _is_windows(),
        "butler": home.sprite_svg("main", size=72, cls="setup-butler"),
        "next_step": STEP_KEYS[idx + 1] if 0 <= idx < len(STEP_KEYS) - 1 else None,
        "prev_step": STEP_KEYS[idx - 1] if idx > 0 else None,
        "values": current_values(step), "secrets": secret_flags(),
    }
    base.update(ctx or {})
    if step in ("brain", "hub") and "cc" not in base:
        base["cc"] = claude_status(probe=False)
    if step == "brain" and "powers" not in base:
        base["powers"] = sterling_powers()
    return _d()._render(request, "setup.html", base, status_code=status, nav="settings")


def _is_windows() -> bool:
    return sys.platform == "win32"


# ===========================================================================
# current values (never secrets)
# ===========================================================================

def secret_flags() -> dict[str, bool]:
    s = config.get_settings()
    return {"api_key": bool(s.anthropic_api_key), "twilio_token": bool(s.twilio_auth_token),
            "dashboard_token": bool(s.dashboard_token)}


def _job() -> Any:
    try:
        from assistant import store

        jobs = store.list_jobs()
    except Exception:
        return None
    return jobs[0] if jobs else None


def current_values(step: str) -> dict[str, Any]:
    s = config.get_settings()
    a = _asettings()
    if step == "about":
        return {"owner_name": s.owner_name, "honorific": a.owner_honorific, "assistant_name": a.name,
                "timezone": s.timezone}
    if step == "brain":
        return {"pipeline_cap": f"{s.max_daily_llm_budget_usd:g}", "assistant_cap": f"{a.daily_budget_usd:g}",
                "assistant_on": a.enabled or not s.setup_complete, "fake": s.fake_llm, "backend": a.backend,
                "turns_cap": a.max_turns_per_day}
    if step == "money":
        j = _job()
        return {"job_name": j.name if j else "", "rate": f"{j.hourly_rate:g}" if j else "",
                "schedule": j.pay_schedule if j else "biweekly",
                "tax": f"{j.est_tax_rate * 100:g}" if j else "20"}
    if step == "phone":
        return {"owner_phone": s.owner_phone, "twilio_sid": s.twilio_account_sid, "twilio_from": s.twilio_from_number,
                "public_url": s.public_base_url, **texting_values()}
    if step == "youtube":
        return youtube_status()
    if step == "memory":
        return memory_status()
    if step == "power":
        from voice.settings import form_values as voice_values

        return {"whisper_device": "cuda" if s.whisper_device == "cuda" else "cpu", "dry_run": s.dry_run,
                "startup": start_with_windows(), **voice_values()}
    return {}


def _f(form: Any, name: str, limit: int = 200) -> str:
    return " ".join(str(form.get(name) or "").split())[:limit]


def _money(raw: str, lo: float, hi: float) -> float | None:
    try:
        v = float(raw.replace("$", "").replace(",", "").strip())
    except ValueError:
        return None
    return v if lo <= v <= hi else None


def normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"[\s\-().]", "", raw or "")
    if re.fullmatch(r"\d{10}", digits):
        digits = "+1" + digits
    elif re.fullmatch(r"1\d{10}", digits):
        digits = "+" + digits
    return digits if E164_RE.match(digits) else None


def _save(changes: dict[str, str | None]) -> list[str]:
    s = config.get_settings()
    if not s.setup_complete and "SETUP_COMPLETE" not in config_store.read_values():
        changes = {**changes, "SETUP_COMPLETE": "false"}  # an existing file otherwise counts as "set up"
    return config_store.update(changes)


def resume_step() -> str:
    """Where an unfinished wizard continues: the last step page opened, else inferred from the
    saved file (a name saved means "About you" is done)."""
    try:
        last = db.kv_get(KV_LAST_STEP)
    except Exception:  # a broken DB must not lock the wizard
        last = None
    if last in STEP_KEYS:
        return str(last)
    vals = config_store.read_values()
    return "brain" if vals.get("OWNER_NAME") else "about"


def _note_step(step: str) -> None:
    if step in STEP_KEYS and not config.get_settings().setup_complete:
        try:
            db.kv_set(KV_LAST_STEP, step)
        except Exception:
            pass


# ===========================================================================
# Claude account (the assistant's brain on the owner's subscription; assistant/claude_code.py)
# ===========================================================================

def claude_status(*, probe: bool = True) -> dict[str, Any]:
    from assistant import claude_code

    try:
        out = claude_code.status(probe=probe)
    except Exception as exc:  # the page must render whatever the CLI does
        out = {"installed": False, "logged_in": None, "error": type(exc).__name__}
    out["signin"] = claude_code.SIGNIN.snapshot()
    return out


def _claude_signed_in(model: str) -> None:
    """Sign-in + test succeeded: the assistant (all agents) runs on the Claude account from now on."""
    _save({"ASSISTANT_BACKEND": "claude_code", "FAKE_ASSISTANT": "false", "ASSISTANT_ENABLED": "true"})
    db.log_event("setup", "assistant switched to the Claude account", data={"model": model})


def _claude_signed_out() -> None:
    _save({"ASSISTANT_BACKEND": "api", "FAKE_ASSISTANT": None, "CLAUDE_CODE_OAUTH_TOKEN": None})


# ===========================================================================
# step savers: form -> (errors, config changes); side effects only when valid
# ===========================================================================

def save_about(form: Any) -> tuple[dict[str, str], dict[str, str | None]]:
    errors: dict[str, str] = {}
    name, hon, aname, tz = (_f(form, "owner_name", 60), _f(form, "honorific", 30), _f(form, "assistant_name", 30),
                            _f(form, "timezone", 64))
    if not name:
        errors["owner_name"] = "Please tell me your name."
    if not hon:
        errors["honorific"] = "How should I address you? (for example: sir)"
    if not aname:
        errors["assistant_name"] = "Give your assistant a name."
    try:
        ZoneInfo(tz)
        if "/" not in tz and tz != "UTC":
            raise ZoneInfoNotFoundError(tz)
    except (ZoneInfoNotFoundError, ValueError):
        errors["timezone"] = "Use a time zone like America/New_York."
    return errors, {"OWNER_NAME": name, "OWNER_HONORIFIC": hon, "ASSISTANT_NAME": aname, "TIMEZONE": tz}


def save_brain(form: Any) -> tuple[dict[str, str], dict[str, str | None]]:
    errors: dict[str, str] = {}
    changes: dict[str, str | None] = {}
    key = str(form.get("api_key") or "").strip()
    if key and not API_KEY_RE.match(key):
        errors["api_key"] = "That does not look like an Anthropic key (it starts with sk-ant-)."
    pipe = _money(str(form.get("pipeline_cap") or ""), 0.1, 50)
    asst = _money(str(form.get("assistant_cap") or ""), 0, 50)
    if pipe is None:
        errors["pipeline_cap"] = "A dollar amount between 0.10 and 50."
    if asst is None:
        errors["assistant_cap"] = "A dollar amount between 0 and 50."
    if errors:
        return errors, {}
    has_key = bool(key) or (bool(config.get_settings().anthropic_api_key) and not form.get("clear_api_key"))
    if key:
        changes["ANTHROPIC_API_KEY"] = key
    elif form.get("clear_api_key"):
        changes["ANTHROPIC_API_KEY"] = ""
    changes["FAKE_LLM"] = "false" if has_key else "true"   # no key: free practice mode
    changes["MAX_DAILY_LLM_BUDGET_USD"] = f"{pipe:.2f}"
    changes["ASSISTANT_DAILY_BUDGET_USD"] = f"{asst:.2f}"
    changes["ASSISTANT_ENABLED"] = "true" if form.get("assistant_on") else "false"
    return errors, changes


def save_money(form: Any) -> tuple[dict[str, str], dict[str, str | None]]:
    from assistant import ledger, store

    errors: dict[str, str] = {}
    name = _f(form, "job_name", 80)
    rate = _money(str(form.get("rate") or ""), 0.01, 10_000)
    tax = _money(str(form.get("tax") or "0"), 0, 60)
    schedule = str(form.get("schedule") or "biweekly")
    if not name:
        errors["job_name"] = "What is the job called? (e.g. the company name)"
    if rate is None:
        errors["rate"] = "Your hourly pay before tax, e.g. 18.50"
    if tax is None:
        errors["tax"] = "A percentage between 0 and 60."
    if schedule not in store.PAY_SCHEDULES:
        errors["schedule"] = "Pick how often you are paid."
    if not errors:
        ledger.set_job(name, hourly_rate=rate, pay_schedule=schedule, est_tax_rate=(tax or 0) / 100)
    return errors, {}


def save_phone(form: Any) -> tuple[dict[str, str], dict[str, str | None]]:
    errors: dict[str, str] = {}
    changes: dict[str, str | None] = {}
    raw_phone, sid, from_raw = _f(form, "owner_phone", 30), _f(form, "twilio_sid", 64), _f(form, "twilio_from", 30)
    token = str(form.get("twilio_token") or "").strip()
    url = _f(form, "public_url", 200).rstrip("/")
    phone = normalize_phone(raw_phone) if raw_phone else ""
    if phone is None:
        errors["owner_phone"] = "Your mobile number, e.g. +1 555 123 4567"
    from_num = normalize_phone(from_raw) if from_raw else ""
    if from_num is None:
        errors["twilio_from"] = "The Twilio number you bought, e.g. +1 555 000 1111"
    if sid and not re.fullmatch(r"AC[0-9a-fA-F]{32}", sid):
        errors["twilio_sid"] = "The Account SID starts with AC and has 34 characters."
    if token and not re.fullmatch(r"[0-9a-fA-F]{32}", token):
        errors["twilio_token"] = "The Auth Token is 32 letters and numbers (Twilio console home page)."
    if url:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname or p.path not in ("",) or p.query:
            errors["public_url"] = "A web address like https://clipagent.example.com (nothing after the name)."
    if errors:
        return errors, {}
    changes.update({"OWNER_PHONE": phone, "TWILIO_ACCOUNT_SID": sid, "TWILIO_FROM_NUMBER": from_num})
    if url:
        changes["PUBLIC_BASE_URL"] = url
    if token:
        changes["TWILIO_AUTH_TOKEN"] = token
    elif form.get("clear_twilio_token"):
        changes["TWILIO_AUTH_TOKEN"] = ""
    _save_texting(form, errors, changes)
    return (errors, {}) if errors else (errors, changes)


def save_power(form: Any) -> tuple[dict[str, str], dict[str, str | None]]:
    errors: dict[str, str] = {}
    device = str(form.get("whisper_device") or "cpu")
    if device not in ("cpu", "cuda"):
        errors["whisper_device"] = "Choose CPU or GPU."
    dry = str(form.get("dry_run") or "true")
    if dry not in ("true", "false"):
        errors["dry_run"] = "Choose practice or live."
    if errors:
        return errors, {}
    if _is_windows():
        problem = set_start_with_windows(bool(form.get("startup")))
        if problem:
            errors["startup"] = problem
    from voice.settings import parse_form as voice_form

    verrors, vchanges = voice_form(form)
    errors.update(verrors)
    if verrors:
        return errors, {}
    return errors, {"WHISPER_DEVICE": device, "DRY_RUN": dry, **vchanges}


SAVERS: dict[str, Callable[[Any], tuple[dict[str, str], dict[str, str | None]]]] = {
    "about": save_about, "brain": save_brain, "money": save_money, "phone": save_phone, "power": save_power,
}
_SECRET_FIELDS = {"api_key", "twilio_token"}


# ===========================================================================
# tests the owner can run from the forms
# ===========================================================================

def test_api_key() -> dict[str, Any]:
    """One tiny real call (Haiku, a few tokens) with the saved key. Fake mode answers offline."""
    import budget
    import llm

    s = config.get_settings()
    if not s.anthropic_api_key:
        return {"ok": False, "title": "No key saved", "text": "Paste your Anthropic key above first."}
    try:
        res = llm.complete("haiku", system="Reply with the single word OK.", user="ping", purpose="setup.test_key",
                           max_tokens=512, cache_system=False)
    except budget.BudgetExceeded:
        return {"ok": False, "title": "Budget reached", "text": "Today's AI budget is used up; try again tomorrow."}
    except llm.LLMError as exc:
        cause = exc.__cause__
        status = getattr(cause, "status_code", None)
        body = str(cause or exc).lower()
        if status == 401:
            text = "Anthropic did not accept that key. Copy it again from console.anthropic.com."
        elif "credit" in body or "billing" in body:
            text = "The key works, but the Anthropic account has no credit. Add some under Billing."
        elif status is None and "configured" in str(exc):
            text = "No key is saved yet."
        else:
            text = f"Anthropic answered with an error ({status or 'no connection'}). Try again in a minute."
        return {"ok": False, "title": "That did not work", "text": text}
    if res.fake:
        return {"ok": True, "title": "Saved", "text": "Practice mode is on, so no real call was made."}
    return {"ok": True, "title": "The key works",
            "text": f"Anthropic answered ({res.model}, cost ${res.cost_usd:.4f})."}


OPT_IN_CONFIRMATION = ("{name}: You're signed up for personal assistant texts (check-ins, reminders, daily "
                       "summaries). Msg frequency varies, up to 8/day. Msg & data rates may apply. "
                       "Reply HELP for help, STOP to opt out.")


def test_text() -> dict[str, Any]:
    import notify

    s = config.get_settings()
    if not s.owner_phone:
        return {"ok": False, "title": "No phone number", "text": "Enter your mobile number above first."}
    a = _asettings()
    # Exactly the opt-in confirmation described in the SMS registration (docs/sms-opt-in.md) - keep in sync.
    nid = notify.sms(OPT_IN_CONFIRMATION.format(name=a.name), kind="setup_test")
    row = db.get_notification(nid)
    status = row.status if row else "failed"
    if status == "dry_run":
        why = ("practice mode (dry run) is on" if s.dry_run else "Twilio is not fully set up")
        return {"ok": True, "title": "Recorded, not sent",
                "text": f"Nothing was sent because {why}. Every would-be text is saved on the dashboard instead. "
                        "Switch dry run off under Power & privacy when you are ready for real texts."}
    if status in ("sent", "queued", "delivered"):
        return {"ok": True, "title": "Text sent", "text": "Check your phone - it should arrive within a minute."}
    return {"ok": False, "title": "Twilio refused it",
            "text": ((row.error if row else "") or "unknown error")[:200]}


# ===========================================================================
# Texting channel: SMS / WhatsApp (Phone step) - kept in one block; see assistant/messaging.py
# ===========================================================================

TEXT_TEST_ACTIONS = {"test_sms": "sms", "test_whatsapp": "whatsapp"}


def texting_values() -> dict[str, Any]:
    s = config.get_settings()
    try:
        from assistant import messaging

        wa = messaging.whatsapp_state()
        sms = messaging.sms_state()
        route = messaging.route_text(refresh=False)
        status = {"sms_state": sms, "sms_text": messaging.SMS_STATE_TEXT.get(sms, sms), "wa": wa,
                  "route": route.transport, "route_hold": route.hold, "route_reason": route.reason,
                  "a2p": messaging.a2p_status(), "sandbox_hint": messaging.SANDBOX_NUMBER_HINT}
    except Exception as exc:  # status is decoration; the form must still render
        log.warning("texting status failed: %s", exc)
        status = {"error": str(exc)[:200]}
    return {"messaging_channel": s.messaging_channel, "whatsapp_from": s.whatsapp_from,
            "whatsapp_join_code": s.whatsapp_join_code, "texting": status}


def _save_texting(form: Any, errors: dict[str, str], changes: dict[str, str | None]) -> None:
    """Only fields present in the form change (older forms / API posts leave them alone)."""
    if "messaging_channel" in form:
        ch = str(form.get("messaging_channel") or "auto").strip().lower()
        if ch not in ("auto", "sms", "whatsapp"):
            errors["messaging_channel"] = "Choose Auto, SMS or WhatsApp."
        else:
            changes["MESSAGING_CHANNEL"] = ch
    if "whatsapp_from" in form:
        raw = _f(form, "whatsapp_from", 40)
        raw = raw.split(":", 1)[1] if raw.lower().startswith("whatsapp:") else raw
        num = normalize_phone(raw) if raw else ""
        if num is None:
            errors["whatsapp_from"] = "The WhatsApp number from the Twilio sandbox page, e.g. +1 415 523 8886"
        else:
            changes["WHATSAPP_FROM"] = num
    if "whatsapp_join_code" in form:
        code = _f(form, "whatsapp_join_code", 60)
        code = code[5:].strip() if code.lower().startswith("join ") else code
        if code and not re.fullmatch(r"[A-Za-z0-9-]+(?: [A-Za-z0-9-]+)?", code):
            errors["whatsapp_join_code"] = "Just the words after 'join', e.g. orange-tiger"
        else:
            changes["WHATSAPP_JOIN_CODE"] = code


def test_text_via(transport: str) -> dict[str, Any]:
    s = config.get_settings()
    if not s.owner_phone:
        return {"ok": False, "title": "No phone number", "text": "Enter your mobile number above first."}
    if transport == "whatsapp" and not s.whatsapp_from:
        return {"ok": False, "title": "No WhatsApp number", "text": "Enter the sandbox's WhatsApp number above first."}
    from assistant import messaging

    a = _asettings()
    label = "WhatsApp message" if transport == "whatsapp" else "text"
    res = messaging.send_test(transport, f"{a.name}: test {label} from ClipAgent. Reply HELP for commands.")
    st, err = res["status"], (res.get("error") or "")
    if st == "dry_run":
        why = "practice mode (dry run) is on" if s.dry_run else "Twilio is not fully set up"
        return {"ok": True, "title": "Recorded, not sent", "text": f"Nothing was sent because {why}."}
    if st == "sent":
        hint = (" If it does not arrive: SMS from a US local number is blocked until the A2P campaign is approved "
                "(error 30034) - use WhatsApp until then." if transport == "sms" else
                " If it does not arrive, send the join message again from WhatsApp, then retry.")
        return {"ok": True, "title": f"{label.capitalize()} sent", "text": "Twilio accepted it." + hint}
    if st == "queued":
        return {"ok": False, "title": "Held, not sent",
                "text": err.replace("held: ", "") + ". Send any WhatsApp message to the sandbox number, then retry."}
    try:
        from assistant import twilio_doctor

        m = re.search(r"\b(2\d{4}|3\d{4}|6\d{4})\b", err)
        e = twilio_doctor.explain(int(m.group(1))) if m else None
    except Exception:
        e = None
    return {"ok": False, "title": "Twilio refused it",
            "text": (f"{e['cause']} Fix: {e['fix']}" if e else err[:200] or "unknown error")}


# ===========================================================================
# YouTube: client secret upload + OAuth (loopback redirect to this server)
# ===========================================================================

def youtube_status() -> dict[str, Any]:
    import youtube_auth

    s = config.get_settings()
    out: dict[str, Any] = {"has_secret": Path(s.youtube_client_secrets).is_file(), "has_token": False,
                           "scopes_ok": False}
    tp = youtube_auth.token_path()
    if tp.is_file():
        out["has_token"] = True
        try:
            granted = set(json.loads(tp.read_text(encoding="utf-8")).get("scopes") or [])
            out["scopes_ok"] = all(x in granted for x in youtube_auth.SCOPES)
        except (OSError, ValueError):
            pass
    return out


def validate_client_secret(raw: bytes) -> tuple[dict[str, Any] | None, str]:
    """Parse an OAuth client JSON; only Google "Desktop app" (installed) clients are accepted."""
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return None, "That file is not a Google client JSON file."
    if not isinstance(data, dict):
        return None, "That file is not a Google client JSON file."
    if "web" in data:
        return None, ("That client is a 'Web application'. Create an OAuth client of type 'Desktop app' "
                      "and download its JSON instead.")
    inst = data.get("installed")
    need = ("client_id", "client_secret", "auth_uri", "token_uri")
    if not isinstance(inst, dict) or any(not isinstance(inst.get(k), str) or not inst.get(k) for k in need):
        return None, "That JSON is missing the 'installed' client details Google puts in a Desktop-app file."
    if not inst["client_id"].endswith(".apps.googleusercontent.com"):
        return None, "The client_id does not look like a Google OAuth client."
    if not all(inst[k].startswith("https://") for k in ("auth_uri", "token_uri")):
        return None, "The sign-in addresses in that file are not https."
    return data, ""


def make_flow(client_config: Mapping[str, Any], *, redirect_uri: str, state: str | None = None,
              code_verifier: str | None = None) -> Any:
    """A google-auth-oauthlib ``Flow`` (PKCE on). Tests replace :data:`FLOW_FACTORY`."""
    import youtube_auth
    from google_auth_oauthlib.flow import Flow

    kw: dict[str, Any] = {"redirect_uri": redirect_uri}
    if state:
        kw["state"] = state
    if code_verifier:
        kw["code_verifier"] = code_verifier
    else:
        kw["autogenerate_code_verifier"] = True
    return Flow.from_client_config(dict(client_config), scopes=list(youtube_auth.SCOPES), **kw)


FLOW_FACTORY: Callable[..., Any] = make_flow
_OAUTH_KEY = "setup:oauth:{state}"


def _redirect_uri(request: Request) -> str:
    host = request.url.hostname if request.url.hostname in ("127.0.0.1", "localhost") else "127.0.0.1"
    port = request.url.port or 80
    return f"http://{host}:{port}/setup/youtube/callback"


def _client_config() -> dict[str, Any] | None:
    p = Path(config.get_settings().youtube_client_secrets)
    try:
        data, _ = validate_client_secret(p.read_bytes())
    except OSError:
        return None
    return data


# ===========================================================================
# vault import
# ===========================================================================

class ArchiveError(ValueError):
    pass


def _members(data: bytes) -> list[tuple[str, str, int, Callable[[], bytes]]]:
    """(name, kind file|dir|other, size, reader) for a zip or tar(.gz/.bz2/.xz) archive."""
    out: list[tuple[str, str, int, Callable[[], bytes]]] = []
    if zipfile.is_zipfile(io.BytesIO(data)):
        zf = zipfile.ZipFile(io.BytesIO(data))
        for zi in zf.infolist():
            mode = (zi.external_attr >> 16) & 0o170000
            kind = "dir" if zi.is_dir() else ("other" if mode not in (0, 0o100000) else "file")
            out.append((zi.filename, kind, zi.file_size, lambda zi=zi: zf.open(zi).read(vault_max_bytes() + 1)))
        return out
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except tarfile.TarError as exc:
        raise ArchiveError("That is not a .zip or .tgz archive.") from exc
    for ti in tf.getmembers():
        kind = "file" if ti.isreg() else "dir" if ti.isdir() else "other"

        def read(ti: tarfile.TarInfo = ti) -> bytes:
            fh = tf.extractfile(ti)
            return fh.read(vault_max_bytes() + 1) if fh else b""
        out.append((ti.name, kind, ti.size, read))
    return out


def vault_max_bytes() -> int:
    from assistant import vault

    return vault.MAX_NOTE_BYTES


def _clean_name(name: str) -> str | None:
    """Archive member name -> safe relative POSIX path, or None (absolute, drive, ``..``, NUL)."""
    n = name.replace("\\", "/")
    if not n or "\x00" in n or n.startswith("/") or re.match(r"^[A-Za-z]:", n):
        return None
    parts = [p for p in n.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return "/".join(parts)


def import_vault_archive(data: bytes) -> dict[str, Any]:
    """Merge an exported vault into the live one. Existing notes are never overwritten: an
    untouched template note is replaced, an identical note skipped, a different one saved beside
    it as ``<name> (imported).md``. Returns counts + refused names; reindexes the search."""
    from assistant import vault

    if len(data) > MAX_ARCHIVE_BYTES:
        raise ArchiveError("That archive is too large.")
    members = _members(data)
    if len(members) > MAX_ARCHIVE_MEMBERS:
        raise ArchiveError("That archive has too many files.")
    refused: list[str] = []
    files: list[tuple[str, int, Callable[[], bytes]]] = []
    total = 0
    for name, kind, size, read in members:
        rel = _clean_name(name)
        if rel is None:
            raise ArchiveError(f"Unsafe path in the archive: {name[:80]!r}. Nothing was imported.")
        if kind == "dir":
            continue
        if kind != "file":
            raise ArchiveError(f"The archive contains a link or special file ({name[:80]!r}). Nothing was imported.")
        base = PurePosixPath(rel).name
        if base == ".gitkeep":
            continue
        if not rel.lower().endswith(".md") or any(p.startswith(".") for p in rel.split("/")):
            refused.append(rel)  # .obsidian settings, images, ...: not memory
            continue
        if size > vault_max_bytes():
            raise ArchiveError(f"{rel} is larger than a note may be. Nothing was imported.")
        total += size
        if total > MAX_ARCHIVE_TOTAL:
            raise ArchiveError("The notes in that archive are too large in total.")
        files.append((rel, size, read))
    if not files:
        raise ArchiveError("No notes (.md files) found in that archive.")
    # "vault/People/x.md" -> "People/x.md" when everything sits in one wrapper folder
    firsts = {r.split("/", 1)[0] for r, _, _ in files}
    template_top = {p.name for p in vault.TEMPLATE_DIR.iterdir()} if vault.TEMPLATE_DIR.is_dir() else set()
    if len(firsts) == 1 and all("/" in r for r, _, _ in files) and next(iter(firsts)) not in template_top:
        files = [(r.split("/", 1)[1], s, rd) for r, s, rd in files]

    root = vault.ensure_vault()
    added = replaced = same = renamed = 0
    for rel, _size, read in files:
        raw = read()
        if len(raw) > vault_max_bytes():
            raise ArchiveError(f"{rel} is larger than a note may be.")
        try:
            text = raw.decode("utf-8-sig").replace("\r\n", "\n")
        except UnicodeDecodeError:
            refused.append(rel)
            continue
        try:
            target, rel_norm = vault.safe_path(rel)
        except vault.VaultError:
            refused.append(rel)
            continue
        if target.exists():
            current = target.read_text(encoding="utf-8")
            if current == text:
                same += 1
                continue
            tmpl = vault.TEMPLATE_DIR / rel_norm
            if tmpl.is_file() and current == tmpl.read_text(encoding="utf-8").replace(
                    "{{date}}", db.local_today().isoformat()):
                vault._atomic_write(target, text)
                replaced += 1
                continue
            alt_rel = rel_norm[:-3] + " (imported).md"
            target, rel_norm = vault.safe_path(alt_rel)
            if target.exists():
                same += 1
                continue
            renamed += 1
        else:
            added += 1
        vault._atomic_write(target, text)
    stats = vault.reindex()
    result = {"added": added, "replaced_template": replaced, "unchanged": same, "kept_both": renamed,
              "refused": refused[:20], "refused_count": len(refused), "root": str(root), "index": str(stats)}
    db.log_event("setup", "vault archive imported", data={k: v for k, v in result.items() if k != "refused"})
    return result


def memory_status() -> dict[str, Any]:
    a = _asettings()
    count = 0
    try:
        from assistant import vault

        count = len(vault.list_notes())
    except Exception:
        pass
    return {"vault_dir": str(a.vault_dir), "notes": count}


# ===========================================================================
# Windows integration: start with Windows, open the data folder
# ===========================================================================

STARTUP_LNK = "ClipAgent.lnk"
# setup.ps1 makes this shortcut (launcher with -NoBrowser) next to the app folder.
BACKGROUND_LNK = REPO_ROOT.parent / "ClipAgent (background).lnk"


def startup_dir() -> Path:
    return Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def start_with_windows() -> bool:
    return _is_windows() and (startup_dir() / STARTUP_LNK).is_file()


def set_start_with_windows(on: bool) -> str | None:
    """Copy / remove the background shortcut in the Startup folder. Returns a problem or None."""
    target = startup_dir() / STARTUP_LNK
    try:
        if on:
            if not BACKGROUND_LNK.is_file():
                return "The ClipAgent shortcut is missing - run ClipAgent-Setup.bat again, then retry."
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(BACKGROUND_LNK, target)
        else:
            target.unlink(missing_ok=True)
    except OSError as exc:
        return f"Windows refused: {exc.strerror or exc}"
    return None


# ===========================================================================
# maintenance: update / restart / stop
# ===========================================================================

def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=str(REPO_ROOT), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, check=False)


def _req_digest() -> str:
    import hashlib

    try:
        return hashlib.sha256((REPO_ROOT / "requirements.txt").read_bytes()).hexdigest()
    except OSError:
        return ""


def update_app() -> dict[str, Any]:
    """git pull --ff-only on the checked-out branch; pip install when requirements changed."""
    git = shutil.which("git") or "git"
    out: dict[str, Any] = {"ok": False, "updated": False, "log": []}
    try:
        br = _run([git, "rev-parse", "--abbrev-ref", "HEAD"], 30)
        branch = br.stdout.strip()
        if br.returncode != 0 or branch == "HEAD" or not BRANCH_RE.match(branch) or ".." in branch:
            out["log"].append("This app folder is not on a branch, so it cannot update itself.")
            return out
        before = _run([git, "rev-parse", "HEAD"], 30).stdout.strip()
        req_before = _req_digest()
        upstream = _self_edit_upstream(git, branch)
        if upstream:  # the assistant's own branch: merge the published branch into it, keep its changes
            pull = _run([git, *SELF_EDIT_GIT_ID, "pull", "--no-rebase", "--no-edit", "origin", upstream], 300)
        else:
            pull = _run([git, "pull", "--ff-only", "origin", branch], 300)
        out["log"].append((pull.stdout + pull.stderr).strip()[-1500:])
        if pull.returncode != 0:
            if upstream:
                _run([git, "merge", "--abort"], 60)
                out["log"].append("The update conflicts with the assistant's own changes; kept the local version. "
                                  "Undo its changes (Settings > The brain) to take the update.")
            else:
                out["log"].append("The update could not be applied automatically (see above). Nothing was changed.")
            return out
        after = _run([git, "rev-parse", "HEAD"], 30).stdout.strip()
        out["updated"] = before != after
        if out["updated"] and _req_digest() != req_before:
            pip = _run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-r",
                        str(REPO_ROOT / "requirements.txt")], 1800)
            out["log"].append((pip.stdout + pip.stderr).strip()[-1500:])
            if pip.returncode != 0:
                out["log"].append("Installing the new requirements failed; ClipAgent keeps running the old ones.")
                return out
    except (OSError, subprocess.SubprocessError) as exc:
        out["log"].append(f"Could not run git/pip: {exc}")
        return out
    out["ok"] = True
    db.log_event("setup", "update checked", data={"updated": out["updated"]})
    return out


# ===========================================================================
# Sterling's powers (self-editing assistant) - kept in one block; see assistant/selfedit.py
# ===========================================================================

SELF_EDIT_GIT_ID = ["-c", "user.name=Sterling", "-c", "user.email=sterling@clipagent.local"]


def _self_edit_upstream(git: str, branch: str) -> str:
    """On the assistant's self-edit branch: the published branch to merge updates from, else ''."""
    try:
        from assistant.settings import get_assistant_settings

        if branch != get_assistant_settings().self_edit_branch:
            return ""
        up = _run([git, "config", "--get", "clipagent.upstream"], 30).stdout.strip()
    except Exception:
        return ""
    return up if up and BRANCH_RE.match(up) and ".." not in up else ""


def sterling_powers() -> dict[str, Any]:
    """What the assistant can do right now (Settings > The brain)."""
    try:
        from assistant import claude_code, runtime, selfedit, usage

        a = _asettings()
        full = a.backend == "claude_code" and a.cc_full
        return {"full": full, "backend": a.backend, "chrome": a.chrome,
                "tools": list(claude_code.BUILTIN_TOOLS) if full else [],
                "folders": [str(p) for p in claude_code.working_dirs()] if full else [],
                "self_edit": selfedit.powers_status(), "stopped": runtime.is_stopped(),
                "paused": db.kv_get("budget:paused") is not None, "usage": usage.usage_status()}
    except Exception as exc:
        log.warning("powers status failed: %s", exc)
        return {"error": str(exc)[:200]}


def doctor_rows() -> list[dict[str, Any]]:
    import main

    fix_step = {"Anthropic": "brain", "assistant": "brain", "assistant budget": "brain", "pay rate": "money",
                "Twilio": "phone", "owner phone": "phone", "owner phone (assistant)": "phone",
                "PUBLIC_BASE_URL": "phone", "YouTube client secret": "youtube", "YouTube token": "youtube",
                "YouTube token scopes": "youtube", "YouTube refresh token": "youtube", "vault": "memory",
                "DASHBOARD_TOKEN": "power", "DRY_RUN": "power", "DASHBOARD_ALLOW_UNAUTHENTICATED": "power",
                "whisper model": "power", "Claude Code": "brain", "Claude account": "brain",
                "Claude runs today": "brain", "Claude plan limit": "brain", "assistant models": "brain"}
    rows = []
    for c in main.doctor_checks():
        step = fix_step.get(c.name)
        rows.append({"name": c.name, "status": c.status, "detail": c.detail, "step": step,
                     "fix": "" if step else c.fix})
    order = {"fail": 0, "warn": 1, "pass": 2}
    return sorted(rows, key=lambda r: order.get(r["status"], 3))


# ===========================================================================
# routes
# ===========================================================================

@router.get("/", include_in_schema=False)
@router.get("", include_in_schema=False)
def index(request: Request) -> Response:
    resp, _ = _guard(request)
    if resp is not None:
        return resp
    import main

    if not config.get_settings().setup_complete and request.query_params.get("welcome") is None:
        step = resume_step()
        if step != "about":   # he has been here before: straight back to where he stopped
            return _redirect(f"/setup/{step}")
    return _render(request, "welcome" if not config.get_settings().setup_complete else "hub",
                   {"can_control": main.control_available(),
                    "summary": {k: current_values(k) for k in ("about", "brain", "money", "phone", "youtube",
                                                                "memory", "power")},
                    "cc": claude_status(probe=False)})


@router.get("/help/{name}", include_in_schema=False)
def help_page(request: Request, name: str) -> Response:
    resp, _ = _guard(request)
    if resp is not None:
        return resp
    import home

    if name not in DOCS or not (REPO_ROOT / "docs" / DOCS[name]).is_file():
        return _error(request, "Not found", "There is no such help page.", 404)
    html = home.render_markdown((REPO_ROOT / "docs" / DOCS[name]).read_text(encoding="utf-8"))
    return _render(request, "help", {"doc_html": html, "doc_name": name})


@router.get("/youtube/callback", include_in_schema=False)
async def youtube_callback(request: Request) -> Response:
    resp, _ = _guard(request)
    if resp is not None:
        return resp
    qp = request.query_params
    state = qp.get("state") or ""
    pending = db.kv_get(_OAUTH_KEY.format(state=state)) if re.fullmatch(r"[A-Za-z0-9_\-]{16,128}", state) else None
    if not pending:
        return _render(request, "youtube", {"result": {"ok": False, "title": "Sign-in expired",
                                                       "text": "That Google sign-in did not start here or is too "
                                                               "old. Press Connect YouTube again."}}, status=400)
    db.kv_delete(_OAUTH_KEY.format(state=state))   # single use
    if db.parse_iso(pending["created"]) < db.utcnow() - OAUTH_TTL:
        return _render(request, "youtube", {"result": {"ok": False, "title": "Sign-in expired",
                                                       "text": "Press Connect YouTube again."}}, status=400)
    if qp.get("error") or not qp.get("code"):
        return _render(request, "youtube", {"result": {"ok": False, "title": "Not connected",
                                                       "text": "Google did not grant access (you may have pressed "
                                                               "Cancel). Try again when ready."}}, status=400)
    cfg = _client_config()
    if cfg is None:
        return _render(request, "youtube", {"result": {"ok": False, "title": "Sign-in file missing",
                                                       "text": "Upload your Google sign-in file first."}}, status=400)
    import youtube_auth

    try:
        flow = FLOW_FACTORY(cfg, redirect_uri=pending["redirect_uri"], state=state,
                            code_verifier=pending["verifier"])
        await run_in_threadpool(flow.fetch_token, code=qp.get("code"))
        creds = flow.credentials
        youtube_auth.save_credentials(creds, youtube_auth.token_path())
    except Exception as exc:  # oauthlib raises many types (incl. Warning for scope changes)
        db.log_event("setup", "YouTube sign-in failed", level="warning", data={"error": type(exc).__name__})
        return _render(request, "youtube", {"result": {"ok": False, "title": "Google refused the sign-in",
                                                       "text": "Try Connect YouTube again. If it keeps failing, "
                                                               "check the sign-in file is a Desktop-app client."}},
                       status=400)
    db.kv_delete(youtube_auth._AUTH_ALERT_KEY.format(day=db.local_today().isoformat()))
    db.log_event("setup", "YouTube authorised from the setup page",
                 data={"scopes": list(getattr(creds, "scopes", None) or [])})
    if not getattr(creds, "refresh_token", None):
        return _render(request, "youtube", {"result": {
            "ok": False, "title": "Connected, but only for an hour",
            "text": "Google sent no long-term token. Remove ClipAgent at myaccount.google.com/permissions, "
                    "then press Connect YouTube again."}})
    return _redirect("/setup/youtube?msg=youtube_ok")


@router.get("/{step}", include_in_schema=False)
async def step_page(request: Request, step: str) -> Response:
    resp, _ = _guard(request)
    if resp is not None:
        return resp
    if step not in STEP_KEYS:
        return _error(request, "Not found", "There is no such settings page.", 404)
    ctx: dict[str, Any] = {}
    _note_step(step)
    if step == "done":
        ctx["checks"] = await run_in_threadpool(doctor_rows)
    if step == "brain":
        ctx["cc"] = await run_in_threadpool(claude_status)
    return _render(request, step, ctx)


@router.get("/claude/status", include_in_schema=False)
def claude_status_json(request: Request) -> Response:
    """Polled by the brain page while installing / signing in (no secrets in it)."""
    resp, _ = _guard(request)
    if resp is not None:
        return resp
    from assistant import claude_code

    return _d()._json(claude_code.SIGNIN.snapshot())


@router.post("/claude/{action}", include_in_schema=False)
async def claude_action(request: Request, action: str) -> Response:
    """signin | cancel | code | signout - fixed commands only (assistant/claude_code.py)."""
    from assistant import claude_code

    err, form = await _post_guard(request)
    if err is not None:
        return err
    if action == "signin":
        if not _d()._is_direct_local(request):
            return _render(request, "brain", {"cc": claude_status(probe=False), "result": {
                "ok": False, "title": "Use the laptop", "text": "Sign in from the computer ClipAgent runs on: "
                                                                "Claude's sign-in opens in that computer's browser."}},
                           status=400)
        claude_code.forget_status_cache()
        claude_code.SIGNIN.start(on_success=_claude_signed_in)
        return _redirect("/setup/brain#claude")
    if action == "cancel":
        claude_code.SIGNIN.cancel()
        return _redirect("/setup/brain#claude")
    if action == "code":
        ok = claude_code.SIGNIN.submit_code(str(form.get("code") or ""))
        return _redirect("/setup/brain#claude" if ok else "/setup/brain?msg=code_bad#claude")
    if action == "signout":
        await run_in_threadpool(claude_code.sign_out)
        _claude_signed_out()
        return _redirect("/setup/brain?msg=signed_out")
    return _error(request, "Not found", "There is no such action.", 404)


@router.post("/youtube/secret", include_in_schema=False)
async def youtube_secret(request: Request) -> Response:
    err, form = await _post_guard(request, MAX_SECRET_FILE_BYTES + 4096)
    if err is not None:
        return err
    up = form.get("file")
    raw = await up.read(MAX_SECRET_FILE_BYTES + 1) if hasattr(up, "read") else b""
    data, problem = validate_client_secret(raw) if raw and len(raw) <= MAX_SECRET_FILE_BYTES else (
        None, "Choose the .json file you downloaded from Google Cloud.")
    if data is None:
        return _render(request, "youtube", {"errors": {"file": problem}}, status=400)
    dest = config.get_settings().data_dir / "client_secret.json"
    config_store._atomic_write(dest, json.dumps(data, indent=2))
    _save({"YOUTUBE_CLIENT_SECRETS": str(dest)})
    return _redirect("/setup/youtube?msg=secret_saved")


@router.post("/youtube/connect", include_in_schema=False)
async def youtube_connect(request: Request) -> Response:
    err, _form = await _post_guard(request)
    if err is not None:
        return err
    if not _d()._is_direct_local(request):
        return _render(request, "youtube", {"result": {"ok": False, "title": "Use the laptop",
                                                       "text": "Connect YouTube from the computer ClipAgent runs "
                                                               "on (Google sends you back to 127.0.0.1)."}},
                       status=400)
    cfg = _client_config()
    if cfg is None:
        return _render(request, "youtube", {"errors": {"file": "Upload your Google sign-in file first."}},
                       status=400)
    redirect_uri = _redirect_uri(request)
    state = secrets.token_urlsafe(32)
    flow = FLOW_FACTORY(cfg, redirect_uri=redirect_uri, state=state)
    url, got_state = flow.authorization_url(access_type="offline", prompt="consent")
    db.kv_set(_OAUTH_KEY.format(state=got_state or state),
              {"verifier": flow.code_verifier, "redirect_uri": redirect_uri, "created": db.now_iso()})
    if not url.startswith("https://"):
        return _error(request, "Unexpected sign-in address", "The Google sign-in file looks wrong.", 400)
    return _redirect(url)


@router.post("/memory/import", include_in_schema=False)
async def memory_import(request: Request) -> Response:
    err, form = await _post_guard(request, MAX_ARCHIVE_BYTES + 16_384)
    if err is not None:
        return err
    up = form.get("file")
    raw = await up.read(MAX_ARCHIVE_BYTES + 1) if hasattr(up, "read") else b""
    if not raw:
        return _render(request, "memory", {"errors": {"file": "Choose your vault .zip or .tgz file."}}, status=400)
    try:
        res = await run_in_threadpool(import_vault_archive, raw)
    except (ArchiveError, zipfile.BadZipFile, tarfile.TarError, EOFError, OSError) as exc:
        msg = str(exc) if isinstance(exc, ArchiveError) else "That archive could not be read."
        return _render(request, "memory", {"errors": {"file": msg}}, status=400)
    text = (f"{res['added']} note{'s' if res['added'] != 1 else ''} added, {res['replaced_template']} starter notes filled in, "
            f"{res['unchanged']} already there" + (f", {res['kept_both']} kept side by side as '(imported)'"
                                                   if res["kept_both"] else "")
            + (f"; {res['refused_count']} non-note files ignored" if res["refused_count"] else "") + ".")
    return _render(request, "memory", {"result": {"ok": True, "title": "Memory imported", "text": text}})


@router.post("/finish", include_in_schema=False)
async def finish(request: Request) -> Response:
    err, _form = await _post_guard(request)
    if err is not None:
        return err
    config_store.update({"SETUP_COMPLETE": "true"})
    return _redirect("/dashboard/?msg=setup_done")


@router.post("/action/{action}", include_in_schema=False)
async def action(request: Request, action: str) -> Response:
    import main

    err, _form = await _post_guard(request)
    if err is not None:
        return err
    if action == "open-data":
        if not _is_windows():
            return _error(request, "Not available", "Opening folders only works on Windows.", 400)
        os.startfile(str(config.get_settings().data_dir))  # type: ignore[attr-defined]  # noqa: S606
        return _redirect("/setup/")
    if action == "update":
        res = await run_in_threadpool(update_app)
        restarting = res["ok"] and res["updated"] and main.request_shutdown(restart=True)
        title = ("Updated - restarting" if restarting else "Updated" if res["ok"] and res["updated"]
                 else "Already up to date" if res["ok"] else "Update failed")
        return _render(request, "action", {"title": title, "log": res["log"], "ok": res["ok"],
                                           "reload": restarting,
                                           "hint": "" if restarting or not res["updated"]
                                           else "Restart ClipAgent to use the new version."})
    if action in ("restart", "stop"):
        if not main.request_shutdown(restart=action == "restart"):
            return _render(request, "action", {"title": "Not available", "ok": False, "log": [], "reload": False,
                                               "hint": "Restart and Stop work when ClipAgent was started from its "
                                                       "shortcut (python main.py schedule)."})
        if action == "restart":
            return _render(request, "action", {"title": "Restarting", "ok": True, "log": [], "reload": True,
                                               "hint": "This page reloads by itself in a few seconds."})
        return _render(request, "action", {"title": "ClipAgent stopped", "ok": True, "log": [], "reload": False,
                                           "hint": "Start it again any time from the ClipAgent shortcut."})
    return _error(request, "Not found", "There is no such action.", 404)


@router.post("/{step}", include_in_schema=False)
async def step_post(request: Request, step: str) -> Response:
    err, form = await _post_guard(request)
    if err is not None:
        return err
    d = _d()
    if step == "power" and form.get("action") == "new_token":
        token = secrets.token_urlsafe(24)
        _save({"DASHBOARD_TOKEN": token})
        resp = _render(request, "power", {"new_token": token})
        d._set_session_cookie(resp, request, token)   # keep this browser signed in
        return resp
    if step == "power" and form.get("action") == "clear_token":
        _save({"DASHBOARD_TOKEN": ""})
        return _redirect("/setup/power?msg=saved")
    saver = SAVERS.get(step)
    if saver is None:
        return _error(request, "Not found", "There is no such settings page.", 404)
    errors, changes = saver(form)
    if errors:
        submitted = {k: v for k, v in form.items() if k not in _SECRET_FIELDS and isinstance(v, str)}
        return _render(request, step, {"errors": errors, "values": {**current_values(step), **submitted}},
                       status=400)
    restart = _save(changes) if changes else []
    if form.get("action") in TEXT_TEST_ACTIONS and step == "phone":
        result = await run_in_threadpool(test_text_via, TEXT_TEST_ACTIONS[str(form.get("action"))])
        return _render(request, step, {"result": result})
    if form.get("action") == "test" and step in ("brain", "phone"):
        result = await run_in_threadpool(test_api_key if step == "brain" else test_text)
        return _render(request, step, {"result": result})
    if not config.get_settings().setup_complete:
        nxt = STEP_KEYS[STEP_KEYS.index(step) + 1]
        return _redirect(f"/setup/{nxt}")
    return _redirect(f"/setup/{step}?msg={'restart_needed' if restart else 'saved'}")
