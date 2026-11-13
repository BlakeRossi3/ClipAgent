"""``/voice/api/*`` for the dashboard: status pill, mute toggle, microphone list, "test my mic" level.

Plus :func:`start_child` / :func:`stop_child`, used by ``main.py schedule`` to run ``python -m voice``
alongside the web server when VOICE_ENABLED=true.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

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
