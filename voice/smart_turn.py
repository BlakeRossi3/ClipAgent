"""Semantic end-of-turn: Pipecat **Smart Turn v3** (audio in, "has he finished?" out).

Model: ``pipecat-ai/smart-turn-v3`` on Hugging Face, file ``smart-turn-v3.2-cpu.onnx`` (int8, about 8 MB),
BSD 2-Clause licence (Copyright (c) 2024-2025, Daily), weights, data and training code open
(https://github.com/pipecat-ai/smart-turn). Whisper-Tiny encoder + a linear head; 12-65 ms on a CPU.

Input format (from the repo's ``inference.py`` / ``audio_utils.py``): 16 kHz mono float32, the LAST 8 s of
the current turn (longer audio is cut from the front; shorter audio is zero-padded at the FRONT), then
Whisper's log-mel features (``WhisperFeatureExtractor(chunk_length=8)``, ``do_normalize=True``): 80 mel
bins x 800 frames, tensor ``input_features`` of shape (1, 80, 800). Output: one sigmoid probability that
the turn is complete; > 0.5 = complete. The features are computed here in numpy (no transformers/torch).

How it is used (both the laptop voice loop and the Jarvis page's server speech-to-text path):
VAD sees a short silence (about 300 ms) -> :meth:`SmartTurn.predict` on the turn's audio ->
:func:`end_wait_ms` turns that into "end now" (complete) or "wait longer, up to a cap" (not finished).
No model (``VOICE_SMART_TURN=off``, not downloaded yet, onnxruntime missing) -> ``None`` and the
silence timer decides alone, exactly as before.

The model downloads on first run into ``<data>/models/smart_turn/`` with progress (:func:`ensure_model`,
also part of ``voice.models.ensure_models``). Importing this module loads nothing.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np

log = logging.getLogger(__name__)

MODEL_FILE = "smart-turn-v3.2-cpu.onnx"
MODEL_URL = "https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/main/" + MODEL_FILE
MODEL_MB = 8
RATE = 16000
SECONDS = 8
N_SAMPLES = RATE * SECONDS
N_FFT, HOP, N_MELS = 400, 160, 80
N_FRAMES = N_SAMPLES // HOP          # 800
THRESHOLD = 0.5                      # the model card's cut-off: above = he has finished

# what the probability does to the wait after his last word (milliseconds of silence)
EARLY_MS = 300                       # "finished": end the turn after this much silence
EXTEND_MS = 2000                     # "not finished": wait up to this long for the rest of the sentence


def model_path(models_dir: Path) -> Path:
    return Path(models_dir) / "smart_turn" / MODEL_FILE


# ---------------------------------------------------------------------------
# features: Whisper log-mel, numpy only (matches transformers' WhisperFeatureExtractor)
# ---------------------------------------------------------------------------

def _hz_to_mel(f: np.ndarray) -> np.ndarray:
    """Slaney mel scale (librosa / transformers ``mel_scale="slaney"``)."""
    f = np.asarray(f, dtype=np.float64)
    min_log_hz, min_log_mel, logstep = 1000.0, 15.0, np.log(6.4) / 27.0
    mel = 3.0 * f / 200.0
    return np.where(f >= min_log_hz, min_log_mel + np.log(np.maximum(f, 1e-10) / min_log_hz) / logstep, mel)


def _mel_to_hz(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float64)
    min_log_hz, min_log_mel, logstep = 1000.0, 15.0, np.log(6.4) / 27.0
    hz = 200.0 * m / 3.0
    return np.where(m >= min_log_mel, min_log_hz * np.exp(logstep * (m - min_log_mel)), hz)


_FILTERS: np.ndarray | None = None
_WINDOW: np.ndarray | None = None


def mel_filters() -> np.ndarray:
    """(201, 80) triangular filters, slaney-normalised (Whisper's filterbank)."""
    global _FILTERS
    if _FILTERS is None:
        n_freq = N_FFT // 2 + 1
        fft_freqs = np.linspace(0, RATE // 2, n_freq)
        mel_pts = np.linspace(_hz_to_mel(np.array(0.0)), _hz_to_mel(np.array(RATE / 2)), N_MELS + 2)
        filt_freqs = _mel_to_hz(mel_pts)
        diff = np.diff(filt_freqs)
        slopes = filt_freqs[None, :] - fft_freqs[:, None]
        down = -slopes[:, :-2] / diff[:-1]
        up = slopes[:, 2:] / diff[1:]
        fb = np.maximum(0.0, np.minimum(down, up))
        fb *= (2.0 / (filt_freqs[2:N_MELS + 2] - filt_freqs[:N_MELS]))[None, :]
        _FILTERS = fb
    return _FILTERS


def _window() -> np.ndarray:
    global _WINDOW
    if _WINDOW is None:   # periodic Hann, as transformers' window_function(400, "hann")
        _WINDOW = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(N_FFT) / N_FFT)
    return _WINDOW


def prepare(audio: np.ndarray) -> np.ndarray:
    """Any length of 16 kHz audio (int16 or float) -> exactly the last 8 s, zero-padded at the front."""
    x = np.asarray(audio)
    if x.dtype == np.int16:
        x = x.astype(np.float32) / 32768.0
    x = x.astype(np.float32).reshape(-1)
    if x.size > N_SAMPLES:
        return x[-N_SAMPLES:]
    if x.size < N_SAMPLES:
        return np.pad(x, (N_SAMPLES - x.size, 0))
    return x


def features(audio: np.ndarray) -> np.ndarray:
    """-> float32 (1, 80, 800): normalise (zero mean, unit variance over the 8 s), STFT (centre, reflect
    padding), power, mel, log10 (floor 1e-10), clamp to max-8, (x+4)/4; the last frame is dropped."""
    x = prepare(audio).astype(np.float64)
    x = (x - x.mean()) / np.sqrt(x.var() + 1e-7)
    padded = np.pad(x, (N_FFT // 2, N_FFT // 2), mode="reflect")
    n = 1 + (padded.size - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n)[:, None]
    spec = np.abs(np.fft.rfft(padded[idx] * _window()[None, :], axis=1)) ** 2   # (frames, 201)
    mel = spec @ mel_filters()                                                  # (frames, 80)
    log_spec = np.log10(np.maximum(mel, 1e-10)).T[:, :-1]                       # (80, 800)
    log_spec = np.maximum(log_spec, log_spec.max() - 8.0)
    log_spec = (log_spec + 4.0) / 4.0
    return log_spec[None, :, :N_FRAMES].astype(np.float32)


# ---------------------------------------------------------------------------
# the model
# ---------------------------------------------------------------------------

class SmartTurn:
    """The ONNX model (onnxruntime, one thread, sequential, as the reference ``build_session``)."""

    name = "smart-turn-v3.2"

    def __init__(self, path: Path):
        import onnxruntime as ort   # ships with faster-whisper (Silero VAD), so it is already installed

        so = ort.SessionOptions()
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        so.inter_op_num_threads = 1
        so.intra_op_num_threads = 2
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(str(path), sess_options=so, providers=["CPUExecutionProvider"])
        self.input_name = self.session.get_inputs()[0].name
        self.lock = threading.Lock()

    def predict(self, audio: np.ndarray) -> float:
        feats = features(audio)
        with self.lock:
            out = self.session.run(None, {self.input_name: feats})
        return float(np.asarray(out[0]).reshape(-1)[0])


_detector: Any = None
_tried: Path | None = None
_override: Any = None
_lock = threading.Lock()
_download: threading.Thread | None = None
_downloads = True


def set_downloads(enabled: bool) -> None:
    """Tests: never start a background download."""
    global _downloads
    _downloads = enabled


def set_detector(det: Any) -> None:
    """Tests: an object with ``predict(audio) -> float``; ``None`` = back to the real model."""
    global _override, _detector, _tried
    _override = det
    _detector, _tried = None, None


def enabled() -> bool:
    from voice.settings import get_voice_settings

    return get_voice_settings().smart_turn != "off"


def get_detector(*, download: bool = False) -> Any:
    """The loaded model, or None (off, not downloaded, onnxruntime missing or the file is bad).
    ``download``: start a background download when the file is missing (the server's first use)."""
    global _detector, _tried
    if not enabled():
        return None
    if _override is not None:
        return _override
    from voice.settings import get_voice_settings

    path = model_path(get_voice_settings().models_dir)
    with _lock:
        if _detector is not None and _tried == path:
            return _detector
        if not path.is_file():
            if download and _downloads:
                _download_in_background(path.parent.parent)
            return None
        if _tried == path:      # a load already failed for this file
            return None
        _tried = path
        try:
            _detector = SmartTurn(path)
            log.info("smart turn model loaded (%s)", path.name)
        except Exception as exc:
            log.warning("smart turn model not usable (%s); the silence timer decides", exc)
            _detector = None
        return _detector


def ensure_model(models_dir: Path, say: Callable[[str], None] | None = None) -> list[str]:
    """Download the model if it is missing (progress lines through ``say``). Returns problems."""
    from voice import models

    say = say or (lambda m: log.info("%s", m))
    dest = model_path(models_dir)
    if dest.is_file():
        return []
    say(f"Downloading the end-of-turn model into {dest.parent} (one time, about {MODEL_MB} MB)...")
    try:
        models.download(MODEL_URL, dest, say)
    except Exception as exc:
        say(f"  could not download {MODEL_FILE}: {exc}")
        return [f"{MODEL_FILE}: {exc}"]
    say("End-of-turn model ready.")
    return []


def _download_in_background(models_dir: Path) -> None:
    global _download
    if _download is not None and _download.is_alive():
        return
    _download = threading.Thread(target=ensure_model, args=(models_dir,), name="smart-turn-download", daemon=True)
    _download.start()


def predict(audio: np.ndarray, *, download: bool = False) -> tuple[float | None, int]:
    """-> (probability the turn is complete or None, milliseconds it took). Never raises."""
    det = get_detector(download=download)
    if det is None:
        return None, 0
    t0 = time.monotonic()
    try:
        p = float(det.predict(audio))
    except Exception as exc:
        log.warning("smart turn failed: %s", exc)
        return None, round((time.monotonic() - t0) * 1000)
    return max(0.0, min(1.0, p)), round((time.monotonic() - t0) * 1000)


def end_wait_ms(prob: float | None, base_ms: float, *, early_ms: float = EARLY_MS, cap_ms: float = EXTEND_MS) -> float:
    """Silence (ms) to wait after his last word: the timer alone without a model; ``early_ms`` when the
    model says he has finished; when it says he has not, longer (up to ``cap_ms``, never shorter than
    the timer). Uncertain middles scale between the two."""
    if prob is None:
        return base_ms
    if prob >= THRESHOLD:
        return min(base_ms, early_ms)
    # 0.0 -> the cap; 0.5 -> the plain timer
    return max(base_ms, base_ms + (cap_ms - base_ms) * (THRESHOLD - prob) / THRESHOLD)
