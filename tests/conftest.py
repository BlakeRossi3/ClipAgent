"""Shared pytest fixtures. Every builder should reuse these.

Autouse (applies to every test):

* ``_isolated_env`` - fresh temp data dir + DB, a temp copy of ``strategy.json``,
  ``FAKE_LLM=true``, ``DRY_RUN=true``, no Anthropic/Twilio/YouTube credentials
  (ambient ``ANTHROPIC_*`` etc. are removed), settings cache reset, llm fakes reset.
* ``_no_network`` - any socket connection to a non-loopback address fails the test.

Opt-in fixtures: ``settings``, ``strategy``, ``fake_llm``, ``clock``,
``make_source``, ``make_clip``, ``make_upload``.
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import config
import db
import llm
import strategy as strategy_mod

REPO_ROOT = Path(__file__).resolve().parent.parent

_SCRUB_PREFIXES = ("ANTHROPIC_", "TWILIO_", "YOUTUBE_", "CLIPAGENT_", "OWNER_", "SCOUT_", "QUIET_HOURS_",
                   "ASSISTANT_", "SUBAGENT_")
_SCRUB_KEYS = ("FAKE_LLM", "DRY_RUN", "DAILY_LLM_BUDGET_USD", "MAX_DAILY_LLM_BUDGET_USD", "MAX_UPLOADS_PER_DAY",
               "LLM_PRICES_JSON", "HAIKU_MODEL", "SONNET_MODEL", "TIMEZONE", "PUBLIC_BASE_URL", "DASHBOARD_TOKEN", "DASHBOARD_ALLOW_UNAUTHENTICATED",
               "LLM_SERVER_FALLBACKS", "HAIKU_EFFORT", "SONNET_EFFORT", "URGENT_CALL_DEDUP_HOURS",
               "SOURCE_MEDIA_RETENTION_DAYS", "RENDER_RETENTION_DAYS", "LOG_LEVEL", "FFMPEG_BIN",
               "VAULT_DIR", "FAKE_ASSISTANT")


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for key in list(os.environ):
        if key.startswith(_SCRUB_PREFIXES) or key in _SCRUB_KEYS:
            monkeypatch.delenv(key, raising=False)
    data_dir = tmp_path / "data"
    strat = tmp_path / "strategy.json"
    shutil.copy(REPO_ROOT / "strategy.json", strat)
    monkeypatch.setenv("CLIPAGENT_CONFIG_FILE", str(tmp_path / "no-config.env"))
    monkeypatch.setenv("CLIPAGENT_DATA_DIR", str(data_dir))
    monkeypatch.setenv("CLIPAGENT_STRATEGY_PATH", str(strat))
    monkeypatch.setenv("FAKE_LLM", "true")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("TIMEZONE", "America/New_York")
    monkeypatch.setenv("VAULT_DIR", str(tmp_path / "vault"))  # never the owner's real vault
    config.reset_settings()
    llm.reset()
    config.get_settings().ensure_dirs()
    yield
    llm.reset()
    config.reset_settings()


def _is_loopback(address: Any) -> bool:
    if isinstance(address, tuple) and address:
        host = address[0]
    else:
        return True  # AF_UNIX etc.
    if host in ("localhost", "testserver"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch):
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded_connect(self, address):
        if not _is_loopback(address):
            raise RuntimeError(f"network access attempted in tests: {address!r}")
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        if not _is_loopback(address):
            raise RuntimeError(f"network access attempted in tests: {address!r}")
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    yield


@pytest.fixture
def settings() -> config.Settings:
    return config.get_settings()


@pytest.fixture
def strategy() -> strategy_mod.Strategy:
    return strategy_mod.load_strategy()


# ---------------------------------------------------------------------------
# fake LLM
# ---------------------------------------------------------------------------

@dataclass
class FakeLLM:
    """Programmable stand-in for the Anthropic API.

    * ``queue(*replies)`` - replies returned in order (str, dict/list or llm.FakeReply).
    * ``by_purpose[purpose] = reply_or_callable`` - per-purpose replies.
    * ``handler = fn(req) -> reply`` - full control.
    * With nothing programmed, falls back to the deterministic schema sample.
    * ``calls`` records every ``llm.LLMRequest``.
    """

    calls: list[llm.LLMRequest] = field(default_factory=list)
    replies: list[Any] = field(default_factory=list)
    by_purpose: dict[str, Any] = field(default_factory=dict)
    handler: Callable[[llm.LLMRequest], Any] | None = None

    def queue(self, *replies: Any) -> "FakeLLM":
        self.replies.extend(replies)
        return self

    def __call__(self, req: llm.LLMRequest) -> Any:
        self.calls.append(req)
        if self.replies:
            return self.replies.pop(0)
        if req.purpose in self.by_purpose:
            r = self.by_purpose[req.purpose]
            return r(req) if callable(r) else r
        if self.handler is not None:
            return self.handler(req)
        return llm._default_fake(req)

    def purposes(self) -> list[str]:
        return [c.purpose for c in self.calls]


@pytest.fixture
def fake_llm() -> FakeLLM:
    fake = FakeLLM()
    llm.set_fake_handler(fake)
    return fake


# ---------------------------------------------------------------------------
# time
# ---------------------------------------------------------------------------

class Clock:
    """Frozen, steerable UTC clock (patches ``db.utcnow``)."""

    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def set(self, now: datetime) -> None:
        self.now = now if now.tzinfo else now.replace(tzinfo=UTC)

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """Start frozen at 2026-11-13 15:00 UTC (10:00 America/New_York)."""
    c = Clock(datetime(2026, 11, 13, 15, 0, tzinfo=UTC))
    monkeypatch.setattr(db, "utcnow", c)
    return c


# ---------------------------------------------------------------------------
# row factories
# ---------------------------------------------------------------------------

@pytest.fixture
def make_source() -> Callable[..., int]:
    counter = iter(range(1, 10_000))

    def _make(status: str = "candidate", **kw: Any) -> int:
        n = next(counter)
        defaults: dict[str, Any] = dict(
            url=f"https://www.youtube.com/watch?v=test{n:04d}", source_type="nasa", platform="youtube",
            external_id=f"test{n:04d}", channel="NASA", channel_id="UCLA_DiR1FfKNvjuUpBHmylQ",
            title=f"Test source {n}", duration_sec=600.0, licence="us_gov_public_domain",
            licence_evidence={"uploader": "NASA"}, topic="NASA Artemis",
        )
        defaults.update(kw)
        url, stype = defaults.pop("url"), defaults.pop("source_type")
        sid = db.add_source(url, stype, **defaults)
        assert sid is not None
        if status != "candidate":
            db.set_source_status(sid, status, force=True)
        return sid

    return _make


@pytest.fixture
def make_clip(make_source) -> Callable[..., int]:
    def _make(source_id: int | None = None, status: str = "picked", **kw: Any) -> int:
        sid = source_id if source_id is not None else make_source(status="picked")
        defaults: dict[str, Any] = dict(start_sec=10.0, end_sec=55.0, title="Test clip", description="desc",
                                        hashtags=["#Shorts"], hook_text="Wait for it", credit_text="Video: NASA",
                                        haiku_score=8.0)
        defaults.update(kw)
        start, end = defaults.pop("start_sec"), defaults.pop("end_sec")
        cid = db.add_clip(sid, start, end, **defaults)
        if status != "picked":
            db.set_clip_status(cid, status, force=True)
        return cid

    return _make


@pytest.fixture
def make_upload(make_clip) -> Callable[..., int]:
    counter = iter(range(1, 10_000))

    def _make(clip_id: int | None = None, *, video_id: str | None = None, dry_run: bool = False,
              publish_at: str | None = None, succeeded: bool = True) -> int:
        cid = clip_id if clip_id is not None else make_clip(status="rendered")
        uid = db.add_upload(cid, privacy="private", publish_at=publish_at, dry_run=dry_run)
        if succeeded:
            db.mark_upload_succeeded(uid, video_id or f"vid{next(counter):05d}")
        return uid

    return _make
