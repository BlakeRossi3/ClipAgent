"""Settings for the assistant's toolkit modules (reels, desktop modes, history import, Twilio doctor).

Loaded like :mod:`assistant.settings` (env > ``config.env`` > defaults) and cached against
``config.get_settings()`` so tests that reset phase-1 settings re-read these too. Kept in its
own dataclass so the core assistant settings stay untouched. Variables are documented in
``config.env.example`` (section "Assistant toolkit").
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Any, Mapping

import config

COOKIE_BROWSERS = ("chrome", "edge", "firefox", "brave", "chromium", "opera", "vivaldi")
ENV_PREFIXES = ("REELS_", "HISTORY_IMPORT_", "DESKTOP_MODES_")


@dataclass(frozen=True, slots=True)
class ToolkitSettings:
    reels_browser_cookies: str = ""        # "" = never read browser cookies; else one of COOKIE_BROWSERS
    reels_max_duration_sec: int = 600      # refuse longer videos (a reel is short)
    reels_max_filesize_mb: int = 250
    reels_max_frames: int = 10             # keyframes handed to the model (8-12 is the sweet spot)
    reels_frame_px: int = 768              # longest side of each keyframe JPEG
    reels_keep_days: int = 14              # downloaded reels older than this are deleted by cleanup()
    history_batch_size: int = 15           # conversations distilled per run
    history_reserve_turns: int = 10        # Claude-plan runs always left for the owner's own chats
    history_max_upload_mb: int = 1024      # one uploaded export zip
    desktop_modes_dry_run: bool = False    # plan only, even on Windows

    def replace(self, **changes: Any) -> "ToolkitSettings":
        return replace(self, **changes)


def _int(v: str | None, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(str(v).strip())))
    except (TypeError, ValueError):
        return default


def load_toolkit_settings(environ: Mapping[str, str] | None = None) -> ToolkitSettings:
    env_in = dict(os.environ if environ is None else environ)
    base = config.get_settings() if environ is None else config.load_settings(environ)
    env = {**config._read_env_file(base.config_file), **env_in}
    browser = (env.get("REELS_BROWSER_COOKIES") or "").strip().lower()
    if browser in ("", "0", "false", "no", "off", "none"):
        browser = ""
    elif browser in ("1", "true", "yes", "on"):
        browser = "chrome"
    elif browser not in COOKIE_BROWSERS:
        browser = ""
    return ToolkitSettings(
        reels_browser_cookies=browser,
        reels_max_duration_sec=_int(env.get("REELS_MAX_DURATION_SEC"), 600, 10, 3 * 3600),
        reels_max_filesize_mb=_int(env.get("REELS_MAX_FILESIZE_MB"), 250, 5, 4096),
        reels_max_frames=_int(env.get("REELS_MAX_FRAMES"), 10, 1, 24),
        reels_frame_px=_int(env.get("REELS_FRAME_PX"), 768, 256, 2048),
        reels_keep_days=_int(env.get("REELS_KEEP_DAYS"), 14, 1, 3650),
        history_batch_size=_int(env.get("HISTORY_IMPORT_BATCH"), 15, 1, 200),
        history_reserve_turns=_int(env.get("HISTORY_IMPORT_RESERVE_TURNS"), 10, 0, 1000),
        history_max_upload_mb=_int(env.get("HISTORY_IMPORT_MAX_UPLOAD_MB"), 1024, 1, 8192),
        desktop_modes_dry_run=config._bool(env.get("DESKTOP_MODES_DRY_RUN"), False),
    )


_cache: tuple[config.Settings, ToolkitSettings] | None = None
_override: ToolkitSettings | None = None


def get_toolkit_settings() -> ToolkitSettings:
    global _cache
    if _override is not None:
        return _override
    key = config.get_settings()
    if _cache is None or _cache[0] is not key:
        _cache = (key, load_toolkit_settings())
    return _cache[1]


def set_toolkit_settings(s: ToolkitSettings | None) -> None:
    global _override
    _override = s


def reset_toolkit_settings() -> None:
    global _cache, _override
    _cache = None
    _override = None
