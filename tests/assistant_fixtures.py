"""Shared fixtures for the assistant tests (tests/test_assistant_*.py).

``conftest.py`` belongs to phase 1, so these live here; each assistant test module does::

    from assistant_fixtures import assistant_env, fake_model  # noqa: F401
    pytestmark = pytest.mark.usefixtures("assistant_env")

``assistant_env``: scrubs ASSISTANT_* / VAULT_DIR / FAKE_ASSISTANT / SUBAGENT_* / OWNER_HONORIFIC,
points the vault at a temp dir, resets cached settings, the runtime backend and the tool registry.
``fake_model``: installs and returns a fresh ``runtime.FakeBackend``.
"""

from __future__ import annotations

import os

import pytest

from assistant import runtime, store, tools
from assistant import settings as asettings

_PREFIXES = ("ASSISTANT_", "SUBAGENT_", "HEARTBEAT_")
_KEYS = ("VAULT_DIR", "FAKE_ASSISTANT", "OWNER_HONORIFIC")


@pytest.fixture
def assistant_env(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith(_PREFIXES) or key in _KEYS:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("VAULT_DIR", str(tmp_path / "vault"))
    asettings.reset_assistant_settings()
    store._reset_migration_cache()
    runtime.reset()
    _reset_live()
    saved = dict(tools.REGISTRY)
    saved_handlers = dict(tools.APPROVAL_HANDLERS)
    yield tmp_path / "vault"
    tools.REGISTRY.clear()
    tools.REGISTRY.update(saved)
    tools._loaded_extensions.clear()  # let extension modules re-register into the restored registry
    tools.APPROVAL_HANDLERS.clear()
    tools.APPROVAL_HANDLERS.update(saved_handlers)
    runtime.reset()
    _reset_live()
    asettings.reset_assistant_settings()


def _reset_live() -> None:
    """The warm brain, its event bus, background-task runner and the page's voice engine."""
    from assistant import brain, events, quick, tasks

    brain.reset()
    events.reset()
    quick.reset()
    tasks.set_runner(None)
    try:
        from voice import web

        web.set_engines()
    except Exception:
        pass


@pytest.fixture
def fake_model(assistant_env):
    fb = runtime.FakeBackend()
    runtime.set_backend(fb)
    return fb
