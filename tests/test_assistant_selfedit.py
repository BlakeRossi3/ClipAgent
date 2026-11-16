"""Self-editing guardrails on a temporary git repository (never the real checkout, never real pytest)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import db
from assistant import claude_code, runtime, selfedit
from assistant import settings as asettings
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "app"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "app.py").write_text("VERSION = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "initial")
    selfedit.set_app_dir(r)
    restarts = []
    state = {"pass": True}
    selfedit.set_runners(tests=lambda root, timeout: (state["pass"], "1 failed: test_app" if not state["pass"] else "ok"),
                         restart=lambda: restarts.append(1) or True)
    monkeypatch.setattr(selfedit, "schedule_restart", lambda delay=0: restarts.append("scheduled"))
    yield r, state, restarts
    selfedit.set_runners()


def test_commit_on_pass_then_undo(repo):
    r, _state, restarts = repo
    snap = selfedit.begin()
    assert snap.ok and snap.clean and selfedit.current_branch() == "sterling/self"
    assert _git(r, "config", "--get", selfedit.UPSTREAM_KEY) == "main"
    assert selfedit.finish(snap) is None                         # nothing changed
    (r / "app.py").write_text("VERSION = 2\n")
    (r / "new.py").write_text("x = 1\n")
    out = selfedit.finish(snap, summary="bump the version")
    assert out.status == "committed" and out.restart and set(out.files) == {"app.py", "new.py"}
    body = _git(r, "log", "-1", "--format=%B")
    assert body.startswith("Sterling: bump the version") and selfedit.TRAILER in body
    assert db.kv_get(selfedit.KV_PENDING)["prev"] == snap.head and selfedit.marker_path().read_text().strip() == snap.head
    assert [c["title"] for c in selfedit.self_commits()] == ["Sterling: bump the version"]

    res = selfedit.undo()
    assert res["ok"] and (r / "app.py").read_text() == "VERSION = 1\n" and not (r / "new.py").exists()
    assert restarts == ["scheduled"] and selfedit.self_commits()[0]["reverted"]
    assert not selfedit.undo()["ok"]                              # nothing of Sterling's left to undo


def test_updates_and_cecils_edits_are_merged_not_swapped(repo):
    r, _state, _restarts = repo
    _git(r, "checkout", "-q", "-b", "sterling/self")
    (r / "cecil.py").write_text("by = 'cecil'\n")                  # Cecil's own edit on its branch
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "Sterling: cecil edit")
    _git(r, "checkout", "-q", "main")
    (r / "app.py").write_text("VERSION = 9\n")                   # an update on the published branch
    _git(r, "commit", "-qam", "update")
    snap = selfedit.begin()
    assert snap.ok and selfedit.current_branch() == "sterling/self"
    assert (r / "app.py").read_text() == "VERSION = 9\n" and (r / "cecil.py").exists()   # both kept


def test_conflicting_branch_is_backed_up_then_recovered_by_hand_list(repo):
    r, _state, _restarts = repo
    _git(r, "checkout", "-q", "-b", "sterling/self")
    (r / "app.py").write_text("VERSION = 'cecil'\n")
    _git(r, "commit", "-qam", "Sterling: cecil edit")
    _git(r, "checkout", "-q", "main")
    (r / "app.py").write_text("VERSION = 9\n")
    _git(r, "commit", "-qam", "update")
    selfedit.begin()
    assert (r / "app.py").read_text() == "VERSION = 9\n"         # the update runs
    aside = selfedit.set_aside()
    assert [b["name"].startswith("sterling/backup-") for b in aside["branches"]] == [True]
    res = selfedit.recover(restart=False)
    assert not res["ok"] and res["conflicts"] and "app.py" in res["conflicts"][0]["files"]
    assert not selfedit.changed_files()                          # nothing half-merged left behind


def test_recover_brings_back_backups_and_stashes(repo):
    r, state, restarts = repo
    _git(r, "checkout", "-q", "-b", "sterling/backup-manual-2")
    (r / "feature.py").write_text("x = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "Sterling: feature")
    _git(r, "checkout", "-q", "main")
    (r / "notes.py").write_text("y = 2\n")                       # an unsaved edit set aside by the manual update
    _git(r, "stash", "push", "--include-untracked", "-m", "before-manual-update")
    aside = selfedit.set_aside()
    assert len(aside["branches"]) == 1 and len(aside["stashes"]) == 1
    state["pass"] = False
    res = selfedit.recover(restart=False)                        # tests fail -> everything put back
    assert not res["ok"] and not (r / "feature.py").exists() and len(selfedit.set_aside()["stashes"]) == 1
    state["pass"] = True
    res = selfedit.recover()
    assert res["ok"] and (r / "feature.py").exists() and (r / "notes.py").exists()
    assert selfedit.set_aside() == {"branches": [], "stashes": []} and restarts == ["scheduled"]
    assert selfedit.recover()["message"].startswith("Nothing was set aside")


def test_rollback_on_failing_tests(repo):
    r, state, _ = repo
    snap = selfedit.begin()
    state["pass"] = False
    (r / "app.py").write_text("VERSION = broken\n")
    (r / "junk.py").write_text("oops\n")
    out = selfedit.finish(snap)
    assert out.status == "rolled_back" and "test_app" in out.summary
    assert (r / "app.py").read_text() == "VERSION = 1\n" and not (r / "junk.py").exists()
    assert _git(r, "rev-parse", "HEAD") == snap.head and selfedit.last_outcome()["status"] == "rolled_back"


def test_leftover_edits_are_saved_as_a_commit_never_left_loose(repo):
    r, state, _ = repo
    (r / "app.py").write_text("VERSION = leftover\n")         # an earlier turn's unsaved edit
    snap = selfedit.begin()
    assert snap.clean and selfedit.current_branch() == "sterling/self" and not selfedit.changed_files()
    assert "save earlier unsaved edits" in _git(r, "log", "-1", "--format=%s")
    state["pass"] = False
    (r / "other.py").write_text("y = 2\n")
    out = selfedit.finish(snap)                               # this turn fails its tests: only it is undone
    assert out.status == "rolled_back" and (r / "app.py").read_text() == "VERSION = leftover\n"


def test_published_branch_with_cecils_commit_is_released_after_merge(repo):
    r, _state, _ = repo
    bare = r.parent / "origin.git"
    _git(r.parent, "clone", "-q", "--bare", str(r), str(bare))
    _git(r, "remote", "add", "origin", str(bare))
    _git(r, "fetch", "-q", "origin")
    (r / "gta.py").write_text("widget = 'GTA 6'\n")          # Cecil's edit, left on the published branch
    snap = selfedit.begin()
    assert snap.ok and selfedit.current_branch() == "sterling/self" and (r / "gta.py").exists()
    assert _git(r, "rev-parse", "main") == _git(r, "rev-parse", "origin/main")   # launcher can update it again


def test_only_files_this_turn_changed_count(repo):
    r, _state, _ = repo
    (r / "app.py").write_text("VERSION = hand edit\n")
    snap = selfedit.begin()
    assert selfedit.turn_changes(snap) == []
    import os
    import time as _t

    _t.sleep(0.01)
    (r / "app.py").write_text("VERSION = hand edit, then the turn\n")
    os.utime(r / "app.py", ns=(_t.time_ns(), _t.time_ns() + 10_000_000))
    assert selfedit.turn_changes(snap) == ["app.py"]


def test_unhealthy_restart_rolls_back(repo, monkeypatch):
    r, _state, restarts = repo
    snap = selfedit.begin()
    (r / "app.py").write_text("VERSION = 3\n")
    selfedit.finish(snap)
    monkeypatch.setattr(selfedit, "HEALTH_RETRY_SEC", 0)
    assert selfedit.verify_after_restart(8000, wait=0, fetch=lambda url: False) == "rolled_back"
    assert (r / "app.py").read_text() == "VERSION = 1\n" and 1 in restarts
    assert db.kv_get(selfedit.KV_PENDING) is None and not selfedit.marker_path().exists()
    assert selfedit.verify_after_restart(8000, wait=0, fetch=lambda url: True) == "none"


def test_full_turn_commits_its_own_edit(repo, monkeypatch, clock):
    r, _state, restarts = repo
    monkeypatch.setenv("ASSISTANT_ENABLED", "true")
    monkeypatch.setenv("ASSISTANT_BACKEND", "claude_code")
    monkeypatch.setenv("FAKE_ASSISTANT", "false")
    asettings.reset_assistant_settings()
    monkeypatch.setattr(claude_code, "find_claude", lambda: Path("claude"))

    def runner(argv, *, input, env, cwd, timeout):
        (r / "app.py").write_text("VERSION = 9\n")      # Claude Code edits its own code
        return claude_code.Completed(0, json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                                    "result": "I made the change.", "session_id": "s"}), "")

    claude_code.set_runner(runner)
    try:
        res = runtime.run_turn("make the version 9", channel="dashboard")
    finally:
        claude_code.set_runner(None)
    assert "tests pass" in res.text and restarts == ["scheduled"]
    assert _git(r, "log", "-1", "--format=%s") == "Sterling: make the version 9"
