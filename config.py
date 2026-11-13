"""Typed runtime settings.

Values come from (highest priority first):

1. the process environment,
2. the ``config.env`` file (path overridable with ``CLIPAGENT_CONFIG_FILE``),
3. the defaults below.

``config.env`` is parsed with python-dotenv but is *not* copied into
``os.environ`` - importing this module has no side effects. Call
:func:`get_settings` to obtain the cached :class:`Settings`; tests call
:func:`reset_settings` (or :func:`set_settings`) after changing the environment.

Every variable is documented in ``config.env.example``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parent

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n", ""}


@dataclass(frozen=True, slots=True)
class Settings:
    # --- paths -------------------------------------------------------------
    data_dir: Path
    db_path: Path
    downloads_dir: Path
    transcripts_dir: Path
    renders_dir: Path
    strategy_path: Path

    # --- Anthropic ---------------------------------------------------------
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"
    haiku_model: str = "claude-haiku-5-5"
    sonnet_model: str = "claude-sonnet-5-5"
    haiku_effort: str = "low"
    sonnet_effort: str = "medium"
    llm_max_retries: int = 3
    llm_timeout_sec: float = 120.0
    # Sonnet requests opt into server-side refusal fallbacks (beta).
    llm_server_fallbacks: bool = True
    # {model: {input, output, cache_write, cache_read}} USD per MTok overrides.
    llm_price_overrides: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    fake_llm: bool = False

    # --- guardrails ----------------------------------------------------------
    daily_llm_budget_usd: float = 1.00
    max_daily_llm_budget_usd: float = 5.00  # absolute ceiling, even for approved strategies
    max_uploads_per_day: int = 6
    dry_run: bool = True

    # --- YouTube -------------------------------------------------------------
    youtube_client_secrets: Path = Path("client_secret.json")
    youtube_token_path: Path = Path("data/youtube_token.json")
    youtube_default_privacy: str = "private"
    youtube_category_id: str = "28"  # Science & Technology
    youtube_daily_quota: int = 10_000  # Data API units per Pacific day (raise after a Google quota increase)
    youtube_api_key: str = ""          # optional API key, only for scout's Data API CC search backend
    youtube_monetary_analytics: bool = False  # collect revenue when the token has the monetary scope

    # --- scout -----------------------------------------------------------------
    scout_cc_backend: str = "ytdlp"    # ytdlp (0 quota) | data_api (100 units per search)
    scout_cc_api_max_searches_per_day: int = 2

    # --- Twilio / owner ------------------------------------------------------
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    owner_phone: str = ""
    owner_name: str = "Blake"
    public_base_url: str = "http://localhost:8000"
    dashboard_token: str = ""
    # Local "HH:MM" window in which only urgent calls ring; equal start/end disables it.
    quiet_hours_start: str = "22:00"
    quiet_hours_end: str = "08:00"
    urgent_call_dedup_hours: float = 6.0   # at most one call per urgent kind in this window
    twilio_voice: str = "Polly.Joanna-Neural"
    twilio_language: str = "en-US"

    # --- misc ----------------------------------------------------------------
    timezone: str = "America/New_York"
    whisper_model: str = "small.en"
    whisper_device: str = "auto"
    whisper_compute_type: str = "int8"
    ffmpeg_bin: str = "ffmpeg"
    log_level: str = "INFO"
    # Disk hygiene (main.py cleanup job): delete downloaded source media this many days after the
    # source finished, and renders this many days after their live upload. DB rows, licence
    # evidence (info.json) and transcripts are always kept.
    source_media_retention_days: int = 3
    render_retention_days: int = 14

    def replace(self, **changes: Any) -> "Settings":
        """Return a copy with ``changes`` applied (handy in tests)."""
        return replace(self, **changes)

    def ensure_dirs(self) -> None:
        """Create the data/media directories. Never called at import time."""
        for d in (self.data_dir, self.downloads_dir, self.transcripts_dir, self.renders_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def twilio_configured(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token and self.twilio_from_number and self.owner_phone)


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------

def _bool(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    v = raw.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise ValueError(f"not a boolean: {raw!r}")


def _path(raw: str | None, default: Path, base: Path) -> Path:
    if raw is None or raw.strip() == "":
        return default
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (base / p)


def _hhmm(raw: str) -> str:
    """Validate a local ``HH:MM`` time (raises ``ValueError`` on a typo, at startup not at 22:00)."""
    h, _, m = raw.partition(":")
    if not (h.isdigit() and m.isdigit() and 0 <= int(h) <= 23 and 0 <= int(m) <= 59):
        raise ValueError(f"not an HH:MM time: {raw!r}")
    return f"{int(h):02d}:{int(m):02d}"


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    from dotenv import dotenv_values

    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Build :class:`Settings` from ``environ`` (default ``os.environ``) + ``config.env``."""
    env_in = dict(os.environ if environ is None else environ)
    config_file = _path(env_in.get("CLIPAGENT_CONFIG_FILE"), REPO_ROOT / "config.env", REPO_ROOT)
    env: dict[str, str] = {**_read_env_file(config_file), **env_in}

    def s(key: str, default: str) -> str:
        v = env.get(key)
        return default if v is None or v.strip() == "" else v.strip()

    data_dir = _path(env.get("CLIPAGENT_DATA_DIR"), REPO_ROOT / "data", REPO_ROOT)
    media_dir = _path(env.get("CLIPAGENT_MEDIA_DIR"), data_dir / "media", REPO_ROOT)

    overrides_raw = env.get("LLM_PRICES_JSON", "").strip()
    overrides = json.loads(overrides_raw) if overrides_raw else {}

    return Settings(
        data_dir=data_dir,
        db_path=_path(env.get("CLIPAGENT_DB_PATH"), data_dir / "clipagent.db", REPO_ROOT),
        downloads_dir=media_dir / "downloads",
        transcripts_dir=media_dir / "transcripts",
        renders_dir=media_dir / "renders",
        strategy_path=_path(env.get("CLIPAGENT_STRATEGY_PATH"), REPO_ROOT / "strategy.json", REPO_ROOT),
        anthropic_api_key=s("ANTHROPIC_API_KEY", ""),
        anthropic_base_url=s("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        haiku_model=s("HAIKU_MODEL", "claude-haiku-5-5"),
        sonnet_model=s("SONNET_MODEL", "claude-sonnet-5-5"),
        haiku_effort=s("HAIKU_EFFORT", "low"),
        sonnet_effort=s("SONNET_EFFORT", "medium"),
        llm_max_retries=int(s("LLM_MAX_RETRIES", "3")),
        llm_timeout_sec=float(s("LLM_TIMEOUT_SEC", "120")),
        llm_server_fallbacks=_bool(env.get("LLM_SERVER_FALLBACKS"), True),
        llm_price_overrides=overrides,
        fake_llm=_bool(env.get("FAKE_LLM"), False),
        daily_llm_budget_usd=float(s("DAILY_LLM_BUDGET_USD", "1.00")),
        max_daily_llm_budget_usd=float(s("MAX_DAILY_LLM_BUDGET_USD", "5.00")),
        max_uploads_per_day=int(s("MAX_UPLOADS_PER_DAY", "6")),
        dry_run=_bool(env.get("DRY_RUN"), True),
        youtube_client_secrets=_path(env.get("YOUTUBE_CLIENT_SECRETS"), REPO_ROOT / "client_secret.json", REPO_ROOT),
        youtube_token_path=_path(env.get("YOUTUBE_TOKEN_PATH"), data_dir / "youtube_token.json", REPO_ROOT),
        youtube_default_privacy=s("YOUTUBE_DEFAULT_PRIVACY", "private"),
        youtube_category_id=s("YOUTUBE_CATEGORY_ID", "28"),
        youtube_daily_quota=int(s("YOUTUBE_DAILY_QUOTA", "10000")),
        youtube_api_key=s("YOUTUBE_API_KEY", ""),
        youtube_monetary_analytics=_bool(env.get("YOUTUBE_MONETARY_ANALYTICS"), False),
        scout_cc_backend=s("SCOUT_CC_BACKEND", "ytdlp").lower(),
        scout_cc_api_max_searches_per_day=max(0, int(s("SCOUT_CC_API_MAX_SEARCHES_PER_DAY", "2"))),
        twilio_account_sid=s("TWILIO_ACCOUNT_SID", ""),
        twilio_auth_token=s("TWILIO_AUTH_TOKEN", ""),
        twilio_from_number=s("TWILIO_FROM_NUMBER", ""),
        owner_phone=s("OWNER_PHONE", ""),
        owner_name=s("OWNER_NAME", "Blake"),
        public_base_url=s("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
        dashboard_token=s("DASHBOARD_TOKEN", ""),
        quiet_hours_start=_hhmm(s("QUIET_HOURS_START", "22:00")),
        quiet_hours_end=_hhmm(s("QUIET_HOURS_END", "08:00")),
        urgent_call_dedup_hours=float(s("URGENT_CALL_DEDUP_HOURS", "6")),
        twilio_voice=s("TWILIO_VOICE", "Polly.Joanna-Neural"),
        twilio_language=s("TWILIO_LANGUAGE", "en-US"),
        timezone=s("TIMEZONE", "America/New_York"),
        whisper_model=s("WHISPER_MODEL", "small.en"),
        whisper_device=s("WHISPER_DEVICE", "auto"),
        whisper_compute_type=s("WHISPER_COMPUTE_TYPE", "int8"),
        ffmpeg_bin=s("FFMPEG_BIN", "ffmpeg"),
        log_level=s("LOG_LEVEL", "INFO").upper(),
        source_media_retention_days=max(0, int(s("SOURCE_MEDIA_RETENTION_DAYS", "3"))),
        render_retention_days=max(0, int(s("RENDER_RETENTION_DAYS", "14"))),
    )


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide settings, loading them on first use."""
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def set_settings(settings: Settings) -> None:
    """Install an explicit Settings instance (tests, scripts)."""
    global _settings
    _settings = settings


def reset_settings() -> None:
    """Forget cached settings so the next :func:`get_settings` re-reads the environment."""
    global _settings
    _settings = None
