"""assistant/live_settings.py: settings_get / settings_set allowlist, caps, secrets, live reload."""

from __future__ import annotations

import pytest

import config_store
from assistant import events, live_settings, tools
from assistant import settings as asettings
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPAGENT_CONFIG_FILE", str(tmp_path / "config.env"))
    config_store.reload()
    asettings.reset_assistant_settings()
    return tmp_path


def _set(setting, value):
    tools.available(["settings_set"])
    ctx = tools.ToolContext(channel="voice")
    return tools.dispatch("settings_set", {"setting": setting, "value": value}, ctx, allowed={"settings_set"})


def test_voice_by_name_engine_and_browser_voices(cfg):
    assert live_settings.apply("voice", "Lewis")["now"] == "Kokoro Lewis"
    v = config_store.read_values()
    assert v["VOICE_TTS_VOICE"] == "bm_lewis" and v["VOICE_TTS"] == "kokoro" and v["VOICE_WEB_TTS"] == "kokoro"
    assert events.BUS.since(0)[-1]["type"] == "settings"
    live_settings.remember_browser_voices(["Microsoft Ryan Online (Natural) - English (United Kingdom)",
                                           "Google UK English Male", 'bad"name'])
    assert live_settings.apply("voice", "Ryan")["now"].startswith("Microsoft Ryan")
    assert config_store.read_values()["VOICE_WEB_TTS"] == "browser"
    assert live_settings.current()["voice"].startswith("browser voice Microsoft Ryan")
    with pytest.raises(live_settings.SettingRefused):
        live_settings.apply("voice", "ElevenLabs")          # no key saved
    assert live_settings.apply("voice", "jarvis")["changed"] == ["VOICE_WEB_TTS", "VOICE_WEB_VOICE"]
    with pytest.raises(live_settings.SettingRefused):
        live_settings.apply("voice", "Gandalf")


def test_allowlist_caps_and_secrets(cfg):
    assert _set("assistant_daily_budget_usd", "4").ok
    assert config_store.read_values()["ASSISTANT_DAILY_BUDGET_USD"] == "4.00"
    over = _set("assistant_daily_budget_usd", "50")
    assert not over.ok and "capped" in over.content
    assert config_store.read_values()["ASSISTANT_DAILY_BUDGET_USD"] == "4.00"
    assert not _set("assistant_max_turn_usd", "9").ok
    for secret in ("ANTHROPIC_API_KEY", "dashboard_token", "twilio_auth_token"):
        r = _set(secret, "x")
        assert not r.ok
    with pytest.raises(live_settings.SettingRefused, match="never changed"):
        live_settings.apply("anthropic_api_key", "sk-x")
    with pytest.raises(live_settings.SettingRefused, match="not a setting"):
        live_settings.apply("dry_run", "false")
    assert "ANTHROPIC_API_KEY" not in config_store.read_values()


def test_other_settings(cfg):
    live_settings.apply("quiet_hours", "23:00 to 7:30")
    v = config_store.read_values()
    assert v["QUIET_HOURS_START"] == "23:00" and v["QUIET_HOURS_END"] == "07:30"
    live_settings.apply("wake_word", "hey jarvis")
    live_settings.apply("heartbeat_minutes", "45")
    live_settings.apply("voice_speed", "1.1")
    v = config_store.read_values()
    assert v["VOICE_WAKE_WORD"] == "hey_jarvis" and v["HEARTBEAT_MINUTES"] == "45" and v["VOICE_TTS_SPEED"] == "1.1"
    assert asettings.get_assistant_settings().heartbeat_minutes == 45        # caches reloaded
    for bad in (("quiet_hours", "late"), ("heartbeat_minutes", "999"), ("voice_speed", "9"), ("voice_engine", "x")):
        with pytest.raises(live_settings.SettingRefused):
            live_settings.apply(*bad)
    cur = live_settings.current()
    assert cur["quiet_hours"] == "23:00-07:30" and "Lewis" in cur["voices_available"]["kokoro"]
