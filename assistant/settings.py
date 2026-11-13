"""Assistant settings, loaded exactly like :mod:`config` (env > ``config.env`` > defaults).

The assistant has its own frozen dataclass so ``config.py`` stays untouched. It is
cached against the identity of ``config.get_settings()``: whenever phase-1 settings
are reset (tests do this per test) these are re-read too.

Variables are documented in ``config.env.example`` (Phase 2 section).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

import config

BACKENDS = ("api", "claude_code")


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
    max_tokens: int = 2048                   # main agent output ceiling per call (thinking included)
    subagent_max_tokens: int = 3072
    web_search: bool = False                 # offer the server-side web_search tool to agents that list it
    vault_dir: Path = Path("data/vault")
    fake: bool = True                        # deterministic offline runtime
    timezone: str = "America/New_York"
    dry_run: bool = True
    # --- backend: "api" (Anthropic API key, per-token billing) or "claude_code" (the owner's Claude
    # subscription through the Claude Code CLI in headless mode; see assistant/claude_code.py)
    backend: str = "api"
    cc_model: str = "opus"                   # Claude Code model alias for the main agent
    cc_subagent_model: str = "sonnet"        # ... and for every sub-agent (lighter on plan limits)
    cc_fallback_models: tuple[str, ...] = ("sonnet",)   # tried in order when a model is not on the plan
    max_turns_per_day: int = 60              # claude -p runs per local day (protects the plan's limits)
    cc_timeout_sec: int = 240                # one claude -p run (tool calls included)
    claude_bin: str = ""                     # explicit path to the claude executable (else PATH / ~/.local/bin)
    cc_oauth_token: str = ""                 # optional CLAUDE_CODE_OAUTH_TOKEN the owner set himself (never shown)

    def replace(self, **changes: Any) -> "AssistantSettings":
        return replace(self, **changes)


def load_assistant_settings(environ: Mapping[str, str] | None = None) -> AssistantSettings:
    """Build settings from ``environ`` (default ``os.environ``) + the phase-1 ``config.env`` file."""
    env_in = dict(os.environ if environ is None else environ)
    base = config.get_settings() if environ is None else config.load_settings(environ)
    env = {**config._read_env_file(base.config_file), **env_in}

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
        max_tokens=max(256, int(s("ASSISTANT_MAX_TOKENS", "2048"))),
        subagent_max_tokens=max(256, int(s("SUBAGENT_MAX_TOKENS", "3072"))),
        web_search=config._bool(env.get("ASSISTANT_WEB_SEARCH"), False),
        vault_dir=config._path(env.get("VAULT_DIR"), base.data_dir / "vault", config.REPO_ROOT),
        fake=config._bool(env.get("FAKE_ASSISTANT"), base.fake_llm),
        timezone=base.timezone,
        dry_run=base.dry_run,
        backend=s("ASSISTANT_BACKEND", "api").lower() if s("ASSISTANT_BACKEND", "api").lower() in BACKENDS
        else "api",
        cc_model=s("ASSISTANT_CC_MODEL", "opus"),
        cc_subagent_model=s("SUBAGENT_CC_MODEL", "sonnet"),
        cc_fallback_models=tuple(m.strip() for m in s("ASSISTANT_CC_FALLBACK", "sonnet").split(",") if m.strip()),
        max_turns_per_day=max(0, int(s("ASSISTANT_MAX_TURNS_PER_DAY", "60"))),
        cc_timeout_sec=max(30, int(s("ASSISTANT_CC_TIMEOUT_SEC", "240"))),
        claude_bin=s("ASSISTANT_CLAUDE_BIN", ""),
        cc_oauth_token=s("CLAUDE_CODE_OAUTH_TOKEN", ""),
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
