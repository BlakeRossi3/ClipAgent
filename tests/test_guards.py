import socket

import pytest

import config


def test_network_is_blocked_in_tests():
    with pytest.raises(RuntimeError, match="network access"):
        socket.create_connection(("192.0.2.1", 443), timeout=1)


def test_no_ambient_credentials_leak(settings):
    assert settings.anthropic_api_key == ""
    assert settings.twilio_auth_token == ""
    assert config.get_settings().fake_llm and config.get_settings().dry_run
