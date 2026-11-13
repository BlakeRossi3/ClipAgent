"""Voice settings, loaded like :mod:`config` (env > ``config.env`` > defaults).

Variables (``VOICE_*``, ``ELEVENLABS_*``) are documented in ``config.env.example`` (Voice section)
and in ``docs/VOICE.md``. Also: the Settings page form helpers (:func:`form_values`, :func:`parse_form`)
so ``setup_web.py`` only needs two calls.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

import config

WAKE_MODES = ("both", "hey_jarvis", "name")          # openWakeWord "hey jarvis", the assistant's name, or both
TTS_ENGINES = ("kokoro", "sapi", "elevenlabs")
BARGE_IN_MODES = ("auto", "wake_only", "off")
KOKORO_VOICES = {   # the British voices in Kokoro v1.0 (bm_ = British male, bf_ = British female)
    "bm_george": "George - British male, warm and low (default)",
    "bm_lewis": "Lewis - British male, crisp",
    "bm_daniel": "Daniel - British male, light",
    "bm_fable": "Fable - British male, storyteller",
    "bf_emma": "Emma - British female",
    "am_michael": "Michael - American male",
}


@dataclass(frozen=True, slots=True)
class VoiceSettings:
    enabled: bool = False
    input_device: str = ""                 # part of the device name; "" = automatic
    output_device: str = ""                # "" = Windows default speakers
    wake_mode: str = "both"
    wake_threshold: float = 0.5
    tts_engine: str = "kokoro"
    tts_voice: str = "bm_george"
    tts_speed: float = 1.0
    sapi_voice: str = ""                   # part of a Windows voice name ("George", "David"); "" = best British male
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "JBFqnCBsd6RMkjVDRZzb"   # "George" in the ElevenLabs library
    stt_model: str = "small.en"
    name_model: str = "tiny.en"            # the small model that listens for the assistant's name
    whisper_device: str = "auto"
    followup_sec: float = 8.0
    filler_delay_sec: float = 0.7
    listen_timeout_sec: float = 6.0
    end_silence_sec: float = 0.7
    max_utterance_sec: float = 30.0
    barge_in: str = "auto"
    hotkey_talk: str = "<ctrl>+<alt>+<space>"
    hotkey_mute: str = "<ctrl>+<alt>+m"
    tray: bool = True
    rescan_sec: float = 60.0               # look for a new/better microphone this often while idle
    assistant_name: str = "Sterling"
    honorific: str = "sir"
    data_dir: Path = Path("data")

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "logs" / "voice.log"

    def replace(self, **kw: Any) -> "VoiceSettings":
        return replace(self, **kw)


def _choice(raw: str, allowed: tuple[str, ...], default: str) -> str:
    v = raw.strip().lower()
    return v if v in allowed else default


def _float(raw: str | None, default: float, lo: float, hi: float) -> float:
    try:
        return min(hi, max(lo, float(raw))) if raw not in (None, "") else default
    except ValueError:
        return default


def load_voice_settings(environ: Mapping[str, str] | None = None) -> VoiceSettings:
    env_in = dict(os.environ if environ is None else environ)
    base = config.get_settings() if environ is None else config.load_settings(environ)
    env = {**config._read_env_file(base.config_file), **env_in}

    def s(key: str, default: str = "") -> str:
        v = env.get(key)
        return default if v is None or v.strip() == "" else v.strip()

    try:
        from assistant.settings import get_assistant_settings, load_assistant_settings

        a = get_assistant_settings() if environ is None else load_assistant_settings(environ)
        name, honorific = a.name, a.owner_honorific
    except Exception:  # pragma: no cover - the assistant package is optional for the voice loop
        name, honorific = "Sterling", "sir"
    return VoiceSettings(
        enabled=config._bool(env.get("VOICE_ENABLED"), False),
        input_device=s("VOICE_INPUT_DEVICE"),
        output_device=s("VOICE_OUTPUT_DEVICE"),
        wake_mode=_choice(s("VOICE_WAKE_WORD", "both"), WAKE_MODES, "both"),
        wake_threshold=_float(env.get("VOICE_WAKE_THRESHOLD"), 0.5, 0.1, 0.95),
        tts_engine=_choice(s("VOICE_TTS", "kokoro"), TTS_ENGINES, "kokoro"),
        tts_voice=s("VOICE_TTS_VOICE", "bm_george"),
        tts_speed=_float(env.get("VOICE_TTS_SPEED"), 1.0, 0.5, 2.0),
        sapi_voice=s("VOICE_SAPI_VOICE"),
        elevenlabs_api_key=s("ELEVENLABS_API_KEY"),
        elevenlabs_voice_id=s("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb"),
        stt_model=s("VOICE_STT_MODEL", base.whisper_model),
        name_model=s("VOICE_NAME_MODEL", "tiny.en"),
        whisper_device=base.whisper_device,
        followup_sec=_float(env.get("VOICE_FOLLOWUP_SEC"), 8.0, 0.0, 60.0),
        filler_delay_sec=_float(env.get("VOICE_FILLER_DELAY_SEC"), 0.7, 0.0, 10.0),
        listen_timeout_sec=_float(env.get("VOICE_LISTEN_TIMEOUT_SEC"), 6.0, 2.0, 30.0),
        end_silence_sec=_float(env.get("VOICE_END_SILENCE_SEC"), 0.7, 0.3, 3.0),
        max_utterance_sec=_float(env.get("VOICE_MAX_UTTERANCE_SEC"), 30.0, 5.0, 120.0),
        barge_in=_choice(s("VOICE_BARGE_IN", "auto"), BARGE_IN_MODES, "auto"),
        hotkey_talk=s("VOICE_HOTKEY_TALK", "<ctrl>+<alt>+<space>"),
        hotkey_mute=s("VOICE_HOTKEY_MUTE", "<ctrl>+<alt>+m"),
        tray=config._bool(env.get("VOICE_TRAY"), True),
        rescan_sec=_float(env.get("VOICE_RESCAN_SEC"), 60.0, 0.0, 3600.0),
        assistant_name=name,
        honorific=honorific,
        data_dir=base.data_dir,
    )


_cache: tuple[config.Settings, VoiceSettings] | None = None
_override: VoiceSettings | None = None


def get_voice_settings() -> VoiceSettings:
    global _cache
    if _override is not None:
        return _override
    key = config.get_settings()
    if _cache is None or _cache[0] is not key:
        _cache = (key, load_voice_settings())
    return _cache[1]


def set_voice_settings(s: VoiceSettings | None) -> None:
    global _override
    _override = s


def reset_voice_settings() -> None:
    global _cache, _override
    _cache = _override = None


# ---------------------------------------------------------------------------
# Settings page (setup_web "power" step)
# ---------------------------------------------------------------------------

def form_values() -> dict[str, Any]:
    v = get_voice_settings()
    return {"voice_enabled": v.enabled, "voice_device": v.input_device, "voice_wake": v.wake_mode,
            "voice_tts": v.tts_engine, "voice_voice": v.tts_voice, "voice_sapi": v.sapi_voice,
            "voice_barge": v.barge_in, "voice_eleven_key_set": bool(v.elevenlabs_api_key),
            "voice_voices": KOKORO_VOICES, "voice_name": v.assistant_name}


def parse_form(form: Any) -> tuple[dict[str, str], dict[str, str | None]]:
    """Validate the voice fields of the Power & privacy form -> (errors, config.env changes).

    Fields that are absent (the first-run wizard shows only the on/off switch) are left alone.
    """
    errors: dict[str, str] = {}
    changes: dict[str, str | None] = {}

    def get(name: str) -> str | None:
        v = form.get(name)
        return None if v is None else " ".join(str(v).split())[:120]

    if get("voice_present") is not None:  # the form had the voice block: an unticked box means off
        changes["VOICE_ENABLED"] = "true" if form.get("voice_enabled") else "false"
    dev = get("voice_device")
    if dev is not None:
        if any(c in dev for c in "\"'\\\n=#"):
            errors["voice_device"] = "That microphone name has characters I cannot save."
        else:
            changes["VOICE_INPUT_DEVICE"] = dev
    for field, key, allowed in (("voice_wake", "VOICE_WAKE_WORD", WAKE_MODES),
                                ("voice_tts", "VOICE_TTS", TTS_ENGINES),
                                ("voice_barge", "VOICE_BARGE_IN", BARGE_IN_MODES)):
        val = get(field)
        if val is None:
            continue
        if val not in allowed:
            errors[field] = "Choose one of the options."
        else:
            changes[key] = val
    voice = get("voice_voice")
    if voice is not None:
        if voice not in KOKORO_VOICES:
            errors["voice_voice"] = "Choose one of the voices."
        else:
            changes["VOICE_TTS_VOICE"] = voice
    sapi = get("voice_sapi")
    if sapi is not None:
        if any(c in sapi for c in "\"'\\\n=#"):
            errors["voice_sapi"] = "That voice name has characters I cannot save."
        else:
            changes["VOICE_SAPI_VOICE"] = sapi
    key = str(form.get("voice_eleven_key") or "").strip()
    if key:
        if not key.replace("_", "").replace("-", "").isalnum() or len(key) > 200:
            errors["voice_eleven_key"] = "That does not look like an ElevenLabs API key."
        else:
            changes["ELEVENLABS_API_KEY"] = key
    elif form.get("voice_eleven_clear"):
        changes["ELEVENLABS_API_KEY"] = ""
    if changes.get("VOICE_TTS") == "elevenlabs" and not (key or get_voice_settings().elevenlabs_api_key):
        errors["voice_tts"] = "ElevenLabs needs an API key (paste it below), or pick the free local voice."
    return errors, ({} if errors else changes)
