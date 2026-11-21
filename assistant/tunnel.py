"""Keep the public web address alive: restart a Cloudflare quick tunnel when it dies.

Twilio reaches ClipAgent (incoming texts, WhatsApp replies, call buttons, delivery receipts) through
``PUBLIC_BASE_URL``. On his laptop that is a Cloudflare *quick* tunnel (``*.trycloudflare.com``): free,
no account, but it dies with the PC or the network and comes back at a NEW random address. When that
happens every reply he texts fails at Twilio with 11200/11210 and nobody notices.

:func:`ensure` (scheduler job ``assistant_tunnel``, every 5 minutes + startup; zero tokens):

* address is not a quick tunnel -> nothing to do (a named tunnel / reverse proxy is his to run);
* it answers ``/healthz`` -> fine;
* two misses in a row (one blip is not a dead tunnel) -> start ``cloudflared tunnel --url
  http://localhost:<port>``, read the new address from its log, save it as ``PUBLIC_BASE_URL``, and
  re-point the Twilio number's SMS webhook *only if it pointed at the old tunnel* (keeps his choice).

The WhatsApp sandbox's inbound URL cannot be set through Twilio's API, so a restart marks it stale
(kv :data:`KV` ``whatsapp_url_stale``) and the Twilio check tells him the exact URL to paste. A named
Cloudflare tunnel (needs his Cloudflare account) would make the address permanent.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

import config
import db

log = logging.getLogger(__name__)

KV = "tunnel:state"
QUICK_SUFFIX = ".trycloudflare.com"
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
MISSES_BEFORE_RESTART = 2
GRACE_SECONDS = 180          # a fresh tunnel's DNS can take a minute or two
WAIT_FOR_URL_SECONDS = 40
DEFAULT_PORT = 8000
_KNOWN_EXE = (r"C:\Program Files (x86)\cloudflared\cloudflared.exe", r"C:\Program Files\cloudflared\cloudflared.exe")


# ---------------------------------------------------------------------------
# seams (tests replace these; nothing here touches the network or starts processes in tests)
# ---------------------------------------------------------------------------

def _alive_real(url: str) -> bool:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/healthz", timeout=10) as r:  # noqa: S310 - our own URL
            return 200 <= r.status < 300
    except Exception as exc:
        log.debug("tunnel health check %s failed: %r", url, exc)
        return False


def _spawn_real(argv: list[str], logfile: str) -> int:
    flags = 0
    if os.name == "nt":  # no console window; keeps running across app restarts
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    p = subprocess.Popen(argv + ["--logfile", logfile], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags)
    return p.pid


def _kill_real(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15)
        else:
            os.kill(pid, 15)
    except Exception as exc:
        log.debug("could not stop old tunnel pid %s: %r", pid, exc)


def _which_real() -> str | None:
    found = shutil.which("cloudflared")
    if found:
        return found
    return next((p for p in _KNOWN_EXE if Path(p).is_file()), None)


_DEFAULTS: dict[str, Callable[..., Any]] = {"alive": _alive_real, "spawn": _spawn_real, "kill": _kill_real,
                                            "sleep": time.sleep, "which": _which_real}
_seams: dict[str, Callable[..., Any]] = dict(_DEFAULTS)


def set_seams(**kw: Callable[..., Any]) -> None:
    unknown = set(kw) - set(_DEFAULTS)
    if unknown:
        raise ValueError(f"unknown seams {sorted(unknown)}")
    _seams.update(kw)


def reset_seams() -> None:
    _seams.clear()
    _seams.update(_DEFAULTS)


# ---------------------------------------------------------------------------

def is_quick(url: str | None) -> bool:
    from urllib.parse import urlparse

    return (urlparse(url or "").hostname or "").lower().endswith(QUICK_SUFFIX)


def state() -> dict[str, Any]:
    v = db.kv_get(KV)
    return v if isinstance(v, dict) else {}


def _merge(**changes: Any) -> dict[str, Any]:
    return db.kv_update(KV, lambda cur: {**(cur if isinstance(cur, dict) else {}), **changes}, {})


def _logfile() -> Path:
    p = Path(config.get_settings().data_dir) / "logs" / "cloudflared.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _read_new_url(logfile: Path, offset: int) -> str | None:
    try:
        with open(logfile, encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            m = URL_RE.search(fh.read())
    except OSError as exc:
        log.debug("cannot read %s: %r", logfile, exc)
        return None
    return m.group(0) if m else None


def ensure(*, port: int = DEFAULT_PORT) -> dict[str, Any]:
    """Check the public address; restart the quick tunnel if it is dead. Returns what happened."""
    base = config.get_settings().public_base_url.rstrip("/")
    if not is_quick(base):
        return {"status": "skipped", "why": "PUBLIC_BASE_URL is not a Cloudflare quick tunnel"}
    if _seams["alive"](base):
        if state().get("misses"):
            _merge(misses=0)
        return {"status": "ok", "url": base}
    st = state()
    started = db.parse_iso(st["started_at"]) if st.get("started_at") else None
    if started and (db.utcnow() - started).total_seconds() < GRACE_SECONDS and st.get("url") == base:
        return {"status": "starting", "url": base}
    misses = int(_merge(misses=int(st.get("misses") or 0) + 1)["misses"])
    if misses < MISSES_BEFORE_RESTART:
        return {"status": "waiting", "url": base, "misses": misses}
    exe = _seams["which"]()
    if not exe:
        why = "the tunnel at " + base + " is down and cloudflared is not installed"
        db.log_event("assistant", "public address down: " + why, level="warning")
        return {"status": "failed", "why": why}
    if st.get("pid"):
        _seams["kill"](int(st["pid"]))
    logfile = _logfile()
    offset = logfile.stat().st_size if logfile.exists() else 0
    _merge(started_at=db.now_iso(), url=None, pid=None)          # bookkeeping before the work
    pid = _seams["spawn"]([exe, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{port}"], str(logfile))
    _merge(pid=pid)
    new = None
    for _ in range(WAIT_FOR_URL_SECONDS):
        new = _read_new_url(logfile, offset)
        if new:
            break
        _seams["sleep"](1)
    if not new:
        why = f"cloudflared started (pid {pid}) but printed no address within {WAIT_FOR_URL_SECONDS}s"
        db.log_event("assistant", "public address down: " + why, level="warning")
        return {"status": "failed", "why": why}
    import config_store

    config_store.update({"PUBLIC_BASE_URL": new})
    sms = None
    try:
        from assistant import twilio_doctor

        sms = twilio_doctor.follow_base_change(base)
    except Exception as exc:  # the new address is saved either way; the Twilio check will show the webhook
        log.warning("could not re-point the Twilio SMS webhook: %r", exc)
        sms = f"could not re-point the SMS webhook: {exc}"
    _merge(url=new, old=base, misses=0, whatsapp_url_stale=True, restarted_at=db.now_iso())
    db.log_event("assistant", f"public address restarted: {base} -> {new}", level="warning",
                 data={"old": base, "new": new, "sms_webhook": sms})
    return {"status": "restarted", "url": new, "old": base, "sms_webhook": sms}


def whatsapp_url_stale() -> bool:
    st = state()
    return bool(st.get("whatsapp_url_stale")) and st.get("url") == config.get_settings().public_base_url.rstrip("/")


def whatsapp_url_updated() -> None:
    """He pasted the new WhatsApp sandbox URL (or a WhatsApp reply arrived): stop reminding him."""
    if state().get("whatsapp_url_stale"):
        _merge(whatsapp_url_stale=False)
