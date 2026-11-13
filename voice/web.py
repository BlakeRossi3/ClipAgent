"""``/voice/api/*`` for the dashboard: status, mute toggle, microphone list, "test my mic" level,
and the in-browser voice (``POST /voice/api/talk``: the home page's push-to-talk / always-listen).

In-browser voice: the page records with getUserMedia and posts the clip here. The server decodes it
(ffmpeg; a plain WAV is read directly), transcribes it with the voice loop's faster-whisper engine,
answers the request at once with what it heard, and runs the assistant turn in the background
(``home.start_background``, polled at ``/dashboard/chat/job/{id}``). The finished job carries the
reply text and a URL for the reply audio, synthesised here with the voice loop's engine chain
(Kokoro -> SAPI -> none) and served from memory by ``GET /voice/api/audio/{id}``.

Plus :func:`start_child` / :func:`stop_child`, used by ``main.py schedule`` to run ``python -m voice``
alongside the web server when VOICE_ENABLED=true.
"""

from __future__ import annotations

import io
import logging
import re
import subprocess
import sys
import threading
import uuid
import wave
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

log = logging.getLogger(__name__)
router = APIRouter(prefix="/voice", tags=["voice"])


def _d() -> Any:
    import dashboard

    return dashboard


@router.get("/api/status", include_in_schema=False)
def api_status(request: Request) -> Response:
    from voice import control
    from voice.settings import get_voice_settings

    d = _d()
    resp, _ = d._guard(request, api=True)
    if resp is not None:
        return resp
    return d._json({**control.status(), "enabled": get_voice_settings().enabled})


@router.post("/api/mute", include_in_schema=False)
async def api_mute(request: Request) -> Response:
    from voice.control import Control

    d = _d()
    resp, method = d._guard(request, api=True)
    if resp is not None:
        return resp
    form = await request.form()
    if not d._csrf_ok(request, method, form):
        return d._json({"ok": False, "error": "csrf"}, status_code=403)
    on = str(form.get("muted") or "1") in ("1", "true", "on")
    Control().set_muted(on, via="dashboard")
    return d._json({"ok": True, "muted": on})


@router.get("/api/devices", include_in_schema=False)
async def api_devices(request: Request) -> Response:
    from voice import audio
    from voice.settings import get_voice_settings

    d = _d()
    resp, _ = d._guard(request, api=True)
    if resp is not None:
        return resp
    try:
        devs = await run_in_threadpool(audio.list_input_devices)
    except audio.AudioUnavailable as exc:
        return d._json({"devices": [], "error": str(exc)})
    pick = audio.choose_device(devs, get_voice_settings().input_device)
    return d._json({"devices": [x.to_dict() for x in devs], "chosen": pick.name if pick else None})


@router.get("/api/level", include_in_schema=False)
async def api_level(request: Request) -> Response:
    from voice import audio

    d = _d()
    resp, _ = d._guard(request, api=True)
    if resp is not None:
        return resp
    name = (request.query_params.get("device") or "")[:120]
    try:
        return d._json(await run_in_threadpool(audio.measure_level, name, 0.3))
    except (audio.AudioUnavailable, audio.DeviceLost) as exc:
        return d._json({"error": str(exc), "level": 0}, status_code=200)


# ---------------------------------------------------------------------------
# in-browser voice (home page push-to-talk / always-listen)
# ---------------------------------------------------------------------------

TALK_MAX_BYTES = 4 * 1024 * 1024        # ~4 min of opus; far more than TALK_MAX_SEC needs
TALK_MAX_SEC = 60.0
TALK_MIN_SEC = 0.3
TALK_LIMITS = ((60, 12), (3600, 150))   # (window seconds, max clips) per session
RATE = 16000
AUDIO_KEEP = 12
_AUDIO_ID = re.compile(r"^[0-9a-f]{32}$")


class AudioDecodeError(Exception):
    pass


_audio_store: OrderedDict[str, bytes] = OrderedDict()
_audio_lock = threading.Lock()
_engine_lock = threading.Lock()
_stt_lock = threading.Lock()
_UNSET: Any = object()
_engines: dict[str, Any] = {"stt": _UNSET, "tts": _UNSET, "decoder": None}


def set_engines(*, stt: Any = _UNSET, tts: Any = _UNSET, decoder: Callable[[bytes], Any] | None = None) -> None:
    """Tests inject fakes (``stt.transcribe(samples) -> str``, ``tts.synthesize(text) -> (samples, rate)``,
    ``tts=None`` = no voice). Call with no arguments to go back to the real engines."""
    _engines.update(stt=stt, tts=tts, decoder=decoder)


def _stt() -> Any:
    with _engine_lock:
        if _engines["stt"] is _UNSET:
            from voice.engines import WhisperSTT
            from voice.settings import get_voice_settings

            v = get_voice_settings()
            _engines["stt"] = WhisperSTT(v.stt_model, device="cpu" if v.whisper_device == "cpu" else "auto",
                                         prompt=f"{v.assistant_name}.")
        return _engines["stt"]


def _tts() -> Any:
    with _engine_lock:
        if _engines["tts"] is _UNSET:
            from voice.engines import build_tts
            from voice.settings import get_voice_settings

            problems: list[str] = []
            try:
                _engines["tts"] = build_tts(get_voice_settings(), problems)
            except ValueError:  # no engine at all: replies are shown, not spoken
                _engines["tts"] = None
            if problems:
                log.warning("browser voice: %s", "; ".join(problems))
        return _engines["tts"]


def decode_audio(data: bytes) -> Any:
    """Recorded clip (webm/opus, ogg, mp4/aac or wav) -> float32 mono samples at 16 kHz."""
    import numpy as np

    from voice import audio

    if _engines["decoder"] is not None:
        return _engines["decoder"](data)
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        try:
            with wave.open(io.BytesIO(data), "rb") as w:
                rate, width, ch = w.getframerate(), w.getsampwidth(), w.getnchannels()
                raw = w.readframes(min(w.getnframes(), int(rate * (TALK_MAX_SEC + 1))))
        except (wave.Error, EOFError) as exc:
            raise AudioDecodeError(f"bad wav: {exc}") from exc
        if width != 2:
            raise AudioDecodeError("only 16-bit wav")
        x = np.frombuffer(raw, dtype=np.int16)
        if ch > 1:
            x = x.reshape(-1, ch)[:, 0]
        return audio.resample(audio.to_float(x), rate, RATE)
    import config

    cmd = [config.get_settings().ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
           "-t", str(int(TALK_MAX_SEC) + 1), "-vn", "-ac", "1", "-ar", str(RATE), "-f", "s16le", "pipe:1"]
    try:
        out = subprocess.run(cmd, input=data, capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioDecodeError(f"ffmpeg: {exc}") from exc
    if out.returncode != 0 or not out.stdout:
        raise AudioDecodeError("ffmpeg could not read the recording: "
                               + out.stderr.decode("utf-8", "replace")[-200:])
    return audio.to_float(np.frombuffer(out.stdout[: len(out.stdout) // 2 * 2], dtype=np.int16))


def encode_wav(samples: Any, rate: int) -> bytes:
    import numpy as np

    from voice import audio

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(audio.to_int16(np.asarray(samples, dtype=np.float32)).tobytes())
    return buf.getvalue()


def _keep_audio(data: bytes) -> str:
    aid = uuid.uuid4().hex
    with _audio_lock:
        _audio_store[aid] = data
        while len(_audio_store) > AUDIO_KEEP:
            _audio_store.popitem(last=False)
    return aid


def voice_turn(text: str) -> dict[str, Any]:
    """The background half: the assistant's answer (recorded on channel "voice") + its audio."""
    from voice.agent import brain_reply

    out = brain_reply(text)
    url, voice_name = None, None
    tts = _tts()
    if tts is not None and out.get("spoken"):
        try:
            samples, rate = tts.synthesize(out["spoken"])
            url = f"/voice/api/audio/{_keep_audio(encode_wav(samples, rate))}"
            voice_name = getattr(tts, "name", None)
        except Exception as exc:  # the text still shows; nothing is spoken
            log.warning("browser voice: speech synthesis failed: %s", exc)
    return {**out, "audio": url, "voice": voice_name}


def _talk_err(error: str, msg: str, status: int) -> Response:
    return _d()._json({"ok": False, "error": error, "msg": msg}, status_code=status)


@router.post("/api/talk", include_in_schema=False)
async def api_talk(request: Request) -> Response:
    """One recorded utterance from the home page -> ``{"ok", "heard", "job"}`` (poll the job)."""
    import home
    from voice import commands
    from voice.settings import get_voice_settings

    d = _d()
    try:
        size = int(request.headers.get("content-length") or 0)
    except ValueError:
        size = 0
    if size > TALK_MAX_BYTES + 65536:
        return _talk_err("too_large", "That recording is too long. Keep it under a minute.", 413)
    if "multipart/form-data" in request.headers.get("content-type", ""):
        try:
            await request.form(max_files=1, max_part_size=TALK_MAX_BYTES + 1024)  # cached for the guard
        except Exception:
            return _talk_err("too_large", "That recording is too long. Keep it under a minute.", 413)
    err, form = await home._post_guard(request, bucket="voice", limits=TALK_LIMITS)
    if err is not None:
        return err
    f = form.get("audio")
    if not hasattr(f, "read"):
        return _talk_err("no_audio", "No recording arrived.", 400)
    data = await f.read()
    if not data:
        return _talk_err("no_audio", "No recording arrived.", 400)
    if len(data) > TALK_MAX_BYTES:
        return _talk_err("too_large", "That recording is too long. Keep it under a minute.", 413)
    a = home.assistant_state()
    if not a["can_chat"]:
        return d._json({"ok": False, "error": "off", "msg": a["reason"], "reason": a["reason"]}, status_code=503)
    try:
        samples = await run_in_threadpool(decode_audio, data)
    except AudioDecodeError as exc:
        log.warning("browser voice: %s", exc)
        return _talk_err("unreadable", "I could not read that recording. Try again, or type it instead.", 400)
    secs = len(samples) / RATE
    if secs > TALK_MAX_SEC + 0.5:
        return _talk_err("too_long", "That recording is too long. Keep it under a minute.", 413)
    h = get_voice_settings().honorific
    if secs < TALK_MIN_SEC:
        return d._json({"ok": False, "error": "too_short", "msg": f"Hold the button while you speak, {h}."})

    def hear() -> str:
        with _stt_lock:
            return _stt().transcribe(samples)

    try:
        text = (await run_in_threadpool(hear) or "").strip()
    except Exception as exc:  # faster-whisper missing or the model failed to load
        db_log("speech recognition failed in the browser voice", exc)
        return _talk_err("stt", "Speech recognition is not working on the server; see the log. You can type instead.",
                         503)
    if commands.is_noise(text):
        return d._json({"ok": False, "error": "no_speech", "msg": f"I didn't catch that, {h}."})
    text = text[: home.CHAT_MAX_CHARS]
    cmd = commands.parse(text, get_voice_settings().assistant_name)
    if cmd in (commands.MUTE, commands.STOP, commands.CANCEL):  # handled in the page, no Claude
        return d._json({"ok": True, "heard": text, "command": cmd, "name": a["name"]})
    jid = home.start_background(lambda: voice_turn(text), what="voice turn")
    return d._json({"ok": True, "heard": text, "job": jid, "name": a["name"], "fake": a["fake"]})


def db_log(message: str, exc: Exception) -> None:
    import db

    log.warning("%s: %s", message, exc)
    db.log_event("voice", message, level="error", data={"error": repr(exc)[:300]})


@router.get("/api/audio/{audio_id}", include_in_schema=False)
def api_audio(request: Request, audio_id: str) -> Response:
    d = _d()
    resp, _ = d._guard(request, api=True)
    if resp is not None:
        return resp
    if not _AUDIO_ID.match(audio_id):
        return Response(status_code=404)
    with _audio_lock:
        data = _audio_store.get(audio_id)
    if data is None:
        return Response(status_code=404)
    return d._security_headers(Response(data, media_type="audio/wav", headers={"Cache-Control": "private, no-store"}))


# ---------------------------------------------------------------------------
# child process for `main.py schedule`
# ---------------------------------------------------------------------------

_child: subprocess.Popen[bytes] | None = None


def start_child() -> bool:
    """Start ``python -m voice`` when VOICE_ENABLED (its own lock stops a second copy)."""
    global _child
    from voice.settings import get_voice_settings

    s = get_voice_settings()
    if not s.enabled:
        return False
    s.log_path.parent.mkdir(parents=True, exist_ok=True)
    out = open(s.log_path.with_name("voice-console.log"), "ab")
    try:
        _child = subprocess.Popen([sys.executable, "-m", "voice"], cwd=str(Path(__file__).resolve().parent.parent),
                                  stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    finally:
        out.close()
    log.info("voice process started (pid %s)", _child.pid)
    return True


def stop_child() -> None:
    global _child
    c, _child = _child, None
    if c is not None and c.poll() is None:
        c.terminate()
        try:
            c.wait(timeout=5)
        except subprocess.TimeoutExpired:
            c.kill()
