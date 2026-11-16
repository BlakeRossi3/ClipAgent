"""Cecil's branch is published to GitHub by code, after a local secret scan; refusals are reported."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from assistant import publish, selfedit
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def app(tmp_path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    a = tmp_path / "app"
    _git(tmp_path, "clone", "-q", str(origin), str(a))
    (a / "app.py").write_text("v = 1\n")
    _git(a, "add", "-A")
    _git(a, "commit", "-qm", "v1")
    _git(a, "push", "-q", "origin", "HEAD:main")
    _git(a, "config", selfedit.UPSTREAM_KEY, "main")
    _git(a, "checkout", "-q", "-b", "sterling/self")
    selfedit.set_app_dir(a)
    yield a, origin
    publish.set_runner(None)


def test_pushes_cecils_branch_once_per_change(app):
    a, origin = app
    (a / "gta.py").write_text("tile = 'GTA VI'\n")
    _git(a, "add", "-A")
    _git(a, "commit", "-qm", "Sterling: tile")
    res = publish.push_now("test", force_run=True)
    assert res["pushed"] and _git(origin, "rev-parse", "cecil/self") == _git(a, "rev-parse", "HEAD")
    assert publish.push_now("again", force_run=True)["skipped"] == "already published"


def test_secret_in_outgoing_changes_blocks_the_push(app):
    a, origin = app
    (a / "t.py").write_text("SID = 'AC" + "0123456789abcdef" * 2 + "'\n")
    _git(a, "add", "-A")
    _git(a, "commit", "-qm", "Sterling: oops")
    res = publish.push_now("test", force_run=True)
    assert not res["pushed"] and "Twilio account ID" in res["error"] and "0123456789abcdef" not in res["error"]
    assert subprocess.run(["git", "rev-parse", "--verify", "-q", "cecil/self"], cwd=origin).returncode != 0


def test_github_refusal_is_reported(app, monkeypatch):
    a, _ = app
    (a / "x.py").write_text("x = 1\n")
    _git(a, "add", "-A")
    _git(a, "commit", "-qm", "Sterling: x")
    publish.set_runner(lambda args, env: subprocess.CompletedProcess(args, 1, "", "remote: GH013: Repository rule violations"))
    res = publish.push_now("test", force_run=True)
    assert not res["pushed"] and "push protection" in res["error"] and publish.status()["ok"] is False


def test_off_in_fake_mode_and_never_from_tests():
    assert publish.push_now("x")["skipped"] == "auto-push off"
