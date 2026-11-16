"""updater.py: in-browser update check + update on temporary git repositories (a local bare "origin",
so no network; pip is faked)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from assistant_fixtures import assistant_env  # noqa: F401
from fastapi.testclient import TestClient

import db
import server
import updater
from assistant import selfedit, tasks

pytestmark = pytest.mark.usefixtures("assistant_env")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    dev = tmp_path / "dev"                       # the publisher's clone
    _git(tmp_path, "clone", "-q", str(origin), str(dev))
    (dev / "app.py").write_text("VERSION = 1\n")
    (dev / "requirements.txt").write_text("fastapi\n")
    _git(dev, "add", "-A")
    _git(dev, "commit", "-qm", "v1")
    _git(dev, "push", "-q", "origin", "HEAD:main")
    app = tmp_path / "app"                       # the owner's running checkout
    _git(tmp_path, "clone", "-q", str(origin), str(app))
    selfedit.set_app_dir(app)
    pips = []
    updater.set_runners(pip=lambda root: (pips.append(root) or True, "installed"))
    yield app, dev, pips
    updater.set_runners()


def publish(dev: Path, name: str, text: str, msg: str = "update") -> None:
    (dev / name).write_text(text)
    _git(dev, "add", "-A")
    _git(dev, "commit", "-qm", msg)
    _git(dev, "push", "-q", "origin", "HEAD:main")


def test_check_is_cached_and_counts_new_commits(repos, clock):
    app, dev, _ = repos
    assert updater.check()["available"] is False
    publish(dev, "app.py", "VERSION = 2\n")
    assert updater.check()["available"] is False                  # cached for 10 minutes
    clock.advance(minutes=11)
    st = updater.check()
    assert st["available"] and st["behind"] == 1 and st["upstream"] == "main"
    assert updater.public(st)["label"] == "1 update available"


def test_plain_branch_fast_forwards_and_marks_health_check(repos, clock):
    app, dev, pips = repos
    before = _git(app, "rev-parse", "HEAD")
    publish(dev, "app.py", "VERSION = 2\n")
    res = updater.update_now()
    assert res["ok"] and res["updated"] and (app / "app.py").read_text() == "VERSION = 2\n"
    assert pips == []                                             # requirements unchanged
    assert db.kv_get(selfedit.KV_PENDING)["prev"] == before and db.kv_get(selfedit.KV_PENDING)["reason"] == "update"
    db.kv_delete(selfedit.KV_PENDING)
    publish(dev, "requirements.txt", "fastapi\nhttpx\n")
    res = updater.update_now()
    assert res["ok"] and pips == [app]                            # changed -> pip install
    db.kv_delete(selfedit.KV_PENDING)
    again = updater.update_now()
    assert again["ok"] and not again["updated"] and "Already up to date" in again["log"][0]


def _on_self_branch(app: Path) -> None:
    _git(app, "config", selfedit.UPSTREAM_KEY, "main")
    _git(app, "checkout", "-q", "-b", "sterling/self")
    (app / "cecil.py").write_text("by = 'cecil'\n")
    _git(app, "add", "-A")
    _git(app, "commit", "-qm", "Sterling: my edit")


def test_self_branch_merges_and_keeps_unsaved_edits(repos, clock):
    app, dev, _ = repos
    _on_self_branch(app)
    (app / "scratch.py").write_text("wip = True\n")                # an unsaved edit of Cecil's
    publish(dev, "app.py", "VERSION = 3\n")
    res = updater.update_now()
    assert res["ok"] and res["updated"] and res["backup"] is None
    assert (app / "app.py").read_text() == "VERSION = 3\n" and (app / "cecil.py").exists()
    assert (app / "scratch.py").read_text() == "wip = True\n"     # stashed and put back
    assert _git(app, "rev-parse", "--abbrev-ref", "HEAD") == "sterling/self"
    assert _git(app, "stash", "list") == ""


def test_self_branch_conflict_keeps_backup_and_takes_published(repos, clock):
    app, dev, _ = repos
    _on_self_branch(app)
    (app / "app.py").write_text("VERSION = 'cecil'\n")
    _git(app, "commit", "-qam", "Sterling: clash")
    publish(dev, "app.py", "VERSION = 4\n")
    res = updater.update_now()
    assert res["ok"] and res["updated"] and res["backup"].startswith("sterling/backup-")
    assert (app / "app.py").read_text() == "VERSION = 4\n" and not (app / "cecil.py").exists()
    assert selfedit.set_aside()["branches"][0]["name"] == res["backup"]        # recover() can bring it back
    assert any("kept on" in line for line in res["log"])


def test_busy_and_not_a_repo(repos, clock, tmp_path):
    app, dev, _ = repos
    db.kv_set(selfedit.KV_PENDING, {"commit": "x", "prev": "y"})
    res = updater.update_now()
    assert res["busy"] and not res["ok"] and "Try again in a moment" in res["log"][0]
    db.kv_delete(selfedit.KV_PENDING)
    tasks.set_runner(lambda brief, ctx: ("ok", "ok"))
    tasks._save({"id": "t1", "task": "long job", "status": "running", "started": db.now_iso(), "steps": []},
                publish=False)
    assert updater.update_now()["busy"]
    tasks._save({"id": "t1", "task": "long job", "status": "done", "started": db.now_iso(), "steps": []},
                publish=False)
    selfedit.set_app_dir(tmp_path / "nothing")
    assert "not a git checkout" in updater.update_now()["log"][0]


def test_api_and_nav_chip(repos, clock):
    app, dev, _ = repos
    publish(dev, "app.py", "VERSION = 5\n")
    c = TestClient(server.create_app())
    j = c.get("/dashboard/api/update").json()
    assert j["available"] and j["behind"] == 1
    page = c.get("/dashboard/scheduled").text
    assert "data-update-chip" in page and 'action="/setup/action/update"' in page
