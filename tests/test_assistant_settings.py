"""assistant.settings: loading, defaults, caching, and the git-ignore guard for the vault."""

from __future__ import annotations

import shutil
import subprocess

import pytest

import config
from assistant import settings as asettings
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")


def test_defaults(tmp_path):
    s = asettings.load_assistant_settings({"CLIPAGENT_CONFIG_FILE": str(tmp_path / "none.env"),
                                           "CLIPAGENT_DATA_DIR": str(tmp_path / "d")})
    assert s.name == "Sterling" and s.owner_honorific == "sir"
    assert s.model == "claude-fable-5-2" and s.subagent_model == "claude-opus-5-5"
    assert s.daily_budget_usd == 3.0 and s.max_turn_usd == 0.5 and s.max_sms_per_day == 8
    assert s.enabled is False and s.fake is False and s.web_search is False
    assert s.vault_dir == tmp_path / "d" / "vault"


def test_env_and_config_file(tmp_path):
    cfg = tmp_path / "c.env"
    cfg.write_text("ASSISTANT_NAME=Jarvis\nASSISTANT_DAILY_BUDGET_USD=1.25\nFAKE_LLM=true\n")
    s = asettings.load_assistant_settings({"CLIPAGENT_CONFIG_FILE": str(cfg), "OWNER_HONORIFIC": "boss",
                                           "ASSISTANT_ENABLED": "yes", "VAULT_DIR": "~/notes"})
    assert s.name == "Jarvis" and s.owner_honorific == "boss" and s.enabled
    assert s.daily_budget_usd == 1.25
    assert s.fake is True  # follows FAKE_LLM
    assert s.vault_dir.is_absolute() and s.vault_dir.name == "notes"
    s2 = asettings.load_assistant_settings({"CLIPAGENT_CONFIG_FILE": str(cfg), "FAKE_ASSISTANT": "false"})
    assert s2.fake is False


def test_cached_until_config_reset(monkeypatch):
    a = asettings.get_assistant_settings()
    assert asettings.get_assistant_settings() is a
    assert a.fake is True  # conftest sets FAKE_LLM=true
    monkeypatch.setenv("ASSISTANT_NAME", "Alfred")
    assert asettings.get_assistant_settings().name == a.name
    config.reset_settings()
    assert asettings.get_assistant_settings().name == "Alfred"


def test_override():
    custom = asettings.get_assistant_settings().replace(name="Hudson")
    asettings.set_assistant_settings(custom)
    assert asettings.get_assistant_settings().name == "Hudson"
    asettings.set_assistant_settings(None)
    assert asettings.get_assistant_settings().name == "Sterling"


def test_default_vault_path_is_gitignored(tmp_path):
    """The owner's vault must never be committable: the repo is public."""
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    root = config.REPO_ROOT
    if subprocess.run(["git", "-C", str(root), "rev-parse"], capture_output=True).returncode != 0:
        pytest.skip("not a git checkout")
    s = asettings.load_assistant_settings({"CLIPAGENT_CONFIG_FILE": str(tmp_path / "none.env")})
    default_vault = s.vault_dir
    assert default_vault.is_relative_to(root), default_vault
    for rel in ("Home.md", "Profile/About.md", "Journal/2026/2026-11-13.md"):
        probe = (default_vault / rel).relative_to(root)
        res = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", "--no-index", str(probe)])
        assert res.returncode == 0, f"{probe} is NOT git-ignored - personal data could be committed"
    tracked = subprocess.run(["git", "-C", str(root), "ls-files", str(default_vault.relative_to(root))],
                             capture_output=True, text=True).stdout.strip()
    assert tracked == "", f"vault files are tracked by git: {tracked}"
