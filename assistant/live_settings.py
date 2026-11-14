"""``settings_get`` / ``settings_set``: Sterling reads and changes his settings - every one except secrets.

Blake says "switch to Lewis" and the very next sentence is in Lewis's voice: the tool writes
``config.env`` (comment-preserving, atomic: :mod:`config_store`), drops every settings cache, tells
the Jarvis page over the event bus (``settings`` event: it reloads its speaker), and the server-side
voice is rebuilt on its next sentence because ``voice.web`` keys its engine on
:meth:`VoiceSettings.tts_signature`. The separate laptop voice process notices the file change
(``voice.settings.reload_if_changed``) and rebuilds its voice too.

The friendly names below get special handling (voice lookup by name, "22:00-08:00" parsing...). Any
other name is a config key (``DRY_RUN``, ``MAX_UPLOADS_PER_DAY``...) and goes to
:func:`assistant.admin.config_set`: everything is settable except :data:`assistant.admin.SECRET_KEYS`
(and anything matching ``admin.SECRET_RE``). Blake owns the values: only sanity bounds remain
(money cannot be negative), no artificial caps.

==========================  ==============================================================
setting                     value
==========================  ==============================================================
voice                       a voice by name: George, Lewis, Fable, Daniel, Emma, Michael
                            (Kokoro), a browser voice ("Ryan", "Google UK English Male"),
                            "browser", "ElevenLabs" or "Jarvis" (the reference repo's voice)
voice_engine                browser | kokoro | elevenlabs | sapi
voice_speed                 0.5 - 2.0 (Kokoro)
wake_word                   both | hey_jarvis | name
heartbeat_minutes           0 - 240 (0 = off; the schedule changes after a restart)
quiet_hours                 "22:00-08:00"
assistant_daily_budget_usd  dollars, >= 0
assistant_max_turn_usd      dollars, >= 0
==========================  ==============================================================
"""

from __future__ import annotations

import re
from typing import Any

import db

KV_BROWSER_VOICES = "voice:browser_voices"
SETTINGS = ("voice", "voice_engine", "voice_speed", "wake_word", "heartbeat_minutes", "quiet_hours",
            "assistant_daily_budget_usd", "assistant_max_turn_usd")
_HHMM = re.compile(r"^([01]?\d|2[0-3])[:.]?([0-5]\d)$")
_WAKE = {"both": "both", "either": "both", "hey jarvis": "hey_jarvis", "hey_jarvis": "hey_jarvis", "jarvis": "hey_jarvis",
         "name": "name", "my name": "name", "sterling": "name", "your name": "name"}


class SettingRefused(ValueError):
    """Shown to the model as the tool's error text."""


def browser_voices() -> list[str]:
    v = db.kv_get(KV_BROWSER_VOICES, [])
    return [str(x) for x in v][:80] if isinstance(v, list) else []


def remember_browser_voices(names: list[str]) -> list[str]:
    clean = []
    for n in names[:120]:
        n = " ".join(str(n).split())[:120]
        if n and not any(c in n for c in "\"'\\\n=#") and n not in clean:
            clean.append(n)
    db.kv_set(KV_BROWSER_VOICES, clean)
    return clean


def current() -> dict[str, Any]:
    """settings_get: the allowlisted settings, the voices he can pick, and what each one means."""
    import config
    from assistant.settings import get_assistant_settings
    from voice.settings import KOKORO_VOICES, get_voice_settings, kokoro_name

    v, a, c = get_voice_settings(), get_assistant_settings(), config.get_settings()
    return {
        "voice": _describe_voice(v),
        "voice_engine": {"jarvis_page": v.web_tts, "laptop": v.tts_engine},
        "voice_speed": v.tts_speed,
        "wake_word": v.wake_mode,
        "heartbeat_minutes": a.heartbeat_minutes,
        "quiet_hours": f"{c.quiet_hours_start}-{c.quiet_hours_end}",
        "assistant_daily_budget_usd": a.daily_budget_usd,
        "assistant_max_turn_usd": a.max_turn_usd,
        "voices_available": {"kokoro": [kokoro_name(k) for k in KOKORO_VOICES],
                             "browser": browser_voices()[:30],
                             "elevenlabs": "available" if v.elevenlabs_api_key else "needs an ElevenLabs key in Settings"},
        "settable": list(SETTINGS) + ["any other non-secret config key (config_get lists them)"],
    }


def _describe_voice(v: Any) -> str:
    from voice.settings import kokoro_name

    if v.web_tts == "kokoro":
        return f"Kokoro {kokoro_name(v.tts_voice)}"
    if v.web_tts == "elevenlabs":
        return "ElevenLabs"
    return f"browser voice {v.web_voice}" if v.web_voice else "the browser's best British male voice"


def _money(setting: str, value: Any) -> float:
    try:
        x = float(str(value).replace("$", "").strip())
    except ValueError:
        raise SettingRefused(f"{setting} needs a number of dollars.") from None
    if x < 0:
        raise SettingRefused(f"{setting} cannot be negative.")
    return round(x, 2)


def resolve_voice(value: str) -> tuple[dict[str, str], str]:
    """A spoken voice name -> (config.env changes, how to say it)."""
    from voice.settings import KOKORO_VOICES, get_voice_settings, kokoro_name

    v = get_voice_settings()
    want = " ".join(str(value).lower().replace("voice", " ").split())
    if not want:
        raise SettingRefused("Which voice? Say a name such as Lewis or George.")
    for key in KOKORO_VOICES:
        if want in (key, kokoro_name(key).lower(), f"kokoro {kokoro_name(key).lower()}"):
            return ({"VOICE_TTS_VOICE": key, "VOICE_TTS": "kokoro", "VOICE_WEB_TTS": "kokoro"},
                    f"Kokoro {kokoro_name(key)}")
    if want in ("elevenlabs", "eleven labs", "jarvis", "the jarvis", "jarvis repo", "jarvis repository", "reference"):
        if v.elevenlabs_api_key:
            from voice.settings import ELEVENLABS_DEFAULT_VOICE

            return ({"VOICE_WEB_TTS": "elevenlabs", "VOICE_TTS": "elevenlabs",
                     "ELEVENLABS_VOICE_ID": ELEVENLABS_DEFAULT_VOICE}, "the ElevenLabs Jarvis voice")
        if want.startswith("eleven"):
            raise SettingRefused("ElevenLabs needs an API key; he can paste one in Settings, Power & privacy, Voice.")
        return {"VOICE_WEB_TTS": "browser", "VOICE_WEB_VOICE": ""}, "the browser's British voice, as the Jarvis repo uses"
    if want in ("browser", "default", "system", "windows", "the browser", "browser default"):
        return {"VOICE_WEB_TTS": "browser", "VOICE_WEB_VOICE": ""}, "the browser's best British male voice"
    names = browser_voices()
    hits = [n for n in names if want in n.lower()]
    if hits:
        hits.sort(key=lambda n: (("natural" not in n.lower()), ("online" not in n.lower()), len(n)))
        return {"VOICE_WEB_TTS": "browser", "VOICE_WEB_VOICE": hits[0]}, hits[0].split(" - ")[0]
    options = ", ".join(kokoro_name(k) for k in KOKORO_VOICES if k.startswith("bm_"))
    raise SettingRefused(f"No voice called {value!r}. Kokoro voices: {options}; or a browser voice, "
                         "or ElevenLabs. settings_get lists the browser voices.")


def changes_for(setting: str, value: Any) -> tuple[dict[str, str], str]:
    """(config.env changes, a short description). Raises SettingRefused."""
    import config
    from voice.settings import BARGE_IN_MODES  # noqa: F401 - keeps the import cheap and local
    from voice.settings import get_voice_settings

    from assistant import admin

    setting = str(setting or "").strip().lower()
    if admin.is_secret_key(setting):
        raise SettingRefused("Refused: keys, tokens and passwords are never changed by me. He can change "
                             "them himself in Settings.")
    if setting not in SETTINGS:
        raise SettingRefused(f"{setting!r} is not one of the friendly settings ({', '.join(SETTINGS)}); "
                             "use config_set with its config key.")
    sval = " ".join(str(value if value is not None else "").split())
    if setting == "voice":
        return resolve_voice(sval)
    if setting == "voice_engine":
        e = sval.lower().replace(" ", "")
        e = {"windows": "sapi", "eleven": "elevenlabs", "elevenlabs": "elevenlabs", "chrome": "browser",
             "edge": "browser"}.get(e, e)
        if e not in ("browser", "kokoro", "elevenlabs", "sapi"):
            raise SettingRefused("voice_engine is browser, kokoro, elevenlabs or sapi.")
        if e == "elevenlabs" and not get_voice_settings().elevenlabs_api_key:
            raise SettingRefused("ElevenLabs needs an API key; he can paste one in Settings.")
        ch = {"VOICE_WEB_TTS": e} if e != "sapi" else {}
        if e != "browser":
            ch["VOICE_TTS"] = e
        return ch, e
    if setting == "voice_speed":
        try:
            x = float(sval)
        except ValueError:
            raise SettingRefused("voice_speed is a number between 0.5 and 2.") from None
        if not 0.5 <= x <= 2.0:
            raise SettingRefused("voice_speed is between 0.5 and 2.")
        return {"VOICE_TTS_SPEED": f"{x:g}"}, f"speed {x:g}"
    if setting == "wake_word":
        w = _WAKE.get(sval.lower())
        if w is None:
            raise SettingRefused("wake_word is both, hey_jarvis or name.")
        return {"VOICE_WAKE_WORD": w}, w
    if setting == "heartbeat_minutes":
        try:
            n = int(float(sval))
        except ValueError:
            raise SettingRefused("heartbeat_minutes is a whole number of minutes (0 = off).") from None
        if not 0 <= n <= 240:
            raise SettingRefused("heartbeat_minutes is between 0 and 240.")
        return {"HEARTBEAT_MINUTES": str(n)}, f"every {n} minutes (from the next restart)" if n else "off"
    if setting == "quiet_hours":
        parts = re.split(r"\s*(?:-|to|until)\s*", sval.lower())
        if len(parts) != 2:
            raise SettingRefused('quiet_hours looks like "22:00-08:00".')
        out = []
        for p in parts:
            m = _HHMM.match(p.strip())
            if not m:
                raise SettingRefused('quiet_hours looks like "22:00-08:00" (24-hour clock).')
            out.append(f"{int(m.group(1)):02d}:{m.group(2)}")
        return {"QUIET_HOURS_START": out[0], "QUIET_HOURS_END": out[1]}, f"{out[0]} to {out[1]}"
    if setting == "assistant_daily_budget_usd":
        x = _money(setting, sval)
        return {"ASSISTANT_DAILY_BUDGET_USD": f"{x:.2f}"}, f"${x:.2f} a day"
    if setting == "assistant_max_turn_usd":
        x = _money(setting, sval)
        return {"ASSISTANT_MAX_TURN_USD": f"{x:.2f}"}, f"${x:.2f} a turn"
    raise SettingRefused(f"{setting} cannot be set.")  # pragma: no cover - SETTINGS is exhaustive
    _ = config  # pragma: no cover


def apply(setting: str, value: Any, *, via: str = "assistant") -> dict[str, Any]:
    """settings_set: validate, write config.env, reload every cache, tell the page. Raises SettingRefused."""
    import config_store
    from assistant import admin, events

    name = str(setting or "").strip()
    if name.lower() not in SETTINGS:  # any other non-secret setting: the administrator path
        if admin.is_secret_key(name):
            raise SettingRefused("Refused: keys, tokens and passwords are never changed by me. He can change "
                                 "them himself in Settings.")
        try:
            return admin.config_set(name, value, via=via)
        except admin.AdminError as exc:
            raise SettingRefused(str(exc)) from None
    setting = name.lower()
    changes, said = changes_for(setting, value)
    if any(admin.is_secret_key(k) and k != "ELEVENLABS_VOICE_ID" for k in changes):  # belt and braces
        raise SettingRefused("Refused: that would change a secret.")
    config_store.update(changes)
    config_store.reload()
    try:
        from voice import settings as vsettings

        vsettings.reset_voice_settings()
        from voice import web

        web.reload_tts()
    except Exception:  # the voice package is optional
        pass
    db.log_event("assistant", f"setting changed: {setting}", data={"via": via, "keys": sorted(changes),
                                                                   "value": said[:80]})
    if setting in ("voice", "voice_engine", "voice_speed"):
        from voice.settings import get_voice_settings

        events.publish("settings", voice=get_voice_settings().public())
    return {"ok": True, "setting": setting, "now": said, "changed": sorted(changes)}


# ---------------------------------------------------------------------------
# as tools (assistant.tools registry; the brain's MCP server offers them)
# ---------------------------------------------------------------------------

def _get(ctx: Any, a: dict[str, Any]) -> Any:
    return current()


def _set(ctx: Any, a: dict[str, Any]) -> Any:
    from assistant.tools import ToolError

    try:
        return apply(str(a.get("setting") or ""), a.get("value"), via=getattr(ctx, "channel", "assistant"))
    except SettingRefused as exc:
        raise ToolError(str(exc)) from None


def register_tools(register: Any) -> None:
    from assistant.tools import Tool, _obj

    register(Tool("settings_get", "Read your own voice and assistant settings (current voice and engine, wake "
                                  "word, heartbeat, quiet hours, budgets) and the voices he can pick. Every other "
                                  "setting: config_get.",
                  _obj({}), _get, owner="assistant.live_settings"), replace=True)
    register(Tool("settings_set", "Change one of your settings at once, live, e.g. {setting: 'voice', value: "
                                  "'Lewis'}. Friendly names: " + ", ".join(SETTINGS) + "; any other non-secret "
                                  "config key works too (e.g. DRY_RUN). Secrets are never changed. Do it "
                                  "immediately when he asks, then confirm in a few words.",
                  _obj({"setting": {"type": "string"},
                        "value": {"type": "string", "description": "The new value, e.g. 'Lewis', '22:00-07:00', '4'."}},
                       required=["setting", "value"]),
                  _set, owner="assistant.live_settings", side_effects=True), replace=True)
