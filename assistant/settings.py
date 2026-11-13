"""Assistant settings, loaded exactly like :mod:`config` (env > ``config.env`` > defaults).

The assistant has its own frozen dataclass so ``config.py`` stays untouched. It is
cached against the identity of ``config.get_settings()``: whenever phase-1 settings
are reset (tests do this per test) these are re-read too.

Variables are documented in ``config.assistant.env.example``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

import config


@dataclass(frozen=True, slots=True)
class AssistantSettings:
    enabled: bool = False
    name: str = "Sterling"
    owner_honorific: str = "sir"
    owner_name: str = "Blake"
    model: str = "claude-fable-5-2"          # main agent
    subagent_model: str = "claude-opus-5-5"  # every sub-agent
    effort: str = "medium"
    subagent_effort: str = "low"
    daily_budget_usd: float = 3.00           # separate from the pipeline's DAILY_LLM_BUDGET_USD
    max_turn_usd: float = 0.50               # ceiling for one conversation turn (all calls inside it)
    max_sms_per_day: int = 8                 # proactive outbound texts (replies to the owner do not count)
    max_tool_rounds: int = 8                 # model<->tool round trips per agent run
    vault_dir: Path = Path("data/vault")
    fake: bool = True                        # deterministic offline runtime
    timezone: str = "America/New_York"
    dry_run: bool = True

    def replace(self, **changes: Any) -> "AssistantSettings":
        return replace(self, **changes)


def load_assistant_settings(environ: Mapping[str, str] | None = None) -> AssistantSettings:
    """Build settings from ``environ`` (default ``os.environ``) + the phase-1 ``config.env`` file."""
    env_in = dict(os.environ if environ is None else environ)
    config_file = config._path(env_in.get("CLIPAGENT_CONFIG_FILE"), config.REPO_ROOT / "config.env",
                               config.REPO_ROOT)
    env = {**config._read_env_file(config_file), **env_in}
    base = config.get_settings() if environ is None else config.load_settings(environ)

    def s(key: str, default: str) -> str:
        v = env.get(key)
        return default if v is None or v.strip() == "" else v.strip()

    return AssistantSettings(
        enabled=config._bool(env.get("ASSISTANT_ENABLED"), False),
        name=s("ASSISTANT_NAME", "Sterling"),
        owner_honorific=s("OWNER_HONORIFIC", "sir"),
        owner_name=base.owner_name,
        model=s("ASSISTANT_MODEL", "claude-fable-5-2"),
        subagent_model=s("SUBAGENT_MODEL", "claude-opus-5-5"),
        effort=s("ASSISTANT_EFFORT", "medium"),
        subagent_effort=s("SUBAGENT_EFFORT", "low"),
        daily_budget_usd=max(0.0, float(s("ASSISTANT_DAILY_BUDGET_USD", "3.00"))),
        max_turn_usd=max(0.0, float(s("ASSISTANT_MAX_TURN_USD", "0.50"))),
        max_sms_per_day=max(0, int(s("ASSISTANT_MAX_SMS_PER_DAY", "8"))),
        max_tool_rounds=max(1, int(s("ASSISTANT_MAX_TOOL_ROUNDS", "8"))),
        vault_dir=config._path(env.get("VAULT_DIR"), base.data_dir / "vault", config.REPO_ROOT),
        fake=config._bool(env.get("FAKE_ASSISTANT"), base.fake_llm),
        timezone=base.timezone,
        dry_run=base.dry_run,
    )


_cache: tuple[config.Settings, AssistantSettings] | None = None
_override: AssistantSettings | None = None


def get_assistant_settings() -> AssistantSettings:
    """Cached settings; re-read whenever ``config`` settings are reset."""
    global _cache
    if _override is not None:
        return _override
    key = config.get_settings()  # held strongly, so an `is` check cannot be fooled by id reuse
    if _cache is None or _cache[0] is not key:
        _cache = (key, load_assistant_settings())
    return _cache[1]


def set_assistant_settings(settings: AssistantSettings | None) -> None:
    """Install explicit settings (tests, scripts); ``None`` returns to env loading."""
    global _override
    _override = settings


def reset_assistant_settings() -> None:
    global _cache, _override
    _cache = None
    _override = None
