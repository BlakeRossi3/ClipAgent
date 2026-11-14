"""Self-editing guardrails: Sterling may change its own code; every change is tested and reversible.

One full-power turn (``runtime`` calls these around it)::

    snap = begin()            # on the local branch sterling/self (created on first use); record HEAD
    ... Claude Code edits files in the app checkout ...
    out = finish(snap)        # nothing changed -> None
                              # changed: offline test suite (python -m pytest -q -x, timeout)
                              #   pass -> git add -A + commit (trailer "Sterling-Self-Edit: yes"),
                              #           restart the app, verify /healthz after the restart
                              #   fail -> git reset --hard <recorded HEAD> + git clean of new files

* ``undo()`` (dashboard button, SMS ``UNDO``) reverts the newest self-edit commit not already reverted
  (``git revert --no-edit``), and restarts. It never touches commits that are not Sterling's.
* After a restart, :func:`verify_after_restart` (started by ``main.schedule``) checks ``/healthz``;
  an unhealthy app is reset to the commit before the change and restarted again.
* If the tree was already dirty before the turn (someone edited by hand), nothing is ever reset:
  a failing change is left in place and reported.

Limits (documented in docs/ASSISTANT.md): the suite catches what tests catch; a change that only
breaks at runtime is caught by the health check, not by meaning.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import db
from assistant.settings import get_assistant_settings

log = logging.getLogger(__name__)

TRAILER = "Sterling-Self-Edit: yes"
KV_PENDING = "assistant:selfedit:pending"     # {"commit", "prev", "at"} until /healthz confirms
KV_LAST = "assistant:selfedit:last"           # last outcome, for the Settings page
UPSTREAM_KEY = "clipagent.upstream"           # git config: the branch the launcher merges in
GIT_ID = ["-c", "user.name=Sterling", "-c", "user.email=sterling@clipagent.local"]
MAX_SUMMARY = 600
HEALTH_RETRY_SEC = 3.0

_app_dir: Path | None = None


def app_dir() -> Path:
    from assistant.claude_code import REPO_ROOT

    return _app_dir or REPO_ROOT


def set_app_dir(path: Path | None) -> None:
    """Tests: work on a temporary git repository."""
    global _app_dir
    _app_dir = Path(path) if path else None


# -- injectable runners (tests never run the real suite or restart) ---------

TestRunner = Callable[[Path, float], tuple[bool, str]]   # (app_dir, timeout) -> (passed, output tail)


def _real_tests(root: Path, timeout: float) -> tuple[bool, str]:
    try:
        p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"], cwd=str(root),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           check=False, **_nowin())
    except subprocess.TimeoutExpired:
        return False, f"the test suite took longer than {int(timeout)} s"
    out = (p.stdout + p.stderr).strip()
    return p.returncode == 0, out[-1500:]


def _real_restart() -> bool:
    import main

    return main.request_shutdown(restart=True)


_tests: TestRunner = _real_tests
_restart: Callable[[], bool] = _real_restart


def set_runners(*, tests: TestRunner | None = None, restart: Callable[[], bool] | None = None) -> None:
    global _tests, _restart
    _tests = tests or _real_tests
    _restart = restart or _real_restart


def _nowin() -> dict[str, Any]:
    return {"creationflags": 0x08000000} if sys.platform == "win32" else {}


def git(*args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=str(app_dir()), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, check=False, **_nowin())


def _out(*args: str) -> str:
    return git(*args).stdout.strip()


# -- the turn ----------------------------------------------------------------

@dataclass
class Snapshot:
    head: str
    clean: bool
    branch: str
    ok: bool = True
    note: str = ""
    protected: dict[str, bytes | None] = field(default_factory=dict)   # owner-only files as they were


@dataclass
class Outcome:
    status: str                 # committed | rolled_back | kept_dirty | commit_failed
    summary: str
    files: list[str] = field(default_factory=list)
    commit: str | None = None
    restart: bool = False

    def owner_note(self) -> str:
        return self.summary


def is_repo() -> bool:
    return (app_dir() / ".git").exists() and git("rev-parse", "--is-inside-work-tree").returncode == 0


def current_branch() -> str:
    return _out("rev-parse", "--abbrev-ref", "HEAD")


def ensure_branch() -> str:
    """Switch the checkout to the self-edit branch (created from the current branch on first use;
    the original branch is remembered as the one updates are merged from)."""
    target = get_assistant_settings().self_edit_branch
    br = current_branch()
    if br == target:
        return target
    if br and br != "HEAD" and not _out("config", "--get", UPSTREAM_KEY):
        git("config", UPSTREAM_KEY, br)
    exists = git("rev-parse", "--verify", "--quiet", f"refs/heads/{target}").returncode == 0
    # -B would move an existing branch; switch to it only when it already holds this HEAD's history.
    r = git("checkout", target) if exists else git("checkout", "-b", target)
    if r.returncode != 0:
        raise RuntimeError(f"could not switch to {target}: {(r.stderr or r.stdout).strip()[:200]}")
    if not _out("config", "--get", "user.email"):  # merges by the launcher's update need an identity
        git("config", "user.name", "Sterling")
        git("config", "user.email", "sterling@clipagent.local")
    db.log_event("assistant", f"self-edit branch {target} in use", data={"from": br})
    return target


def changed_files() -> list[str]:
    lines = git("status", "--porcelain", "--untracked-files=all").stdout.splitlines()
    return sorted({ln[3:].split(" -> ")[-1].strip('"') for ln in lines if ln.strip()})


def _protected_now() -> dict[str, bytes | None]:
    """Owner-only files (the heartbeat checklist + its template): their bytes, None = absent."""
    out: dict[str, bytes | None] = {}
    try:
        from assistant import heartbeat

        for p in heartbeat.protected_paths():
            out[str(p)] = p.read_bytes() if p.is_file() else None
    except Exception as exc:
        log.warning("protected-file snapshot failed: %s", exc)
    return out


def _restore_protected(before: dict[str, bytes | None]) -> list[str]:
    """Put back any owner-only file the turn changed. Returns the names restored."""
    restored = []
    for path, now in _protected_now().items():
        if path not in before or before[path] == now:
            continue
        p = Path(path)
        if before[path] is None:
            p.unlink(missing_ok=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(before[path])  # type: ignore[arg-type]
        restored.append(p.name)
    if restored:
        db.log_event("assistant", "refused a change to the owner's heartbeat checklist", level="warning",
                     data={"files": restored})
    return restored


def begin() -> Snapshot:
    """Before a full-power turn. Never raises: a broken git setup just disables the guardrail."""
    s = get_assistant_settings()
    guard = _protected_now()
    if not s.self_edit or not is_repo():
        return Snapshot("", True, "", ok=False, note="not a git checkout", protected=guard)
    try:
        clean = not changed_files()
        branch = ensure_branch() if clean else current_branch()
        return Snapshot(_out("rev-parse", "HEAD"), clean, branch, protected=guard)
    except Exception as exc:
        db.log_event("assistant", "self-edit guard unavailable", level="warning", data={"error": repr(exc)[:300]})
        return Snapshot("", True, "", ok=False, note=str(exc)[:200], protected=guard)


REFUSED_NOTE = ("I tried to change Agent/Heartbeat.md, which is yours to edit, so I put it back. I can propose the "
                "change for your approval instead.")


def finish(snap: Snapshot, *, summary: str = "") -> Outcome | None:
    """After the turn: test, then commit + restart, or roll back. None when nothing changed.
    Owner-only files (the heartbeat checklist) are restored first, whatever else happened."""
    restored = _restore_protected(snap.protected) if snap.protected else []
    out = _finish(snap, summary=summary)
    if not restored:
        return out
    if out is None:
        return Outcome("refused", REFUSED_NOTE, restored)
    out.summary = f"{out.summary} {REFUSED_NOTE}"
    return out


def _finish(snap: Snapshot, *, summary: str = "") -> Outcome | None:
    if not snap.ok:
        return None
    files = changed_files()
    moved = _out("rev-parse", "HEAD") != snap.head
    if not files and not moved:
        return None
    timeout = get_assistant_settings().self_edit_test_timeout_sec
    passed, output = _tests(app_dir(), timeout)
    if not passed:
        if not snap.clean:
            out = Outcome("kept_dirty", "I changed my code but the tests fail, and there were already "
                          "uncommitted changes, so I left everything as it is. Failing: " + _tail(output), files)
        else:
            git("reset", "--hard", snap.head)
            git("clean", "-fd")
            out = Outcome("rolled_back", "I tried to change my own code but the tests failed, so I undid it. "
                          "What failed: " + _tail(output), files)
        _remember(out)
        return out
    if moved and not files:
        # Claude committed by itself despite the instruction: keep it, tested.
        commit = _out("rev-parse", "HEAD")
    else:
        git("add", "-A")
        title = " ".join((summary or "self-edit").split())[:72] or "self-edit"
        r = git(*GIT_ID, "commit", "-q", "-m", f"Sterling: {title}", "-m",
                "Files: " + ", ".join(files[:20]) + "\n\n" + TRAILER)
        if r.returncode != 0:
            out = Outcome("commit_failed", "My code change passed the tests but could not be committed: "
                          + (r.stderr or r.stdout).strip()[:200], files)
            _remember(out)
            return out
        commit = _out("rev-parse", "HEAD")
    db.kv_set(KV_PENDING, {"commit": commit, "prev": snap.head, "at": db.now_iso()})
    _write_marker(snap.head)
    out = Outcome("committed", f"I changed my own code ({', '.join(files[:5]) or 'see history'}); the tests "
                  "pass, so it is saved and I am restarting to use it. Text UNDO to reverse it.", files, commit,
                  restart=True)
    _remember(out)
    return out


def marker_path() -> Path:
    """Plain-text copy of the pre-change commit, for the Windows launcher: if the app does not come
    back up after a self-edit, ClipAgent.ps1 resets to this commit and starts again."""
    import config

    return config.get_settings().data_dir / "assistant" / "selfedit_pending.txt"


def _write_marker(sha: str) -> None:
    try:
        marker_path().parent.mkdir(parents=True, exist_ok=True)
        marker_path().write_text(sha + "\n", encoding="ascii")
    except OSError as exc:
        log.info("self-edit marker not written: %s", exc)


def _clear_marker() -> None:
    try:
        marker_path().unlink(missing_ok=True)
    except OSError:
        pass


def _tail(output: str) -> str:
    lines = [ln for ln in (output or "").splitlines() if ln.strip()]
    return " | ".join(lines[-4:])[:400] or "no output"


def _remember(out: Outcome) -> None:
    db.kv_set(KV_LAST, {"status": out.status, "summary": out.summary[:MAX_SUMMARY], "files": out.files[:30],
                        "commit": out.commit, "at": db.now_iso()})
    level = "info" if out.status == "committed" else "warning"
    db.log_event("assistant", f"self-edit {out.status}", level=level,
                 data={"files": out.files[:30], "commit": out.commit})


def schedule_restart(delay: float = 4.0) -> None:
    """Restart shortly after the reply has been stored (never inside the request)."""
    def go() -> None:
        time.sleep(delay)
        if not _restart():
            db.log_event("assistant", "self-edit saved; restart ClipAgent to use it", level="warning")

    threading.Thread(target=go, name="selfedit-restart", daemon=True).start()


# -- history + undo ------------------------------------------------------------

def self_commits(limit: int = 10) -> list[dict[str, Any]]:
    """Newest first: {sha, title, at, reverted} for Sterling's own commits on this branch."""
    if not is_repo():
        return []
    raw = git("log", "-n", "200", "--format=%H%x1f%cI%x1f%s%x1f%b%x1e").stdout
    reverted: set[str] = set()
    out = []
    for rec in raw.split("\x1e"):
        parts = rec.strip("\n").split("\x1f")
        if len(parts) < 4:
            continue
        sha, at, title, body = parts
        for word in body.split():
            w = word.strip(".")
            if len(w) == 40 and "This reverts commit" in body:
                reverted.add(w)
        if TRAILER in body:
            out.append({"sha": sha, "title": title, "at": at, "reverted": sha in reverted})
    return out[:limit]


def undo() -> dict[str, Any]:
    """Revert the newest self-edit not yet reverted, then restart. {ok, message, sha}."""
    if not is_repo():
        return {"ok": False, "message": "This app folder is not a git checkout, so there is nothing to undo."}
    if changed_files():
        return {"ok": False, "message": "There are uncommitted changes in the app folder; not undoing anything."}
    target = next((c for c in self_commits(50) if not c["reverted"]), None)
    if target is None:
        return {"ok": False, "message": "There is no change of mine to undo."}
    r = git(*GIT_ID, "revert", "--no-edit", target["sha"])
    if r.returncode != 0:
        git("revert", "--abort")
        return {"ok": False, "message": "Undo did not apply cleanly; nothing was changed. "
                                        + (r.stderr or r.stdout).strip()[:200]}
    db.log_event("assistant", "self-edit undone", level="warning", data={"sha": target["sha"], "title": target["title"]})
    db.kv_delete(KV_PENDING)
    schedule_restart()
    return {"ok": True, "sha": target["sha"],
            "message": f"Undone: {target['title'].removeprefix('Sterling: ')}. Restarting."}


# -- after a restart -------------------------------------------------------------

def verify_after_restart(port: int, *, wait: float = 8.0, fetch: Callable[[str], bool] | None = None) -> str:
    """Check /healthz once the restarted app is up; roll back an unhealthy self-edit.
    Returns none | healthy | rolled_back."""
    pending = db.kv_get(KV_PENDING)
    if not isinstance(pending, dict) or not pending.get("prev"):
        return "none"
    time.sleep(wait)
    fetch = fetch or _healthz
    ok = any(fetch(f"http://127.0.0.1:{port}/healthz") or time.sleep(HEALTH_RETRY_SEC) for _ in range(5))
    db.kv_delete(KV_PENDING)
    _clear_marker()
    if ok:
        db.log_event("assistant", "self-edit verified after restart", data={"commit": pending.get("commit")})
        return "healthy"
    git("reset", "--hard", str(pending["prev"]))
    out = Outcome("rolled_back", "My last code change broke the app after the restart, so I put the previous "
                  "version back.", [], pending.get("commit"))
    _remember(out)
    try:
        from assistant import tools

        tools.send_to_owner(out.summary, kind="self_edit", agent="main")
    except Exception as exc:
        log.info("rollback text not sent: %s", exc)
    _restart()
    return "rolled_back"


def _healthz(url: str) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 - loopback only
            return r.status == 200 and b'"status"' in r.read(2000)
    except Exception:
        return False


def last_outcome() -> dict[str, Any] | None:
    v = db.kv_get(KV_LAST)
    return v if isinstance(v, dict) else None


def powers_status() -> dict[str, Any]:
    """For the Settings page: branch, recent self-edits, last outcome."""
    s = get_assistant_settings()
    repo = is_repo()
    return {"enabled": s.self_edit, "repo": repo, "branch": current_branch() if repo else "",
            "target_branch": s.self_edit_branch, "upstream": _out("config", "--get", UPSTREAM_KEY) if repo else "",
            "commits": self_commits(5) if repo else [], "last": last_outcome(),
            "pending": json.dumps(db.kv_get(KV_PENDING)) if db.kv_get(KV_PENDING) else ""}
