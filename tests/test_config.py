from pathlib import Path

import pytest

import config


def test_defaults_under_isolated_env(settings, tmp_path):
    assert settings.data_dir == tmp_path / "data"
    assert settings.db_path == tmp_path / "data" / "clipagent.db"
    assert settings.downloads_dir == tmp_path / "data" / "media" / "downloads"
    assert settings.haiku_model == "claude-haiku-5-5"
    assert settings.sonnet_model == "claude-sonnet-5-5"
    assert settings.daily_llm_budget_usd == 1.0
    assert settings.max_uploads_per_day == 6
    assert settings.dry_run is True
    assert settings.fake_llm is True
    assert settings.anthropic_api_key == ""
    assert settings.timezone == "America/New_York"
    assert not settings.twilio_configured


def test_env_overrides_file(tmp_path, monkeypatch):
    env_file = tmp_path / "x.env"
    env_file.write_text("MAX_UPLOADS_PER_DAY=3\nDAILY_LLM_BUDGET_USD=2.5\nOWNER_NAME=Pat\n")
    monkeypatch.setenv("CLIPAGENT_CONFIG_FILE", str(env_file))
    monkeypatch.setenv("MAX_UPLOADS_PER_DAY", "4")
    s = config.load_settings()
    assert s.max_uploads_per_day == 4          # env wins
    assert s.daily_llm_budget_usd == 2.5       # from file
    assert s.owner_name == "Pat"


def test_config_file_does_not_mutate_environ(tmp_path, monkeypatch):
    import os

    env_file = tmp_path / "x.env"
    env_file.write_text("TWILIO_ACCOUNT_SID=ACxyz\n")
    monkeypatch.setenv("CLIPAGENT_CONFIG_FILE", str(env_file))
    s = config.load_settings()
    assert s.twilio_account_sid == "ACxyz"
    assert "TWILIO_ACCOUNT_SID" not in os.environ


def test_bool_parsing_and_errors(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "no")
    assert config.load_settings().dry_run is False
    monkeypatch.setenv("DRY_RUN", "maybe")
    with pytest.raises(ValueError):
        config.load_settings()


def test_price_overrides_json(monkeypatch):
    monkeypatch.setenv("LLM_PRICES_JSON", '{"claude-haiku-5-5": {"input": 1, "output": 2}}')
    assert config.load_settings().llm_price_overrides["claude-haiku-5-5"]["output"] == 2


def test_cache_reset_and_set():
    a = config.get_settings()
    assert config.get_settings() is a
    b = a.replace(max_uploads_per_day=1)
    config.set_settings(b)
    assert config.get_settings().max_uploads_per_day == 1
    config.reset_settings()
    assert config.get_settings().max_uploads_per_day == 6


def test_relative_paths_resolve_against_repo(monkeypatch):
    monkeypatch.setenv("CLIPAGENT_DATA_DIR", "somewhere")
    s = config.load_settings()
    assert s.data_dir == config.REPO_ROOT / "somewhere"
    assert isinstance(s.youtube_token_path, Path)


def test_operational_settings(monkeypatch):
    s = config.load_settings()
    assert (s.quiet_hours_start, s.quiet_hours_end, s.urgent_call_dedup_hours) == ("22:00", "08:00", 6.0)
    assert s.youtube_daily_quota == 10_000 and s.youtube_monetary_analytics is False
    assert s.scout_cc_backend == "ytdlp" and s.scout_cc_api_max_searches_per_day == 2
    monkeypatch.setenv("QUIET_HOURS_START", "7:05")
    monkeypatch.setenv("YOUTUBE_DAILY_QUOTA", "50000")
    monkeypatch.setenv("YOUTUBE_MONETARY_ANALYTICS", "true")
    monkeypatch.setenv("SCOUT_CC_BACKEND", "Data_API")
    monkeypatch.setenv("TWILIO_VOICE", "Polly.Matthew-Neural")
    s = config.load_settings()
    assert s.quiet_hours_start == "07:05" and s.youtube_daily_quota == 50_000
    assert s.youtube_monetary_analytics and s.scout_cc_backend == "data_api"
    assert s.twilio_voice == "Polly.Matthew-Neural"
    monkeypatch.setenv("QUIET_HOURS_END", "25:00")
    with pytest.raises(ValueError):
        config.load_settings()
