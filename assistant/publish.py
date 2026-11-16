"""Publish Cecil's own branch to GitHub so the build chat can merge it - nobody pushes by hand.

After every saved change (``selfedit`` commit, a recovery, a start-up merge) and every 30 minutes (job
``assistant_publish``) the app pushes ``sterling/self`` to ``origin`` as ``ASSISTANT_PUSH_BRANCH``
(default ``cecil/self``). This is plain code, not the model: Cecil's Claude Code keeps ``git push`` denied.

Safety, in order:
1. Local secret scan of everything that would be published (diff against what GitHub already has): the
   values of every secret key in ``config.env`` (``admin.is_secret_key``) and well-known token shapes
   (Twilio SIDs, Anthropic / GitHub / Slack keys, private keys). A hit -> nothing is pushed, a warning
   event + status say why (the file and the kind, never the value).
2. GitHub push protection (server side) is the backstop; its refusal is reported the same way.
The mirror branch belongs to Cecil alone, so it is force-pushed (his history can be rewritten by an
update conflict). ``GIT_TERMINAL_PROMPT=0``: a missing GitHub sign-in fails fast instead of hanging.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from typing import Any, Callable

import db
from assistant.settings import get_assistant_settings

KV_STATUS = "publish:status"
_lock = threading.Lock()
PATTERNS: dict[str, re.Pattern[str]] = {
    "Twilio account ID": re.compile(r"\bAC[0-9a-fA-F]{32}\b"),
    "Twilio API key": re.compile(r"\bSK[0-9a-fA-F]{32}\b"),
    "Anthropic key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "GitHub token": re.compile(r"\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}

_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None


def set_runner(fn: Callable[..., subprocess.CompletedProcess[str]] | None) -> None:
    """Tests: replace the ``git push`` call (``fn(args, env) -> CompletedProcess``)."""
    global _runner
    _runner = fn


def _secret_values() -> list[str]:
    try:
        import config_store
        from assistant import admin

        vals = config_store.read_values()
    except Exception:
        return []
    return [v.strip() for k, v in vals.items() if admin.is_secret_key(k) and len((v or "").strip()) >= 8]


def scan(text: str) -> list[str]:
    """Kinds of secret found in ``text`` (empty = clean). Never returns the secret itself."""
    found = [kind for kind, rx in PATTERNS.items() if rx.search(text)]
    if any(v in text for v in _secret_values()):
        found.append("a value from config.env")
    return found


def _outgoing_diff(remote_ref: str, base_ref: str) -> str:
    from assistant import selfedit

    have_remote = selfedit.git("rev-parse", "--verify", "--quiet", remote_ref).returncode == 0
    base = remote_ref if have_remote else base_ref
    if selfedit.git("rev-parse", "--verify", "--quiet", base).returncode != 0:
        base = ""
    args = ["log", "-p", "--no-color", "--format=", f"{base}..HEAD"] if base else ["log", "-p", "--no-color",
                                                                                  "--format=", "-n", "200", "HEAD"]
    return selfedit.git(*args, timeout=180).stdout


def status() -> dict[str, Any]:
    v = db.kv_get(KV_STATUS)
    return v if isinstance(v, dict) else {}


def _save(**kw: Any) -> dict[str, Any]:
    st = {**status(), **kw, "at": db.now_iso()}
    db.kv_set(KV_STATUS, st)
    return st


def push_now(reason: str = "", *, force_run: bool = False) -> dict[str, Any]:
    """Push Cecil's branch if it moved since the last push. Returns {ok, pushed, skipped|error, sha}."""
    from assistant import selfedit

    s = get_assistant_settings()
    if not force_run and (not s.auto_push or s.fake):
        return {"ok": True, "pushed": False, "skipped": "auto-push off"}
    if not selfedit.is_repo():
        return {"ok": True, "pushed": False, "skipped": "not a git checkout"}
    if not _lock.acquire(blocking=False):
        return {"ok": True, "pushed": False, "skipped": "a push is already running"}
    try:
        branch = s.self_edit_branch
        if selfedit.git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode != 0:
            return {"ok": True, "pushed": False, "skipped": f"no {branch} yet"}
        sha = selfedit._out("rev-parse", f"refs/heads/{branch}")
        if status().get("sha") == sha and status().get("ok"):
            return {"ok": True, "pushed": False, "skipped": "already published", "sha": sha}
        selfedit.git("fetch", "-q", "origin", timeout=120)
        upstream = selfedit._out("config", "--get", selfedit.UPSTREAM_KEY) or "main"
        found = scan(_outgoing_diff(f"origin/{s.push_branch}", f"origin/{upstream}"))
        if found:
            msg = "Not published: it would put " + ", ".join(sorted(set(found))) + " on GitHub."
            db.log_event("assistant", msg, level="warning", data={"sha": sha, "reason": reason})
            return {**_save(ok=False, sha=sha, error=msg, reason=reason), "pushed": False}
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
        args = ["git", "push", "--force", "origin", f"refs/heads/{branch}:refs/heads/{s.push_branch}"]
        run = _runner or (lambda a, e: subprocess.run(a, cwd=str(selfedit.app_dir()), capture_output=True, text=True,
                                                       encoding="utf-8", errors="replace", timeout=180, env=e,
                                                       check=False, **selfedit._nowin()))
        r = run(args, env)
        out = (r.stderr or "") + (r.stdout or "")
        if r.returncode != 0:
            why = ("GitHub's push protection found a secret" if "GH013" in out or "secret" in out.lower()
                   else "GitHub sign-in is missing or expired" if re.search(r"auth|credential|403|denied", out, re.I)
                   else "the push failed")
            msg = f"Not published: {why}. {out.strip()[-300:]}"
            db.log_event("assistant", msg[:400], level="warning", data={"sha": sha, "reason": reason})
            return {**_save(ok=False, sha=sha, error=msg[:600], reason=reason), "pushed": False}
        db.log_event("assistant", f"published {branch} to GitHub as {s.push_branch}", data={"sha": sha, "reason": reason})
        return {**_save(ok=True, sha=sha, error="", reason=reason, branch=s.push_branch), "pushed": True}
    except Exception as exc:   # never break a turn or a job
        return {**_save(ok=False, error=f"{type(exc).__name__}: {exc}"[:300], reason=reason), "pushed": False}
    finally:
        _lock.release()


def push_async(reason: str = "") -> None:
    if not get_assistant_settings().auto_push or get_assistant_settings().fake:
        return
    threading.Thread(target=push_now, args=(reason,), name="cecil-publish", daemon=True).start()
