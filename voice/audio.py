"""Microphone and speaker plumbing: device choice, frames, levels, the energy gate, playback.

Everything that touches PortAudio (``sounddevice``) is imported inside functions, so tests and
the web server can import this module without audio hardware or the library installed.

Audio inside the voice loop is always 16 kHz mono int16 in frames of :data:`FRAME` samples
(80 ms: what openWakeWord wants per call).
"""

from __future__ import annotations

import logging
import queue
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import numpy as np

log = logging.getLogger(__name__)

RATE = 16000
FRAME = 1280                     # 80 ms
FRAME_SEC = FRAME / RATE
STALL_SEC = 3.0                  # no audio this long from an open stream = the device went away


class DeviceLost(RuntimeError):
    """The microphone stopped delivering audio (unplugged, disabled, taken by another app)."""


class AudioUnavailable(RuntimeError):
    """sounddevice/PortAudio is not installed or found no microphone."""


@dataclass(frozen=True)
class Device:
    index: int
    name: str
    hostapi: str = ""
    channels: int = 1
    default_rate: float = 16000.0
    is_default: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "name": self.name, "hostapi": self.hostapi, "channels": self.channels,
                "default_rate": self.default_rate, "is_default": self.is_default}


# Inputs that are not microphones: loopback / "what you hear" captures and Windows' mapper aliases.
_NOT_A_MIC = re.compile(r"stereo mix|what u hear|wave out|loopback|monitor of|sound mapper|primary sound capture|"
                        r"cable output|voicemeeter", re.IGNORECASE)
_GOOD = re.compile(r"headset|headphone|usb|yeti|blue|rode|shure|hyperx|razer|logitech|jabra|wireless|airpods|"
                   r"buds|hands-free|microphone \(", re.IGNORECASE)
_HOSTAPI_RANK = {"Windows WASAPI": 1, "MME": 3, "Windows DirectSound": 2, "Windows WDM-KS": 0,
                 "ALSA": 2, "PulseAudio": 3, "Core Audio": 3}


def _base_name(name: str) -> str:
    """MME truncates names to 31 characters; compare devices across host APIs by that prefix."""
    return name.strip().lower()[:31]


def choose_device(devices: Iterable[Device], preferred: str = "") -> Device | None:
    """Pick the microphone: the one named in Settings (substring match), else the system default,
    else the most microphone-like input (headset/USB first). Loopback captures are never chosen."""
    mics = [d for d in devices if d.channels > 0 and not _NOT_A_MIC.search(d.name)]
    if not mics:
        return None
    if preferred.strip():
        want = preferred.strip().lower()
        hits = [d for d in mics if want in d.name.lower() or _base_name(d.name) in want]
        if hits:
            return max(hits, key=lambda d: (d.is_default, _HOSTAPI_RANK.get(d.hostapi, 1)))
    defaults = [d for d in mics if d.is_default]
    if defaults:
        return defaults[0]
    return max(mics, key=lambda d: (bool(_GOOD.search(d.name)), _HOSTAPI_RANK.get(d.hostapi, 1), -d.index))


def _siblings(device: Device) -> list[Device]:
    """The same microphone exposed through other host APIs, best first (shared-mode WASAPI, DirectSound...)."""
    try:
        devices = list_input_devices()
    except Exception:  # pragma: no cover - hardware only
        return []
    base = _base_name(device.name)
    same = [d for d in devices if d.index != device.index and _base_name(d.name) == base]
    order = {"Windows WASAPI": 0, "Windows DirectSound": 1, "MME": 2, "Windows WDM-KS": 3}
    return sorted(same, key=lambda d: order.get(d.hostapi, 4))


def list_input_devices() -> list[Device]:
    """Inputs as PortAudio sees them now (re-initialises PortAudio so a newly plugged device appears)."""
    sd = _sd()
    try:
        sd._terminate()
        sd._initialize()
    except Exception:  # pragma: no cover - only matters on real hardware
        pass
    try:
        hostapis = sd.query_hostapis()
        raw = sd.query_devices()
        default_in = sd.default.device[0] if isinstance(sd.default.device, (list, tuple)) else sd.default.device
    except Exception as exc:
        raise AudioUnavailable(f"could not list audio devices: {exc}") from exc
    out = []
    for i, d in enumerate(raw):
        if int(d.get("max_input_channels", 0)) <= 0:
            continue
        api = hostapis[d["hostapi"]]["name"] if d.get("hostapi") is not None else ""
        api_default = hostapis[d["hostapi"]].get("default_input_device") == i if d.get("hostapi") is not None else False
        out.append(Device(i, str(d["name"]), api, int(d["max_input_channels"]), float(d.get("default_samplerate") or RATE),
                          is_default=(i == default_in) or (api_default and default_in in (None, -1))))
    return out


def _sd() -> Any:
    try:
        import sounddevice as sd  # heavy optional import (PortAudio)
    except Exception as exc:  # ImportError or OSError (PortAudio library missing)
        raise AudioUnavailable(f"sounddevice is not available: {exc}") from exc
    return sd


# ---------------------------------------------------------------------------
# signal helpers
# ---------------------------------------------------------------------------

def rms(frame: np.ndarray) -> float:
    """Root-mean-square level in 0..1 (int16 or float input)."""
    if frame.size == 0:
        return 0.0
    x = frame.astype(np.float32)
    if frame.dtype == np.int16:
        x /= 32768.0
    return float(np.sqrt(np.mean(x * x)))


def to_float(frames: np.ndarray) -> np.ndarray:
    return (frames.astype(np.float32) / 32768.0) if frames.dtype == np.int16 else frames.astype(np.float32)


def to_int16(x: np.ndarray) -> np.ndarray:
    return np.clip(x * 32767.0, -32768, 32767).astype(np.int16)


def resample(x: np.ndarray, src_rate: float, dst_rate: float = RATE) -> np.ndarray:
    """Linear-interpolation resample of a mono float signal (fine for speech and wake words)."""
    if src_rate == dst_rate or x.size == 0:
        return x
    n = max(1, int(round(x.size * dst_rate / src_rate)))
    return np.interp(np.linspace(0, x.size - 1, n), np.arange(x.size), x).astype(np.float32)


def tone(freq: float = 880.0, sec: float = 0.12, rate: int = 24000, volume: float = 0.18) -> np.ndarray:
    """A short soft chime (wake acknowledgement / mute), faded in and out so it does not click."""
    t = np.arange(int(rate * sec)) / rate
    env = np.minimum(1.0, np.minimum(t, sec - t) / 0.02)
    return (volume * env * np.sin(2 * np.pi * freq * t)).astype(np.float32)


class EnergyGate:
    """Speech/no-speech from loudness over an adaptive noise floor, with hysteresis.

    Adapted from adewaskar/jarvis ``src/lib/vad.ts`` (MIT License, Copyright (c) 2026 Aditya
    Dewaskar): trigger at 2.6x the room's noise floor, 2.4x more while the assistant is talking
    (residual echo), release at 0.6x the trigger, and only adapt the floor when not in speech.
    Used on its own when webrtcvad is missing, and as an extra guard for barge-in.
    """

    TRIGGER_OVER_FLOOR = 2.6
    GUARD_BOOST = 2.4
    RELEASE_RATIO = 0.6
    FLOOR_MIN = 0.0015

    def __init__(self) -> None:
        self.floor = 0.004
        self.smooth = 0.0
        self.active = False
        self.guard = False

    @property
    def threshold(self) -> float:
        return self.floor * self.TRIGGER_OVER_FLOOR * (self.GUARD_BOOST if self.guard else 1.0)

    def loud(self, frame: np.ndarray) -> bool:
        e = rms(frame)
        self.smooth += (e - self.smooth) * 0.5
        if not self.active:
            rate = 0.02 if self.smooth > self.floor else 0.1
            self.floor = max(self.FLOOR_MIN, self.floor + (self.smooth - self.floor) * rate)
        th = self.threshold
        self.active = self.smooth > (th * self.RELEASE_RATIO if self.active else th)
        return self.active


# ---------------------------------------------------------------------------
# microphone
# ---------------------------------------------------------------------------

class MicSource:
    """An open microphone delivering :data:`FRAME`-sample int16 frames at 16 kHz."""

    def __init__(self, device: Device, *, clock: Callable[[], float] = time.monotonic):
        self.device = device
        self.clock = clock
        self._q: queue.Queue[np.ndarray] = queue.Queue(maxsize=400)
        self._buf = np.zeros(0, dtype=np.float32)
        self._stream: Any = None
        self._rate = float(RATE)
        self._last = clock()

    def open(self) -> "MicSource":
        sd = _sd()

        def cb(indata: np.ndarray, frames: int, t: Any, status: Any) -> None:
            try:
                self._q.put_nowait(indata[:, 0].copy())
            except queue.Full:  # the loop is busy (synthesising speech); drop the oldest audio
                try:
                    self._q.get_nowait()
                    self._q.put_nowait(indata[:, 0].copy())
                except queue.Empty:
                    pass

        last_exc: Exception | None = None
        # The same physical mic appears once per Windows audio API (MME, DirectSound, WASAPI). One of them
        # can refuse to open (e.g. MME "Unanticipated host error" while another app holds the device), so
        # fall back to the other APIs for the same mic before giving up.
        for dev in [self.device, *_siblings(self.device)]:
            for rate in (RATE, dev.default_rate, 48000.0, 44100.0):
                try:
                    self._stream = sd.InputStream(device=dev.index, channels=1, samplerate=rate, dtype="float32",
                                                  blocksize=int(rate * FRAME_SEC), callback=cb)
                    self._stream.start()
                    self._rate = float(rate)
                    break
                except Exception as exc:
                    last_exc = exc
                    self._stream = None
            if self._stream is not None:
                if dev is not self.device:
                    log.warning("microphone %r failed on %s; using %s instead", self.device.name,
                                self.device.hostapi, dev.hostapi)
                    self.device = dev
                break
        if self._stream is None:
            raise DeviceLost(f"could not open {self.device.name!r}: {last_exc}")
        self._last = self.clock()
        log.info("microphone open: %s (%s, %.0f Hz)", self.device.name, self.device.hostapi, self._rate)
        return self

    def read(self, timeout: float = 0.5) -> np.ndarray | None:
        """Next frame, or None when nothing arrived within ``timeout``. Raises :class:`DeviceLost`."""
        while self._buf.size < FRAME:
            try:
                chunk = self._q.get(timeout=timeout)
            except queue.Empty:
                stream = self._stream
                if stream is None or not getattr(stream, "active", True) or self.clock() - self._last > STALL_SEC:
                    raise DeviceLost(f"no audio from {self.device.name!r}") from None
                return None
            self._last = self.clock()
            self._buf = np.concatenate([self._buf, resample(chunk, self._rate)])
        frame, self._buf = self._buf[:FRAME], self._buf[FRAME:]
        return to_int16(frame)

    def close(self) -> None:
        s, self._stream = self._stream, None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception:  # pragma: no cover
                pass


def measure_level(device_name: str = "", seconds: float = 0.3) -> dict[str, Any]:
    """One short reading for the Settings page's "test my mic" meter."""
    dev = choose_device(list_input_devices(), device_name)
    if dev is None:
        raise AudioUnavailable("no microphone found")
    mic = MicSource(dev).open()
    try:
        levels = []
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            f = mic.read(timeout=0.5)
            if f is not None:
                levels.append(rms(f))
    finally:
        mic.close()
    peak = max(levels, default=0.0)
    return {"device": dev.name, "level": round(min(1.0, peak * 8), 3), "rms": round(peak, 5)}


# ---------------------------------------------------------------------------
# speaker
# ---------------------------------------------------------------------------

class SoundDevicePlayer:
    """Non-blocking playback that can be cut off mid-sentence (barge-in)."""

    def __init__(self, device: str = "", *, clock: Callable[[], float] = time.monotonic):
        self.device = device or None
        self.clock = clock
        self._end = 0.0

    def play(self, samples: np.ndarray, rate: int) -> None:
        sd = _sd()
        sd.stop()
        dev = None
        if self.device:
            for i, d in enumerate(sd.query_devices()):
                if d.get("max_output_channels", 0) > 0 and self.device.lower() in str(d["name"]).lower():
                    dev = i
                    break
        sd.play(samples.astype(np.float32), int(rate), device=dev)
        self._end = self.clock() + samples.size / float(rate) + 0.05

    def busy(self) -> bool:
        return self.clock() < self._end

    def stop(self) -> None:
        self._end = 0.0
        try:
            _sd().stop()
        except AudioUnavailable:
            pass
