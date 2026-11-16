"""Update the app from the browser: "Update available" indicator + one-click "Update now".

:func:`check` (at most every :data:`CHECK_EVERY`, cached in kv :data:`KV_STATUS`; the nav asks for it on
page load via ``GET /dashboard/api/update``) runs ``git fetch origin`` and counts the commits the
published branch has that the running version lacks.

:func:`update_now` does what the Windows launcher (``windows/ClipAgent.ps1`` Update-App) does:

1. refuses while a self-edit or a background task is mid-flight ("busy, try again in a moment");
2. ``git fetch origin``;
3. on the assistant's own branch (``sterling/self``, published branch in ``git config clipagent.upstream``):
   uncommitted edits are stashed (``sterling-autosave-before-update``), ``origin/<upstream>`` is merged in
   keeping both sides; the stash is re-applied (a conflicting stash stays in the stash list). On a merge
   conflict: abort, keep the assistant's work on ``sterling/backup-<stamp>`` and take the published
   version (``selfedit.recover()`` / tool ``self_edits_recover`` brings it back later);
   on any other branch: ``git merge --ff-only origin/<branch>``;
4. ``pip install -r requirements.txt`` only when it changed;
5. records the previous commit for :func:`assistant.selfedit.verify_after_restart` (``/healthz`` after the
   restart; an unhealthy app is reset to the previous commit) - the caller then restarts the app.

Every command is an argv list (never a shell). Tests use a temporary git repository
(``assistant.selfedit.set_app_dir``) and :func:`set_runners` (no pip, no network).
"""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
import sys
import threading
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import db

log = logging.getLogger(__name__)

KV_STATUS = "app:update:status"
CHECK_EVERY = timedelta(minutes=10)
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
STASH_MSG = "sterling-autosave-before-update"
GIT_ID = ["-c", "user.name=Sterling", "-c", "user.email=sterling@clipagent.local"]
BUSY_TEXT = "Busy right now (Cecil is in the middle of something). Try again in a moment."

_lock = threading.Lock()
Pip = Callable[[Path], tuple[bool, str]]   # (app_dir) -> (ok, output tail)


def _real_pip(root: Path) -> tuple[bool, str]:
    p = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-r",
                        str(root / "requirements.txt")], cwd=str(root), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=1800, check=False)
    return p.returncode == 0, (p.stdout + p.stderr).strip()[-1500:]


_pip: Pip = _real_pip
_fetch_override: Callable[[], tuple[bool, str]] | None = None


def set_runners(*, pip: Pip | None = None, fetch: Callable[[], tuple[bool, str]] | None = None) -> None:
    """Tests: replace pip (and optionally the fetch step) with fakes. No arguments = the real ones."""
    global _pip, _fetch_override
    _pip = pip or _real_pip
    _fetch_override = fetch


def root() -> Path:
    from assistant import selfedit

    return selfedit.app_dir()


def git(*args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    from assistant import selfedit

    return selfedit.git(*args, timeout=timeout)


def _out(*args: str) -> str:
    return git(*args).stdout.strip()


def _ok_branch(name: str) -> bool:
    return bool(name) and name != "HEAD" and bool(BRANCH_RE.match(name)) and ".." not in name \
        and not name.startswith("-")


def _req_digest() -> str:
    try:
        return hashlib.sha256((root() / "requirements.txt").read_bytes()).hexdigest()
    except OSError:
        return ""


def _fetch() -> tuple[bool, str]:
    if _fetch_override is not None:
        return _fetch_override()
    try:
        r = git("fetch", "--quiet", "origin", timeout=120)
    except subprocess.TimeoutExpired:
        return False, "git fetch took too long (offline?)"
    return r.returncode == 0, (r.stdout + r.stderr).strip()[-500:]


def _target() -> dict[str, Any]:
    """{branch, upstream, self_branch} or {error}. ``upstream`` = the published branch to take updates from."""
    from assistant import selfedit
    from assistant.settings import get_assistant_settings

    if not selfedit.is_repo():
        return {"error": "This app folder is not a git checkout, so it cannot update itself."}
    branch = _out("rev-parse", "--abbrev-ref", "HEAD")
    if not _ok_branch(branch):
        return {"error": "This app folder is not on a branch, so it cannot update itself."}
    self_branch = branch == get_assistant_settings().self_edit_branch
    upstream = branch
    if self_branch:
        up = _out("config", "--get", selfedit.UPSTREAM_KEY)
        upstream = up if _ok_branch(up) else ""
        if not upstream:
            return {"error": "The assistant's branch has no published branch recorded (git config clipagent.upstream)."}
    return {"branch": branch, "upstream": upstream, "self_branch": self_branch}


def _behind(upstream: str) -> tuple[int, str]:
    remote = _out("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{upstream}")
    if not remote:
        return 0, ""
    n = _out("rev-list", "--count", f"HEAD..{remote}")
    return (int(n) if n.isdigit() else 0), remote


# ---------------------------------------------------------------------------
# checking
# ---------------------------------------------------------------------------

def cached() -> dict[str, Any] | None:
    v = db.kv_get(KV_STATUS)
    return v if isinstance(v, dict) else None


def check(*, force: bool = False) -> dict[str, Any]:
    """{available, behind, branch, upstream, head, remote, checked_at, error} - fetches at most every 10 min."""
    old = cached()
    if not force and old and old.get("checked_at") and \
            db.utcnow() - db.parse_iso(old["checked_at"]) < CHECK_EVERY and old.get("head") == _head_or_blank():
        return old
    if not _lock.acquire(timeout=0.1):
        return old or {"available": False, "behind": 0, "checking": True}
    try:
        st: dict[str, Any] = {"available": False, "behind": 0, "checked_at": db.now_iso(), "error": ""}
        t = _target()
        if "error" in t:
            st["error"] = t["error"]
        else:
            ok, msg = _fetch()
            st.update(branch=t["branch"], upstream=t["upstream"], head=_out("rev-parse", "HEAD"))
            if not ok:
                st["error"] = "Could not reach the update server (offline?)."
                log.info("update check fetch failed: %s", msg)
            behind, remote = _behind(t["upstream"])
            st.update(behind=behind, remote=remote[:12], available=behind > 0)
        db.kv_set(KV_STATUS, st)
        return st
    finally:
        _lock.release()


def _head_or_blank() -> str:
    try:
        from assistant import selfedit

        return _out("rev-parse", "HEAD") if selfedit.is_repo() else ""
    except Exception:
        return ""


def public(st: dict[str, Any] | None) -> dict[str, Any]:
    st = st or {}
    n = int(st.get("behind") or 0)
    return {"available": bool(st.get("available")), "behind": n, "checked_at": st.get("checked_at"),
            "branch": st.get("branch") or "", "upstream": st.get("upstream") or "",
            "head": str(st.get("head") or "")[:7], "error": st.get("error") or "",
            "label": (f"{n} update{'s' if n != 1 else ''} available" if n else "Up to date")}


# ---------------------------------------------------------------------------
# updating
# ---------------------------------------------------------------------------

def busy() -> str:
    """Why an update must wait now ('' = free): a self-edit or a background task mid-flight."""
    from assistant import selfedit, tasks

    if selfedit.in_flight():
        return "a code change of Cecil's is being tested"
    if db.kv_get(selfedit.KV_PENDING):
        return "the last change is still being checked after its restart"
    running = tasks.running_tasks()
    if running:
        return f"a background task is running ({running[0].get('task', '')[:60]})"
    try:
        from assistant import brain

        if brain.running() and brain.get_brain().owner() is not None:
            return "Cecil is answering you right now"
    except Exception:
        pass
    return ""


def update_now() -> dict[str, Any]:
    """Fetch and merge the published branch (see the module docstring). Never restarts by itself.

    {ok, updated, busy, before, after, backup, log}."""
    out: dict[str, Any] = {"ok": False, "updated": False, "busy": False, "before": "", "after": "", "backup": None,
                           "log": []}
    why = busy()
    if why:
        out.update(busy=True, log=[f"{BUSY_TEXT} ({why})"])
        return out
    if not _lock.acquire(timeout=1):
        out.update(busy=True, log=["An update or update check is already running. Try again in a moment."])
        return out
    try:
        return _update(out)
    except (OSError, subprocess.SubprocessError) as exc:
        out["log"].append(f"Could not run git/pip: {exc}")
        return out
    finally:
        _lock.release()


def _update(out: dict[str, Any]) -> dict[str, Any]:
    from assistant import selfedit

    t = _target()
    if "error" in t:
        out["log"].append(t["error"])
        return out
    up = t["upstream"]
    ok, msg = _fetch()
    if not ok:
        out["log"].append("Could not reach the update server (offline?). Nothing was changed. " + msg)
        return out
    before = _out("rev-parse", "HEAD")
    out["before"] = before[:7]
    behind, remote = _behind(up)
    if not behind:
        out.update(ok=True, after=before[:7])
        out["log"].append(f"Already up to date with origin/{up}.")
        _store_after_update(before)
        return out
    req_before = _req_digest()
    if t["self_branch"]:
        stashed = False
        if selfedit.changed_files():
            r = git(*GIT_ID, "stash", "push", "--include-untracked", "-m", STASH_MSG)
            stashed = r.returncode == 0
            out["log"].append("Put Cecil's unsaved edits aside first (git stash).")
        m = git(*GIT_ID, "merge", "--no-edit", "-m", f"Merge updates from origin/{up}", remote, timeout=300)
        if m.returncode == 0:
            out["log"].append(f"Merged {behind} update(s) from origin/{up}; Cecil's own changes are kept.")
            if stashed:
                p = git("stash", "pop")
                if p.returncode == 0:
                    out["log"].append("Put the unsaved edits back.")
                else:   # they clash with the update: keep them in the stash for selfedit.recover()
                    git("reset", "-q", "--hard", "HEAD")
                    git("clean", "-fdq")
                    out["log"].append("The unsaved edits clash with the update, so they stay set aside "
                                      "(Settings > The brain, or ask Cecil to bring them back).")
        else:
            conflicts = _out("diff", "--name-only", "--diff-filter=U").splitlines()
            git("merge", "--abort")
            backup = f"sterling/backup-{db.utcnow():%Y%m%d-%H%M%S}"
            git("branch", "-f", backup, "HEAD")
            r = git("reset", "-q", "--hard", remote)
            if r.returncode != 0:
                out["log"].append(f"Could not move to origin/{up}: {(r.stderr or r.stdout).strip()[:200]}")
                return out
            out["backup"] = backup
            out["log"].append(f"The update clashed with Cecil's own edits ({', '.join(conflicts[:5]) or 'some files'}), "
                              f"so his work is kept on {backup} and the published version is in use. Ask Cecil to "
                              "bring his edits back when convenient.")
            db.log_event("setup", f"update conflicted; Cecil's edits kept on {backup}", level="warning",
                         data={"conflicts": conflicts[:20]})
    else:
        m = git("merge", "--ff-only", remote, timeout=300)
        if m.returncode != 0:
            out["log"].append((m.stdout + m.stderr).strip()[-800:])
            out["log"].append("The update could not be applied automatically (local changes?). Nothing was changed.")
            return out
        out["log"].append(f"Fast-forwarded {behind} update(s) from origin/{up}.")
    after = _out("rev-parse", "HEAD")
    out.update(after=after[:7], updated=after != before)
    if out["updated"] and _req_digest() != req_before:
        ok, tail = _pip(root())
        out["log"].append(tail)
        if not ok:
            out["log"].append("Installing the new requirements failed; restart once it is fixed.")
            return out
        out["log"].append("Installed the new requirements.")
    if out["updated"]:   # after the restart: /healthz, else back to `before` (selfedit.verify_after_restart)
        db.kv_set(selfedit.KV_PENDING, {"commit": after, "prev": before, "at": db.now_iso(), "reason": "update"})
        selfedit._write_marker(before)
    out["ok"] = True
    _store_after_update(after)
    db.log_event("setup", "app updated" if out["updated"] else "update checked",
                 data={"before": before[:12], "after": after[:12], "backup": out["backup"]})
    return out


def _store_after_update(head: str) -> None:
    db.kv_set(KV_STATUS, {"available": False, "behind": 0, "checked_at": db.now_iso(), "head": head, "error": ""})
