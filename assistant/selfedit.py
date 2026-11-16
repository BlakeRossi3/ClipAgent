"""Self-editing guardrails: Cecil may change its own code; every change is tested and reversible.

One full-power turn (``runtime`` calls these around it)::

    snap = begin()            # on the local branch sterling/self (created on first use); record HEAD
    ... Claude Code edits files in the app checkout ...
    out = finish(snap)        # nothing changed -> None
                              # changed: offline test suite (python -m pytest -q -x, timeout)
                              #   pass -> git add -A + commit (trailer "Sterling-Self-Edit: yes"),
                              #           restart the app, verify /healthz after the restart
                              #   fail -> git reset --hard <recorded HEAD> + git clean of new files

* ``undo()`` (dashboard button, SMS ``UNDO``) reverts the newest self-edit commit not already reverted
  (``git revert --no-edit``), and restarts. It never touches commits that are not Cecil's.
* After a restart, :func:`verify_after_restart` (started by ``main.schedule``) checks ``/healthz``;
  an unhealthy app is reset to the commit before the change and restarted again.
* Only what the turn itself changed counts: :func:`begin` records the files that were already dirty
  (with their size and mtime), and :func:`finish` ignores them unless the turn touched them.
* If the tree was already dirty before the turn (someone edited by hand), nothing is ever reset,
  tested or committed: the turn's change is left in place and reported once until the tree is clean
  again (kv :data:`KV_DIRTY_NOTED`), never a full test run after every turn.

Limits (documented in docs/ASSISTANT.md): the suite catches what tests catch; a change that only
breaks at runtime is caught by the health check, not by meaning.
"""

from __future__ import annotations

import json
import re
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
KV_DIRTY_NOTED = "assistant:selfedit:dirty_noted"   # set once a dirty tree was reported; cleared when clean
UPSTREAM_KEY = "clipagent.upstream"           # git config: the branch the launcher merges in
GIT_ID = ["-c", "user.name=Sterling", "-c", "user.email=sterling@clipagent.local"]
MAX_SUMMARY = 600
HEALTH_RETRY_SEC = 3.0

_app_dir: Path | None = None
_active: dict[int, float] = {}        # id(Snapshot) -> monotonic start, while finish() tests/commits a change


def app_dir() -> Path:
    from assistant.claude_code import REPO_ROOT

    return _app_dir or REPO_ROOT


def set_app_dir(path: Path | None) -> None:
    """Tests: work on a temporary git repository."""
    global _app_dir
    _app_dir = Path(path) if path else None


# -- injectable runners (tests never run the real suite or restart) ---------

TestRunner = Callable[[Path, float], tuple[bool, str]]   # (app_dir, timeout) -> (passed, output tail)


#: Set by callers whose change is already committed (recover): the commit to compare failures against.
_baseline_ref: str | None = None
NO_PYTEST = "tests could not run here (pytest is not installed)"
_FAILED = re.compile(r"^FAILED (\S+?)(?: - |$)", re.M)


def _pytest(root: Path, args: list[str], timeout: float) -> tuple[int, str]:
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args], cwd=str(root),
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                       check=False, **_nowin())
    return p.returncode, (p.stdout + p.stderr).strip()


def _fails_without_change(root: Path, test_id: str, timeout: float) -> bool:
    """Does ``test_id`` also fail on the code from before the change (a problem of this machine, not of
    the change)? Checked in a temporary worktree of the baseline commit; never touches the running app."""
    base = _baseline_ref or _out("rev-parse", "HEAD")
    if not base:
        return False
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="cecil-baseline-"))
    wt = tmp / "wt"
    try:
        if git("worktree", "add", "--detach", str(wt), base, timeout=120).returncode != 0:
            return False
        code, _ = _pytest(wt, ["-x", test_id], timeout)
        return code != 0
    except Exception:
        return False
    finally:
        git("worktree", "remove", "--force", str(wt))
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def _real_tests(root: Path, timeout: float) -> tuple[bool, str]:
    """The offline suite, judged fairly: no pytest at all -> nothing to judge (kept, the /healthz check after
    the restart still guards it); a test that also fails WITHOUT the change is this machine's problem and is
    deselected (up to 5), so only failures the change caused roll it back."""
    deselect: list[str] = []
    try:
        for _ in range(6):
            code, out = _pytest(root, ["-x", *[a for t in deselect for a in ("--deselect", t)]], timeout)
            if code == 0:
                note = f" (ignored {len(deselect)} test(s) that fail on this machine anyway)" if deselect else ""
                return True, out[-1500:] + note
            if "No module named pytest" in out:
                db.log_event("assistant", NO_PYTEST, level="warning")
                return True, NO_PYTEST
            m = _FAILED.search(out)
            if not m or len(deselect) >= 5 or not _fails_without_change(root, m.group(1), timeout):
                return False, out[-1500:]
            deselect.append(m.group(1))
        return False, "too many failing tests"
    except subprocess.TimeoutExpired:
        return False, f"the test suite took longer than {int(timeout)} s"


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
    dirty: dict[str, str] = field(default_factory=dict)   # files already changed before the turn -> signature


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
    if exists and git("merge-base", "--is-ancestor", "HEAD", target).returncode != 0:
        # The running version has updates the self-edit branch lacks: switch to the branch and merge them in,
        # so the owner's updates and Cecil's own edits are combined. Never swap the old app back in.
        r = _switch_and_merge(target, br)
    else:
        r = git("checkout", target) if exists else git("checkout", "-b", target)
    if r.returncode != 0:
        raise RuntimeError(f"could not switch to {target}: {(r.stderr or r.stdout).strip()[:200]}")
    _release_published(br, target)
    if not _out("config", "--get", "user.email"):  # merges by the launcher's update need an identity
        git("config", "user.name", "Cecil")
        git("config", "user.email", "sterling@clipagent.local")
    db.log_event("assistant", f"self-edit branch {target} in use", data={"from": br})
    return target


def _backup_name() -> str:
    return f"sterling/backup-{db.utcnow():%Y%m%d-%H%M%S}"


def _switch_and_merge(target: str, src: str) -> subprocess.CompletedProcess[str]:
    """Check out ``target`` and merge ``src`` (the version that was running) into it. On a conflict the
    branch is kept as a backup (``recover()`` / Cecil can merge it by hand) and restarted from ``src``."""
    head = _out("rev-parse", "HEAD")
    r = git("checkout", target)
    if r.returncode != 0:
        return r
    m = git(*GIT_ID, "merge", "--no-edit", "-m", f"Merge updates from {src or head[:8]}", head)
    if m.returncode == 0:
        db.log_event("assistant", f"merged the latest updates into {target}", data={"from": src or head})
        return m
    conflicts = _out("diff", "--name-only", "--diff-filter=U").splitlines()
    git("merge", "--abort")
    backup = _backup_name()
    git("branch", "-f", backup, target)
    db.log_event("assistant", f"my edits on {target} conflict with the update; kept them as {backup}",
                 level="warning", data={"conflicts": conflicts[:20]})
    return git("checkout", "-B", target, head)


def save_unsaved(reason: str) -> str | None:
    """Commit every uncommitted change in the app folder (on the current branch) so nothing is ever left
    loose for an update to stash away. Returns the new commit, or None when there was nothing to save."""
    if not changed_files():
        return None
    git("add", "-A")
    r = git(*GIT_ID, "commit", "-q", "--no-verify", "-m", f"Sterling: save {reason}", "-m", TRAILER)
    if r.returncode != 0:
        log.warning("could not save unsaved edits: %s", (r.stderr or r.stdout).strip()[:200])
        return None
    sha = _out("rev-parse", "HEAD")
    db.log_event("assistant", f"saved {reason}", data={"commit": sha})
    return sha


def _release_published(br: str, target: str) -> None:
    """``br`` (the published branch) got local commits and they are now merged into ``target``: put ``br``
    back on origin so the launcher's plain update of it can never get stuck again."""
    if not br or br == target:
        return
    origin = f"origin/{br}"
    if git("rev-parse", "--verify", "--quiet", origin).returncode != 0:
        return
    if git("merge-base", "--is-ancestor", br, origin).returncode == 0:
        return                                   # no local commits on it
    if git("merge-base", "--is-ancestor", br, target).returncode == 0:
        git("branch", "-f", br, origin)


def changed_files() -> list[str]:
    lines = git("status", "--porcelain", "--untracked-files=all").stdout.splitlines()
    return sorted({ln[3:].split(" -> ")[-1].strip('"') for ln in lines if ln.strip()})


def _sig(rel: str) -> str:
    """Cheap change signature of one working-tree file (size + mtime; "-" when it is gone)."""
    try:
        st = (app_dir() / rel).stat()
    except OSError:
        return "-"
    return f"{st.st_size}:{st.st_mtime_ns}"


def turn_changes(snap: Snapshot, files_now: list[str] | None = None) -> list[str]:
    """Files the turn changed: newly dirty ones, pre-existing dirty files whose content moved, and
    pre-existing dirty files that the turn put back to clean."""
    now = changed_files() if files_now is None else files_now
    out = {f for f in now if f not in snap.dirty or _sig(f) != snap.dirty[f]}
    out |= {f for f in snap.dirty if f not in now}
    return sorted(out)


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


def in_flight() -> bool:
    """A change is being tested / committed right now (:func:`finish`); the in-browser update waits."""
    limit = get_assistant_settings().self_edit_test_timeout_sec + 900
    now = time.monotonic()
    for k, t0 in list(_active.items()):
        if now - t0 > limit:          # a turn that never finished: stop counting it
            _active.pop(k, None)
    return bool(_active)


def begin() -> Snapshot:
    """Before a full-power turn. Never raises: a broken git setup just disables the guardrail."""
    s = get_assistant_settings()
    guard = _protected_now()
    if not s.self_edit or not is_repo():
        return Snapshot("", True, "", ok=False, note="not a git checkout", protected=guard)
    try:
        if changed_files():               # leftovers (an earlier turn, a manual edit): save them, never park them
            save_unsaved("earlier unsaved edits")
        files = changed_files()
        clean = not files
        if clean:
            db.kv_delete(KV_DIRTY_NOTED)     # committed or discarded: the next dirty tree is reported again
        branch = ensure_branch() if clean else current_branch()
        return Snapshot(_out("rev-parse", "HEAD"), clean, branch, protected=guard,
                        dirty={f: _sig(f) for f in files})
    except Exception as exc:
        db.log_event("assistant", "self-edit guard unavailable", level="warning", data={"error": repr(exc)[:300]})
        return Snapshot("", True, "", ok=False, note=str(exc)[:200], protected=guard)


REFUSED_NOTE = ("I tried to change Agent/Heartbeat.md, which is yours to edit, so I put it back. I can propose the "
                "change for your approval instead.")


def finish(snap: Snapshot, *, summary: str = "") -> Outcome | None:
    """After the turn: test, then commit + restart, or roll back. None when nothing changed.
    Owner-only files (the heartbeat checklist) are restored first, whatever else happened."""
    restored = _restore_protected(snap.protected) if snap.protected else []
    _active[id(snap)] = time.monotonic()
    try:
        out = _finish(snap, summary=summary)
    finally:
        _active.pop(id(snap), None)
    if not restored:
        return out
    if out is None:
        return Outcome("refused", REFUSED_NOTE, restored)
    out.summary = f"{out.summary} {REFUSED_NOTE}"
    return out


def _finish(snap: Snapshot, *, summary: str = "") -> Outcome | None:
    if not snap.ok:
        return None
    files = turn_changes(snap)
    moved = _out("rev-parse", "HEAD") != snap.head
    if not files and not moved:
        return None
    if not snap.clean:
        # Hand edits were already there: never test, commit or reset on top of them. Say so once per set
        # of hand edits (not after every turn), and leave everything as it is.
        if db.kv_get(KV_DIRTY_NOTED):
            log.info("self-edit check skipped: the app folder already had uncommitted changes")
            return None
        db.kv_set(KV_DIRTY_NOTED, db.now_iso())
        out = Outcome("kept_dirty", f"I changed my code ({', '.join(files[:5])}), but the app folder already had "
                      "uncommitted changes, so I left everything as it is: not tested and not committed. Commit "
                      "or discard those changes and my next edit will be tested and saved.", files)
        _remember(out)
        return out
    timeout = get_assistant_settings().self_edit_test_timeout_sec
    passed, output = _tests(app_dir(), timeout)
    if not passed:
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
    tested = "the tests pass" if output != NO_PYTEST else "the tests could not run here, but the restart check guards it"
    out = Outcome("committed", f"I changed my own code ({', '.join(files[:5]) or 'see history'}); {tested}, "
                  "so it is saved and I am restarting to use it. Text UNDO to reverse it.", files, commit,
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
    """Newest first: {sha, title, at, reverted} for Cecil's own commits on this branch."""
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


def head() -> str:
    """The checkout's HEAD sha ('' when this is not a git checkout)."""
    return _out("rev-parse", "HEAD") if is_repo() else ""


def commits_since(base: str) -> list[str]:
    """Shas of Cecil's own commits (trailer) after ``base`` up to HEAD, oldest first. [] without git/base."""
    if not base or not is_repo():
        return []
    raw = git("log", "--reverse", "--format=%H%x1f%b%x1e", f"{base}..HEAD").stdout
    out = []
    for rec in raw.split("\x1e"):
        parts = rec.strip("\n").split("\x1f")
        if len(parts) == 2 and TRAILER in parts[1]:
            out.append(parts[0].strip())
    return out


def revert_commits(shas: list[str], *, label: str = "") -> dict[str, Any]:
    """Revert these self-edit commits (newest first) in one go, then restart. {ok, message, reverted}.
    Commits already reverted are skipped; anything that does not apply cleanly undoes the whole attempt."""
    if not is_repo():
        return {"ok": False, "message": "This app folder is not a git checkout, so there is nothing to undo.",
                "reverted": []}
    if changed_files():
        return {"ok": False, "message": "There are uncommitted changes in the app folder; not undoing anything.",
                "reverted": []}
    mine = {c["sha"]: c for c in self_commits(200)}
    todo = [s for s in shas if s in mine and not mine[s]["reverted"]]
    if not todo:
        return {"ok": False, "message": "Nothing left to undo for that (already undone, or not my change).",
                "reverted": []}
    start = _out("rev-parse", "HEAD")
    order = [c["sha"] for c in self_commits(200) if c["sha"] in todo]   # newest first
    for sha in order:
        r = git(*GIT_ID, "revert", "--no-edit", sha)
        if r.returncode != 0:
            git("revert", "--abort")
            git("reset", "--hard", start)
            return {"ok": False, "reverted": [],
                    "message": "Undo did not apply cleanly (a later change builds on it); nothing was changed. "
                               + (r.stderr or r.stdout).strip()[:200]}
    db.log_event("assistant", f"self-edits undone: {label[:80]}", level="warning", data={"shas": order})
    db.kv_delete(KV_PENDING)
    schedule_restart()
    return {"ok": True, "reverted": order,
            "message": f"Undone{': ' + label if label else ''} ({len(order)} change{'s' if len(order) != 1 else ''}). "
                       "Restarting."}


# -- bringing set-aside edits back ---------------------------------------------------

STASH_MARKERS = ("before-manual-update", "sterling-autosave-before-update", "cecil")


def set_aside() -> dict[str, list[dict[str, Any]]]:
    """Edits an update put aside: backup branches with commits the running version lacks, and stashes
    the launcher or a manual update made. {"branches": [{name, commits}], "stashes": [{ref, message}]}."""
    if not is_repo():
        return {"branches": [], "stashes": []}
    branches = []
    for name in _out("for-each-ref", "--format=%(refname:short)", "refs/heads/sterling/backup-*").splitlines():
        n = _out("rev-list", "--count", f"HEAD..{name}")
        if n and n != "0":
            branches.append({"name": name, "commits": int(n)})
    stashes = []
    for line in _out("stash", "list", "--format=%gd%x1f%gs").splitlines():
        ref, _, msg = line.partition("\x1f")
        if any(m in msg.lower() for m in STASH_MARKERS):
            stashes.append({"ref": ref, "message": msg})
    return {"branches": branches, "stashes": stashes}


def recover(*, restart: bool = True) -> dict[str, Any]:
    """Merge every set-aside edit back into the running version, test, keep (and restart) or undo.
    Conflicting pieces are skipped and listed so Cecil (or the owner) can merge them by hand.
    {ok, message, merged, conflicts}."""
    if not is_repo():
        return {"ok": False, "message": "This app folder is not a git checkout.", "merged": [], "conflicts": []}
    if changed_files():
        return {"ok": False, "message": "The app folder has uncommitted changes; commit or discard them first.",
                "merged": [], "conflicts": []}
    found = set_aside()
    if not found["branches"] and not found["stashes"]:
        return {"ok": True, "message": "Nothing was set aside; every edit is already in the running version.",
                "merged": [], "conflicts": []}
    ensure_branch()
    start = _out("rev-parse", "HEAD")
    merged: list[str] = []
    conflicts: list[dict[str, Any]] = []
    for b in found["branches"]:
        r = git(*GIT_ID, "merge", "--no-edit", "-m", f"Sterling: bring back edits from {b['name']}", b["name"])
        if r.returncode == 0:
            merged.append(b["name"])
        else:
            files = _out("diff", "--name-only", "--diff-filter=U").splitlines()
            git("merge", "--abort")
            conflicts.append({"what": b["name"], "files": files[:20]})
    applied: list[str] = []
    for st in reversed(found["stashes"]):            # oldest first; refs shift as stashes are dropped
        ref = st["ref"]
        r = git("stash", "apply", ref)
        if r.returncode == 0 and changed_files():
            git("add", "-A")
            c = git(*GIT_ID, "commit", "-q", "-m", f"Sterling: bring back unsaved edits ({st['message'][:60]})",
                    "-m", TRAILER)
            if c.returncode == 0:
                merged.append(st["message"])
                applied.append(st["ref"])
                continue
        files = _out("diff", "--name-only", "--diff-filter=U").splitlines()
        git("reset", "-q", "--hard", "HEAD")
        git("clean", "-fdq")
        if r.returncode != 0:
            conflicts.append({"what": f"{st['ref']} ({st['message']})", "files": files[:20]})
    if _out("rev-parse", "HEAD") == start:
        return {"ok": not conflicts, "merged": [], "conflicts": conflicts,
                "message": _recover_message([], conflicts)}
    global _baseline_ref
    _baseline_ref = start
    try:
        passed, output = _tests(app_dir(), get_assistant_settings().self_edit_test_timeout_sec)
    finally:
        _baseline_ref = None
    if not passed:
        git("reset", "-q", "--hard", start)
        return {"ok": False, "merged": [], "conflicts": conflicts,
                "message": "I merged the set-aside edits but the tests failed, so I put things back as they were. "
                           "What failed: " + _tail(output)}
    for ref in sorted(applied, key=lambda x: -int(x.strip("stash@{}") or 0)):   # newest index first
        git("stash", "drop", ref)
    db.log_event("assistant", "brought set-aside edits back", data={"merged": merged, "conflicts": conflicts})
    if restart:
        db.kv_set(KV_PENDING, {"commit": _out("rev-parse", "HEAD"), "prev": start, "at": db.now_iso()})
        _write_marker(start)
        schedule_restart()
    return {"ok": not conflicts, "merged": merged, "conflicts": conflicts,
            "message": _recover_message(merged, conflicts) + (" Restarting to use them." if restart else "")}


def _recover_message(merged: list[str], conflicts: list[dict[str, Any]]) -> str:
    parts = []
    if merged:
        parts.append(f"Brought back {len(merged)} set-aside edit(s); the tests pass.")
    for c in conflicts:
        parts.append(f"{c['what']} conflicts with the current version in {', '.join(c['files']) or 'some files'}: "
                     f"merge it by hand (git merge {c['what'].split(' ')[0]}), resolve, test, commit.")
    return " ".join(parts) or "Nothing to bring back."


def startup_recover(delay: float = 45.0) -> None:
    """At app start (main.schedule): merge anything an update set aside back in, by itself - he should never
    have to ask. Runs in the background after ``delay`` seconds; conflicts are left listed for Cecil."""
    def run() -> None:
        time.sleep(delay)
        try:
            if not is_repo() or in_flight():
                return
            found = set_aside()
            target = get_assistant_settings().self_edit_branch
            own_ahead = (current_branch() != target
                         and git("rev-parse", "--verify", "--quiet", f"refs/heads/{target}").returncode == 0
                         and _out("rev-list", "--count", f"HEAD..{target}") not in ("", "0"))
            if not found["branches"] and not found["stashes"] and not own_ahead:
                return
            if own_ahead and not found["branches"] and not found["stashes"]:
                head = _out("rev-parse", "HEAD")
                save_unsaved("unsaved edits at start-up")
                ensure_branch()                  # switch to Cecil's branch and merge the running version in
                if _out("rev-parse", "HEAD") != head:
                    db.kv_set(KV_PENDING, {"commit": _out("rev-parse", "HEAD"), "prev": head, "at": db.now_iso()})
                    _write_marker(head)
                    schedule_restart()
                return
            res = recover(restart=True)
            db.log_event("assistant", "startup: set-aside edits " + ("brought back" if res.get("merged") else "checked"),
                         level="info" if res.get("ok") else "warning", data=res)
        except Exception as exc:   # never break startup
            log.warning("startup recover failed: %s", exc)

    threading.Thread(target=run, name="selfedit-startup-recover", daemon=True).start()


def register_tools(register: Callable[..., None]) -> None:
    from assistant.tools import Tool, _obj

    register(Tool("self_edits_set_aside", "Edits of mine an update put aside (backup branches and stashes) that "
                  "the running app does not have yet.", _obj({}, []), lambda ctx, a: set_aside(),
                  owner="assistant.selfedit"), replace=True)
    register(Tool("self_edits_recover", "Merge every set-aside edit of mine back into the running app (with the "
                  "owner's latest updates), run the tests, keep and restart, or undo if they fail. Conflicts are "
                  "listed for me to resolve by hand.", _obj({}, []), lambda ctx, a: recover(),
                  owner="assistant.selfedit", side_effects=True), replace=True)


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
    out = Outcome("rolled_back", "The update broke the app after the restart, so I put the previous version back."
                  if pending.get("reason") == "update" else "My last code change broke the app after the restart, "
                  "so I put the previous version back.", [], pending.get("commit"))
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
            "pending": json.dumps(db.kv_get(KV_PENDING)) if db.kv_get(KV_PENDING) else "",
            "set_aside": set_aside() if repo else {"branches": [], "stashes": []}}
