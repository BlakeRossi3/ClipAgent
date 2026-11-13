"""config_store: comment-preserving, atomic edits of config.env + the config file location rules."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import config
import config_store


@pytest.fixture
def cfg(tmp_path, monkeypatch) -> Path:
    p = tmp_path / "cfg" / "config.env"
    monkeypatch.setenv("CLIPAGENT_CONFIG_FILE", str(p))
    config.reset_settings()
    return p


def test_render_preserves_comments_unknown_keys_and_order():
    text = ("# header comment\n\nANTHROPIC_API_KEY=old  # trailing\n# section\nCUSTOM_THING=keep me\n"
            "export DRY_RUN=true\nDRY_RUN=false\n")
    out = config_store.render(text, {"ANTHROPIC_API_KEY": "sk-ant-new", "DRY_RUN": "true", "NEW_KEY": "x y",
                                     "CUSTOM_GONE": None})
    lines = out.splitlines()
    assert lines[0] == "# header comment" and lines[3] == "# section"
    assert "CUSTOM_THING=keep me" in lines
    assert lines[2] == "ANTHROPIC_API_KEY=sk-ant-new"
    assert sum(1 for x in lines if x.startswith(("DRY_RUN", "export DRY_RUN"))) == 1   # duplicate collapsed
    assert lines[-1] == 'NEW_KEY="x y"'
    assert config_store.render("A=1\nB=2\n", {"A": None}) == "B=2\n"


def test_quote_round_trips_through_dotenv(cfg):
    tricky = 'pa ss"word\\with#hash and $dollar'
    config_store.update({"TWILIO_AUTH_TOKEN": tricky, "OWNER_NAME": "Blake O'Neil", "PUBLIC_BASE_URL": "https://x.y"})
    vals = config_store.read_values()
    assert vals["TWILIO_AUTH_TOKEN"] == tricky and vals["OWNER_NAME"] == "Blake O'Neil"
    assert config.get_settings().owner_name == "Blake O'Neil"     # settings reloaded


def test_rejects_bad_keys_and_multiline_values(cfg):
    with pytest.raises(config_store.ConfigStoreError):
        config_store.update({"bad-key": "1"})
    with pytest.raises(config_store.ConfigStoreError):
        config_store.update({"OWNER_NAME": "two\nlines"})
    assert not cfg.exists()


def test_update_keeps_existing_file_content_and_reports_restart_keys(cfg):
    cfg.parent.mkdir(parents=True)
    cfg.write_text("# my notes\nOWNER_NAME=Blake\n", encoding="utf-8")
    restart = config_store.update({"TIMEZONE": "America/Chicago", "OWNER_NAME": "B"})
    text = cfg.read_text(encoding="utf-8")
    assert text.startswith("# my notes\nOWNER_NAME=B\n") and "TIMEZONE=America/Chicago" in text
    assert restart == ["TIMEZONE"]
    assert list(cfg.parent.glob(".config.*")) == []        # temp file cleaned up
    if sys.platform != "win32":
        assert (cfg.stat().st_mode & 0o777) == 0o600


def test_new_file_gets_header(cfg):
    config_store.update({"OWNER_NAME": "Blake"})
    assert cfg.read_text(encoding="utf-8").startswith("# ClipAgent settings")


def test_config_file_path_rules(tmp_path, monkeypatch):
    repo_cfg = tmp_path / "repo-config.env"
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    data = tmp_path / "appdata"
    env = {"CLIPAGENT_DATA_DIR": str(data)}
    assert config.config_file_path(env) == data / "config.env"          # nothing exists yet: the data dir
    (tmp_path / "config.env").write_text("X=1\n", encoding="utf-8")
    assert config.config_file_path(env) == tmp_path / "config.env"        # legacy repo file still used
    data.mkdir(exist_ok=True)
    (data / "config.env").write_text("X=1\n", encoding="utf-8")
    assert config.config_file_path(env) == data / "config.env"           # data dir wins once it exists
    assert config.config_file_path({}) == tmp_path / "config.env"
    assert config.config_file_path({"CLIPAGENT_CONFIG_FILE": str(repo_cfg), **env}) == repo_cfg


def test_setup_complete_defaults_to_config_file_existing(tmp_path, monkeypatch):
    monkeypatch.delenv("SETUP_COMPLETE", raising=False)
    p = tmp_path / "c.env"
    monkeypatch.setenv("CLIPAGENT_CONFIG_FILE", str(p))
    config.reset_settings()
    assert config.get_settings().setup_complete is False and config.get_settings().config_file == p
    p.write_text("OWNER_NAME=Blake\n", encoding="utf-8")
    config.reset_settings()
    assert config.get_settings().setup_complete is True                  # hand-written config: no wizard
    p.write_text("SETUP_COMPLETE=false\n", encoding="utf-8")
    config.reset_settings()
    assert config.get_settings().setup_complete is False


def test_windows_skips_chmod(cfg, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    calls = []
    monkeypatch.setattr(os, "fchmod", lambda *a: calls.append(a), raising=False)
    config_store.update({"OWNER_NAME": "Blake"})
    assert calls == [] and cfg.is_file()
