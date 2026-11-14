"""``python -m voice``: the always-on voice process (lock, logging, models, mic loop, tray, hotkeys)."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
import threading
import time
from typing import Any

from voice import audio
from voice.control import Control
from voice.settings import VoiceSettings, get_voice_settings

log = logging.getLogger("voice")


def setup_logging(s: VoiceSettings) -> None:
    s.log_path.parent.mkdir(parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(s.log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(h)
    root.addHandler(logging.StreamHandler(sys.stderr))
    root.setLevel(logging.INFO)


def start_tray(agent: Any, name: str) -> None:
    """System-tray icon with Mute/Unmute/Quit (pystray). Best effort."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except Exception as exc:
        log.info("no tray icon (%s)", exc)
        return
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((8, 8, 56, 56), fill=(40, 170, 220, 255))
    menu = pystray.Menu(pystray.MenuItem("Mute / unmute", lambda: agent.post("toggle_mute")),
                        pystray.MenuItem("Talk now", lambda: agent.post("talk")),
                        pystray.MenuItem("Quit voice", lambda icon: (agent.post("quit"), icon.stop())))
    icon = pystray.Icon("clipagent-voice", img, f"{name} (voice)", menu)
    threading.Thread(target=icon.run, name="voice-tray", daemon=True).start()


def start_hotkeys(agent: Any, s: VoiceSettings) -> None:
    try:
        from pynput import keyboard
    except Exception as exc:
        log.info("no global hotkeys (%s)", exc)
        return
    try:
        hk = keyboard.GlobalHotKeys({s.hotkey_talk: lambda: agent.post("talk"),
                                     s.hotkey_mute: lambda: agent.post("toggle_mute")})
        hk.daemon = True
        hk.start()
    except Exception as exc:
        log.warning("hotkeys not registered: %s", exc)


def _announcer() -> Any:
    """Finished background tasks this process asked for, spoken when idle (assistant.tasks)."""
    from assistant import tasks

    return tasks.Announcer("voice")


def _tts_reloader() -> Any:
    """Rebuild the voice when config.env changed and the voice settings with it."""
    from voice import engines
    from voice.settings import get_voice_settings, reload_if_changed

    state = {"sig": get_voice_settings().tts_signature()}

    def check() -> Any:
        s = reload_if_changed()
        if s is None or s.tts_signature() == state["sig"]:
            return None
        state["sig"] = s.tts_signature()
        problems: list[str] = []
        tts = engines.build_tts(s, problems)
        for p in problems:
            log.warning("%s", p)
        return tts

    return check


def build_agent(s: VoiceSettings) -> Any:
    from voice import engines, models
    from voice.agent import VoiceAgent, assistant_brain, store_history

    models.ensure_models(s.models_dir, wake=s.wake_mode != "name", kokoro=s.tts_engine == "kokoro",
                         say=lambda m: log.info("%s", m))
    eng, problems = engines.build_engines(s)
    for p in problems:
        log.warning("%s", p)
    return VoiceAgent(wake=eng["wake"], vad=eng["vad"], stt=eng["stt"], name_stt=eng["name_stt"], tts=eng["tts"],
                      player=eng["player"], brain=assistant_brain, history=store_history, control=Control(),
                      settings=s, announcer=_announcer(), tts_reloader=_tts_reloader())


def run_loop(agent: Any, s: VoiceSettings, *, open_mic: Any = None, sleep: Any = time.sleep) -> None:
    """Mic frames -> agent. Closes the mic while muted; reopens / re-selects after device loss."""
    def default_open() -> audio.MicSource:
        dev = audio.choose_device(audio.list_input_devices(), s.input_device)
        if dev is None:
            raise audio.DeviceLost("no microphone found")
        if s.input_device and s.input_device.lower() not in dev.name.lower():
            log.warning("microphone %r not found; using %r", s.input_device, dev.name)
        return audio.MicSource(dev).open()

    open_mic = open_mic or default_open
    mic = None
    opened_at = 0.0
    while not agent.quit:
        if agent.state == "muted":
            if mic is not None:
                mic.close()
                mic = None
            agent.tick()
            sleep(0.2)
            continue
        if mic is None:
            try:
                mic = open_mic()
                agent.device_name = mic.device.name
                opened_at = time.monotonic()
                agent._publish()
            except Exception as exc:
                log.warning("no microphone: %s", exc)
                agent.control.publish("no_mic", error=str(exc)[:200])
                agent.tick()
                sleep(3.0)
                continue
        try:
            frame = mic.read(timeout=0.5)
        except audio.DeviceLost as exc:
            log.warning("%s; selecting a microphone again", exc)
            mic.close()
            mic = None
            continue
        if frame is None:
            agent.tick()
        else:
            agent.feed(frame)
        if s.rescan_sec and agent.state == "idle" and time.monotonic() - opened_at > s.rescan_sec:
            mic.close()  # pick up a newly plugged headset / changed default
            mic = None
    if mic is not None:
        mic.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m voice", description="ClipAgent voice assistant")
    p.add_argument("--list-devices", action="store_true", help="list microphones and exit")
    p.add_argument("--download-models", action="store_true", help="download the voice models and exit")
    p.add_argument("--force", action="store_true", help="run even when VOICE_ENABLED is false")
    args = p.parse_args(argv)
    s = get_voice_settings()
    if args.list_devices:
        devs = audio.list_input_devices()
        pick = audio.choose_device(devs, s.input_device)
        for d in devs:
            print(f"{'*' if pick and d.index == pick.index else ' '} [{d.index}] {d.name} ({d.hostapi})")
        return 0
    if args.download_models:
        from voice import models

        return 1 if models.ensure_models(s.models_dir, say=print) else 0
    if not s.enabled and not args.force:
        print("voice is off (VOICE_ENABLED=false); use --force to run anyway", file=sys.stderr)
        return 0
    import db
    import main as clipagent_main

    try:
        lock = clipagent_main.acquire_lock("voice")
    except clipagent_main.Busy as exc:
        print(f"voice already running: {exc}", file=sys.stderr)
        return 0
    setup_logging(s)
    db.init_db()
    control = Control()
    control.publish("starting")
    try:
        agent = build_agent(s)
    except Exception as exc:
        log.exception("voice could not start")
        control.publish("stopped", error=str(exc)[:200])
        return 1
    if s.tray:
        start_tray(agent, s.assistant_name)
    start_hotkeys(agent, s)
    db.log_event("voice", "voice loop started", data={"wake": s.wake_mode, "voice": agent.tts.name})
    try:
        run_loop(agent, s)
    except KeyboardInterrupt:
        pass
    finally:
        control.publish("stopped")
        clipagent_main._unlock(lock)
    return 0
