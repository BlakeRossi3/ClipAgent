"""``/voice/api/*``: the Jarvis page's live link to Sterling's warm brain, plus the laptop voice loop's
status, mute toggle, microphone list and "test my mic" level.

* ``GET  /voice/api/stream``     server-sent events from ``assistant.events`` (turns, text deltas,
  sentences to speak, tool steps, tasks, ``display`` panels, settings changes). ``?after=<id>``
  resumes; ``?once=1`` returns the backlog and ends (tests, debugging).
* ``GET  /voice/api/activity``   the rail's snapshot + the voice to speak with.
* ``POST /voice/api/talk``       one recorded utterance -> speech-to-text only (faster-whisper):
  ``{"ok", "heard"}``, a local command, or ``no_speech`` (``quiet``: noise or a filler like "maybe, um").
* ``POST /voice/api/ask``        ``text`` -> a turn of the warm brain (``assistant/brain.py``). The answer
  streams over ``/stream``; with ``Accept: application/x-ndjson`` (the laptop voice process) this
  request itself streams the turn's sentences.
  Utterances that arrive before the previous one has been answered aloud are stitched into one request
  (``merged`` = the turn replaced); see ``brain.ask``.
* ``POST /voice/api/interrupt``  barge-in: stop the answer in flight (only while he is being spoken to).
* ``POST /voice/api/tts``        ``text`` -> audio from the server-side voice (Kokoro / ElevenLabs), for the
  page when it is not using the browser's own voice. The engine is rebuilt whenever the voice settings
  change (:func:`reload_tts`; it used to be cached for the life of the process).
* ``POST /voice/api/voices``     the page reports the browser's voice names (for ``settings_set``).
* ``GET  /voice/api/img?u=``     the SSRF-guarded image proxy for panels and map tiles (``assistant/hud.py``):
  OpenStreetMap tiles, YouTube thumbnails, Wikimedia, or an image the model put in a panel; nothing else.
* ``GET  /voice/api/media?u=``   the same for a video file the model showed; ``/voice/api/clip/<id>`` streams one
  of his rendered Shorts (``renders_dir`` only).
* ``GET  /voice/api/diagnostics`` what the ``D`` panel shows from the server side: brain connection, speech
  settings, his MCP servers (names only), recent timings. ``POST /voice/api/timing``: the page reports how long
  it took from the end of his speech to the first spoken word (also published as a ``timing`` event).

Auth: the dashboard's (cookie / token / loopback) plus CSRF on POSTs; the laptop voice process uses
a local key from the shared database (``X-Voice-Key``, loopback only).

Plus :func:`start_child` / :func:`stop_child`, used by ``main.py schedule`` to run ``python -m voice``
alongside the web server when VOICE_ENABLED=true.
"""

from __future__ import annotations

import asyncio
import hmac
import io
import json
import logging
import secrets
import subprocess
import sys
import threading
import time
import wave
from collections import deque
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse

log = logging.getLogger(__name__)
router = APIRouter(prefix="/voice", tags=["voice"])
KV_LOCAL_KEY = "voice:local_key"
TTS_MAX_CHARS = 600
SSE_PING_SEC = 15.0


def _d() -> Any:
    import dashboard

    return dashboard


def local_key() -> str:
    """Shared secret for the laptop voice process (same machine, same database)."""
    import db

    db.kv_set_if_absent(KV_LOCAL_KEY, secrets.token_hex(24))
    return str(db.kv_get(KV_LOCAL_KEY))


def _local_ok(request: Request) -> bool:
    key = request.headers.get("x-voice-key") or ""
    return bool(key) and _d()._is_loopback(request) and hmac.compare_digest(key.encode(), local_key().encode())


@router.get("/api/status", include_in_schema=False)
def api_status(request: Request) -> Response:
    from voice import control
    from voice.settings import get_voice_settings

    d = _d()
    resp, _ = d._guard(request, api=True)
    if resp is not None:
        return resp
    out = {**control.status(), "enabled": get_voice_settings().enabled}
    try:
        from assistant import tasks

        out["tasks"] = [{"id": t["id"], "task": t["task"], "started": t["started"]} for t in tasks.running_tasks()]
    except Exception as exc:  # the status pill must still work
        log.warning("voice task status failed: %s", exc)
    return d._json(out)


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
# in-browser voice (the Jarvis page)
# ---------------------------------------------------------------------------

TALK_MAX_BYTES = 4 * 1024 * 1024        # ~4 min of opus; far more than TALK_MAX_SEC needs
TALK_MAX_SEC = 60.0
TALK_MIN_SEC = 0.3
TALK_LIMITS = ((60, 30), (3600, 400))   # (window seconds, max clips) per session; VAD segments are short
ASK_LIMITS = ((60, 30), (3600, 400))
RATE = 16000


class AudioDecodeError(Exception):
    pass


_engine_lock = threading.Lock()
_tts_lock = threading.Lock()
_stt_lock = threading.Lock()
_UNSET: Any = object()
_engines: dict[str, Any] = {"stt": _UNSET, "tts": _UNSET, "decoder": None, "builder": None}
_tts_cache: dict[str, Any] = {"sig": None, "engine": None}


def set_engines(*, stt: Any = _UNSET, tts: Any = _UNSET, decoder: Callable[[bytes], Any] | None = None,
                tts_builder: Callable[[Any], Any] | None = None) -> None:
    """Tests inject fakes (``stt.transcribe(samples) -> str``, ``tts.synthesize(text) -> (samples, rate)``,
    ``tts=None`` = no server voice, ``tts_builder(voice_settings) -> tts`` = rebuilt per settings).
    Call with no arguments to go back to the real engines."""
    _engines.update(stt=stt, tts=tts, decoder=decoder, builder=tts_builder)
    reload_tts()


def reload_tts() -> None:
    """Forget the server-side voice; the next sentence rebuilds it from the current settings."""
    with _engine_lock:
        _tts_cache.update(sig=None, engine=None)


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
    """The server-side voice for the page, keyed on the voice settings: a change in Settings or a
    ``settings_set`` call rebuilds it (it used to be built once and cached for ever)."""
    from voice.settings import get_voice_settings

    if _engines["tts"] is not _UNSET:
        return _engines["tts"]
    v = get_voice_settings()
    engine = v.web_tts if v.web_tts != "browser" else v.tts_engine
    v = v.replace(tts_engine=engine if engine in ("kokoro", "sapi", "elevenlabs") else "kokoro")
    sig = v.tts_signature()
    with _engine_lock:
        if _tts_cache["sig"] == sig:
            return _tts_cache["engine"]
        problems: list[str] = []
        try:
            if _engines["builder"] is not None:
                eng = _engines["builder"](v)
            else:
                from voice.engines import build_tts

                eng = build_tts(v, problems)
        except ValueError:   # no engine at all: the page falls back to the browser's voice
            eng = None
        if problems:
            log.warning("page voice: %s", "; ".join(problems))
        _tts_cache.update(sig=sig, engine=eng)
        return eng


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


def synthesize(text: str) -> tuple[bytes, str] | None:
    """One sentence -> (wav bytes, voice name) from the server-side voice, or None."""
    tts = _tts()
    if tts is None or not text.strip():
        return None
    try:
        with _tts_lock:
            samples, rate = tts.synthesize(text)
        return encode_wav(samples, rate), str(getattr(tts, "name", "voice"))
    except Exception as exc:  # the page falls back to the browser's voice
        log.warning("page voice: speech synthesis failed: %s", exc)
        return None


def _talk_err(error: str, msg: str, status: int, **extra: Any) -> Response:
    return _d()._json({"ok": False, "error": error, "msg": msg, **extra}, status_code=status)


def _can_talk() -> tuple[bool, str | None, dict[str, Any]]:
    import home

    a = home.assistant_state()
    return bool(a["can_chat"]), a["reason"], a


@router.post("/api/talk", include_in_schema=False)
async def api_talk(request: Request) -> Response:
    """One recorded utterance -> what was heard (speech-to-text only; the page then asks)."""
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
    ok, reason, a = _can_talk()
    if not ok:
        return d._json({"ok": False, "error": "off", "msg": reason, "reason": reason}, status_code=503)
    t0 = time.monotonic()
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
        return d._json({"ok": False, "error": "too_short", "quiet": True, "msg": f"Hold the button while you speak, {h}."})

    def hear() -> str:
        with _stt_lock:
            return _stt().transcribe(samples)

    try:
        text = (await run_in_threadpool(hear) or "").strip()
    except Exception as exc:  # faster-whisper missing or the model failed to load
        db_log("speech recognition failed in the browser voice", exc)
        return _talk_err("stt", "Speech recognition is not working on the server; see the log. You can type instead.",
                         503)
    stt_ms = round((time.monotonic() - t0) * 1000)
    cmd = commands.parse(text, get_voice_settings().assistant_name)
    if cmd in (commands.MUTE, commands.STOP, commands.CANCEL):  # handled in the page, no Claude
        return d._json({"ok": True, "heard": text, "command": cmd, "name": a["name"], "stt_ms": stt_ms})
    if commands.is_noise(text):
        return d._json({"ok": False, "error": "no_speech", "quiet": True, "heard": text, "msg": ""})
    return d._json({"ok": True, "heard": text[: home.CHAT_MAX_CHARS], "name": a["name"], "fake": a["fake"],
                    "stt_ms": stt_ms})


def ignore_reason(text: str) -> str | None:
    """Why an utterance gets no reply: noise, or a filler like "maybe, um" (wait for the real request)."""
    from voice import commands

    if commands.is_noise(text):
        return "noise"
    if commands.is_filler_utterance(text):
        return "filler"
    return None


async def _ask_guard(request: Request) -> tuple[Response | None, Any]:
    """Dashboard auth + CSRF, or the laptop voice process's local key."""
    import home

    if _local_ok(request):
        return None, await request.form()
    return await home._post_guard(request, bucket="ask", limits=ASK_LIMITS)


@router.post("/api/ask", include_in_schema=False)
async def api_ask(request: Request) -> Response:
    """Something he said (already text) -> a turn of the warm brain."""
    import home
    from assistant import brain, events

    err, form = await _ask_guard(request)
    if err is not None:
        return err
    d = _d()
    text = " ".join(str(form.get("text") or "").split())[: home.CHAT_MAX_CHARS]
    origin = str(form.get("origin") or "browser")
    origin = origin if origin in ("browser", "voice", "text") else "browser"
    if not text:
        return _talk_err("empty", "Nothing to send.", 400)
    why = ignore_reason(text) if form.get("typed") is None else None
    if why:
        events.publish("ignored", text=text[:120], reason=why)
        return d._json({"ok": False, "error": "no_speech", "quiet": True, "reason": why})
    ok, reason, a = _can_talk()
    if not ok:
        if origin == "voice":
            return d._json({"ok": True, "turn": None, "spoken": reason, "status": "off"})
        return d._json({"ok": False, "error": "off", "msg": reason, "reason": reason}, status_code=503)
    if "application/x-ndjson" not in request.headers.get("accept", ""):
        turn = await run_in_threadpool(brain.ask, text, origin=origin)
        return d._json({"ok": True, "turn": turn.id, "heard": turn.heard, "merged": turn.merged_from, "name": a["name"]})

    import queue as _q

    lines: _q.SimpleQueue[str | None] = _q.SimpleQueue()
    turn = await run_in_threadpool(lambda: brain.ask(text, origin=origin,
                                                     on_sentence=lambda s: lines.put(json.dumps({"sentence": s}))))

    def finish() -> None:
        res = turn.wait(brain.TURN_TIMEOUT_SEC + 30)
        lines.put(json.dumps({"done": True, **res}))
        lines.put(None)

    threading.Thread(target=finish, name="voice-ask", daemon=True).start()

    async def gen() -> Any:
        while True:
            try:
                line = lines.get_nowait()
            except _q.Empty:
                await asyncio.sleep(0.05)
                continue
            if line is None:
                return
            yield line + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@router.post("/api/interrupt", include_in_schema=False)
async def api_interrupt(request: Request) -> Response:
    from assistant import brain

    err, _form = await _ask_guard(request)
    if err is not None:
        return err
    return _d()._json({"ok": True, "interrupted": brain.interrupt()})


@router.post("/api/voices", include_in_schema=False)
async def api_voices(request: Request) -> Response:
    """The page reports the browser's voices (names only), so "switch to Ryan" can find one."""
    from assistant import live_settings

    err, form = await _ask_guard(request)
    if err is not None:
        return err
    try:
        names = json.loads(str(form.get("voices") or "[]"))
    except ValueError:
        names = []
    kept = live_settings.remember_browser_voices([n for n in names if isinstance(n, str)]) if isinstance(names, list) else []
    return _d()._json({"ok": True, "count": len(kept)})


@router.post("/api/tts", include_in_schema=False)
async def api_tts(request: Request) -> Response:
    """One sentence -> wav from the server-side voice (Kokoro / ElevenLabs). 503 = use the browser's."""
    err, form = await _ask_guard(request)
    if err is not None:
        return err
    text = " ".join(str(form.get("text") or "").split())[:TTS_MAX_CHARS]
    if not text:
        return _talk_err("empty", "No text.", 400)
    out = await run_in_threadpool(synthesize, text)
    if out is None:
        return _talk_err("no_voice", "The server voice is not available.", 503)
    data, name = out
    return _d()._security_headers(Response(data, media_type="audio/wav",
                                           headers={"Cache-Control": "private, no-store", "X-Voice": name}))


def activity() -> dict[str, Any]:
    from assistant import events, hud, tasks
    from voice.settings import get_voice_settings

    snap = events.snapshot()
    try:
        snap["ui"] = hud.ui_state()
    except Exception as exc:  # the rail must still load
        log.warning("ui state unreadable: %s", exc)
    live = {t["id"]: t for t in snap["tasks"]}
    for t in tasks.list_tasks():   # tasks from before this server started (kv) fill the gaps
        live.setdefault(t["id"], tasks.public(t))
    snap["tasks"] = sorted(live.values(), key=lambda x: x.get("started") or "")[-10:]
    snap["voice"] = get_voice_settings().public()
    return snap


@router.get("/api/activity", include_in_schema=False)
def api_activity(request: Request) -> Response:
    d = _d()
    resp, _ = d._guard(request, api=True)
    if resp is not None:
        return resp
    return d._json(activity())


def _sse(evt: dict[str, Any]) -> str:
    data = json.dumps({k: v for k, v in evt.items() if k != "t"}, ensure_ascii=False, default=str)
    return f"id: {evt['id']}\nevent: {evt['type']}\ndata: {data}\n\n"


@router.get("/api/stream", include_in_schema=False)
async def api_stream(request: Request) -> Response:
    """Server-sent events: everything Sterling is saying and doing, live."""
    from assistant import events

    d = _d()
    resp, _ = d._guard(request, api=True)
    if resp is not None:
        return resp
    raw = request.query_params.get("after") or request.headers.get("last-event-id") or ""
    after = int(raw) if raw.isdigit() else events.BUS.seq
    once = request.query_params.get("once") == "1"

    async def gen() -> Any:
        nonlocal after
        events.BUS.listeners += 1
        try:
            yield f"retry: 2000\nevent: hello\ndata: {json.dumps({'seq': events.BUS.seq})}\n\n"
            last_ping = time.monotonic()
            while True:
                batch = events.BUS.since(after)
                for evt in batch:
                    after = evt["id"]
                    yield _sse(evt)
                if once:
                    return
                if await request.is_disconnected():
                    return
                if time.monotonic() - last_ping > SSE_PING_SEC:
                    last_ping = time.monotonic()
                    yield ": ping\n\n"
                await asyncio.sleep(0.05)
        finally:
            events.BUS.listeners -= 1

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# the HUD: image/media proxy, rendered Shorts, diagnostics, timings
# ---------------------------------------------------------------------------

def _proxy_response(kind: str, request: Request) -> Any:
    from assistant import hud

    async def go() -> Response:
        d = _d()
        resp, _ = d._guard(request, api=True)
        if resp is not None:
            return resp
        url = request.query_params.get("u") or ""
        try:
            ctype, body = await run_in_threadpool(hud.fetch_remote, url, kind=kind)
        except hud.ProxyError as exc:
            return d._security_headers(Response(str(exc), status_code=exc.status, media_type="text/plain"))
        r = d._security_headers(Response(body, media_type=ctype))
        r.headers["Cache-Control"] = "private, max-age=86400"
        r.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
        return r
    return go()


@router.get("/api/img", include_in_schema=False)
async def api_img(request: Request) -> Response:
    """Remote images for panels and map tiles, fetched here so the page never talks to the wider web."""
    return await _proxy_response("image", request)


@router.get("/api/media", include_in_schema=False)
async def api_media(request: Request) -> Response:
    return await _proxy_response("video", request)


@router.get("/api/clip/{clip_id}", include_in_schema=False)
def api_clip(request: Request, clip_id: int) -> Response:
    """One of his rendered Shorts, for a ``show_media`` panel (files under renders_dir only)."""
    import config
    import db
    from fastapi.responses import FileResponse

    d = _d()
    resp, _ = d._guard(request, api=True)
    if resp is not None:
        return resp
    c = db.get_clip(clip_id)
    root = config.get_settings().renders_dir.resolve()
    try:
        path = Path(c.render_path).resolve() if c is not None and c.render_path else None
    except OSError:
        path = None
    if path is None or not path.is_file() or root not in path.parents or path.suffix.lower() != ".mp4":
        return d._json({"error": "not_found"}, status_code=404)
    return d._security_headers(FileResponse(path, media_type="video/mp4"))


TIMINGS: deque[dict[str, Any]] = deque(maxlen=20)


@router.post("/api/timing", include_in_schema=False)
async def api_timing(request: Request) -> Response:
    """The page: time from the end of his speech to the first spoken word (and how it heard him)."""
    from assistant import events

    err, form = await _ask_guard(request)
    if err is not None:
        return err

    def num(k: str) -> float | None:
        try:
            v = float(str(form.get(k) or ""))
        except ValueError:
            return None
        return round(v) if 0 <= v < 600_000 else None

    row = {"turn": str(form.get("turn") or "")[:40], "stt": str(form.get("stt") or "")[:20],
           "speech_to_first_word_ms": num("first_word_ms"), "stt_ms": num("stt_ms"), "ask_ms": num("ask_ms"),
           "tts": str(form.get("tts") or "")[:30]}
    import db

    row["ts"] = db.now_iso()
    TIMINGS.append(row)
    events.publish("timing", timing=row)
    log.info("voice timing: %s", row)
    return _d()._json({"ok": True})


def diagnostics() -> dict[str, Any]:
    from assistant import brain, events
    from voice import control
    from voice.settings import get_voice_settings

    v = get_voice_settings()
    out: dict[str, Any] = {"voice": v.public(), "stt": v.web_stt, "laptop": control.status(),
                           "listeners": events.BUS.listeners, "timings": list(TIMINGS)[-5:]}
    try:
        st = brain.get_brain().status() if brain.running() else {"running": False, "connected": False}
        out["brain"] = {k: st.get(k) for k in ("running", "connected", "busy", "mode", "model", "recovering",
                                               "recoveries", "connects")}
        out["brain"]["last_error"] = (st.get("last_error") or "")[:160]
    except Exception as exc:
        out["brain"] = {"running": False, "error": type(exc).__name__}
    try:
        from assistant import claude_code

        out["mcp_servers"] = sorted(claude_code.user_mcp_servers())
    except Exception:
        out["mcp_servers"] = []
    try:
        from assistant import hud

        out["ui"] = hud.ui_state()
    except Exception:
        pass
    return out


@router.get("/api/diagnostics", include_in_schema=False)
def api_diagnostics(request: Request) -> Response:
    d = _d()
    resp, _ = d._guard(request, api=True)
    if resp is not None:
        return resp
    return d._json(diagnostics())


def db_log(message: str, exc: Exception) -> None:
    import db

    log.warning("%s: %s", message, exc)
    db.log_event("voice", message, level="error", data={"error": repr(exc)[:300]})


# ---------------------------------------------------------------------------
# child process for `main.py schedule`
# ---------------------------------------------------------------------------

_child: subprocess.Popen[bytes] | None = None


def start_child(port: int | None = None) -> bool:
    """Start ``python -m voice`` when VOICE_ENABLED (its own lock stops a second copy). ``port``: where
    this server listens, so the voice process talks to the same warm brain."""
    global _child
    from voice.settings import get_voice_settings

    s = get_voice_settings()
    if not s.enabled:
        return False
    s.log_path.parent.mkdir(parents=True, exist_ok=True)
    out = open(s.log_path.with_name("voice-console.log"), "ab")
    try:
        import os

        env = {**os.environ, **({"CLIPAGENT_BRAIN_URL": f"http://127.0.0.1:{port}"} if port else {})}
        _child = subprocess.Popen([sys.executable, "-m", "voice"], cwd=str(Path(__file__).resolve().parent.parent),
                                  stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env)
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
