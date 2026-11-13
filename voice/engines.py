"""The speech engines (all local and free by default) and their fallbacks.

=========  ===========================================  ==========================================
Layer      Default                                      Fallback
=========  ===========================================  ==========================================
Wake word  openWakeWord ``hey_jarvis`` (ONNX, CPU)       the name spotter: ``tiny.en`` Whisper on each
                                                        short burst of speech, looking for "Sterling"
Speech     webrtcvad (20 ms frames) + the energy gate   the energy gate alone
Words      faster-whisper ``small.en`` (GPU when it     the same on the CPU (int8)
           works, as transcribe.py)
Voice      Kokoro-82M (kokoro-onnx, ``bm_george``)      Windows SAPI voices (pywin32), optional
                                                        ElevenLabs when a key is set
=========  ===========================================  ==========================================

Every engine imports its library inside ``__init__``/first use, never at module import. Model
files go to ``<data>/models`` (see :mod:`voice.models`). A text-to-speech engine that fails is
latched off and the next one in the chain says the sentence instead (idea from adewaskar/jarvis
``src/lib/tts.ts``, MIT).
"""

from __future__ import annotations

import logging
import tempfile
import wave
from pathlib import Path
from typing import Any, Protocol, Sequence

import numpy as np

from voice import audio
from voice.settings import VoiceSettings

log = logging.getLogger(__name__)


class WakeEngine(Protocol):
    def process(self, frame: np.ndarray) -> str | None: ...
    def reset(self) -> None: ...


class VAD(Protocol):
    def is_speech(self, frame: np.ndarray) -> bool: ...


class STT(Protocol):
    def transcribe(self, samples: np.ndarray) -> str: ...


class TTS(Protocol):
    name: str
    def synthesize(self, text: str) -> tuple[np.ndarray, int]: ...


class Player(Protocol):
    def play(self, samples: np.ndarray, rate: int) -> None: ...
    def busy(self) -> bool: ...
    def stop(self) -> None: ...


# ---------------------------------------------------------------------------
# wake word
# ---------------------------------------------------------------------------

class OpenWakeWord:
    """openWakeWord with the ONNX runtime (the default TFLite runtime has no Windows wheels).

    Loads ``hey_jarvis_v0.1.onnx`` plus any custom ``*.onnx`` in ``<models>/wakewords/`` (e.g. a
    trained ``hey_sterling.onnx``; see docs/VOICE.md).
    """

    def __init__(self, models_dir: Path, *, threshold: float = 0.5, builtin: Sequence[str] = ("hey_jarvis",)):
        from openwakeword.model import Model  # heavy optional import

        base = models_dir / "openwakeword"
        paths = [str(base / f"{n}_v0.1.onnx") for n in builtin]
        custom = models_dir / "wakewords"
        if custom.is_dir():
            paths += [str(p) for p in sorted(custom.glob("*.onnx"))]
        self.threshold = threshold
        self.model = Model(wakeword_models=paths, inference_framework="onnx",
                           melspec_model_path=str(base / "melspectrogram.onnx"),
                           embedding_model_path=str(base / "embedding_model.onnx"))
        self.names = [Path(p).stem for p in paths]

    def process(self, frame: np.ndarray) -> str | None:
        scores = self.model.predict(frame)
        for name, score in scores.items():
            if score >= self.threshold:
                self.model.reset()
                return str(name)
        return None

    def reset(self) -> None:
        self.model.reset()


class NoWake:
    """No wake-word model (name spotting only)."""

    def process(self, frame: np.ndarray) -> str | None:
        return None

    def reset(self) -> None:
        pass


# ---------------------------------------------------------------------------
# voice activity
# ---------------------------------------------------------------------------

class WebRtcVad:
    def __init__(self, aggressiveness: int = 2):
        import webrtcvad  # webrtcvad-wheels

        self.vad = webrtcvad.Vad(aggressiveness)
        self.gate = audio.EnergyGate()

    def is_speech(self, frame: np.ndarray) -> bool:
        loud = self.gate.loud(frame)
        pcm = frame.astype(np.int16).tobytes()
        step = 320 * 2  # 20 ms of 16-bit samples
        votes = sum(self.vad.is_speech(pcm[i:i + step], audio.RATE) for i in range(0, len(pcm) - step + 1, step))
        return votes >= 2 and (loud or audio.rms(frame) > 0.02)


class EnergyVad:
    def __init__(self) -> None:
        self.gate = audio.EnergyGate()

    def is_speech(self, frame: np.ndarray) -> bool:
        return self.gate.loud(frame)


# ---------------------------------------------------------------------------
# speech to text
# ---------------------------------------------------------------------------

class WhisperSTT:
    """faster-whisper on a short utterance. GPU first when allowed, CPU int8 if CUDA is missing."""

    def __init__(self, model: str = "small.en", *, device: str = "auto", prompt: str = ""):
        self.model_name, self.device, self.prompt = model, device, prompt
        self.compute_type = "int8" if device == "cpu" else "default"
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            from faster_whisper import WhisperModel  # heavy optional import

            log.info("loading faster-whisper %s (device=%s); the first run downloads it", self.model_name, self.device)
            self._model = WhisperModel(self.model_name, device=self.device, compute_type=self.compute_type)
        return self._model

    def transcribe(self, samples: np.ndarray) -> str:
        try:
            return self._run(samples)
        except (RuntimeError, ValueError) as exc:
            if self.device == "cpu":
                raise
            log.warning("GPU speech recognition failed (%s); using the CPU", exc)
            self.device, self.compute_type, self._model = "cpu", "int8", None
            return self._run(samples)

    def _run(self, samples: np.ndarray) -> str:
        model = self._load()
        segments, _info = model.transcribe(audio.to_float(samples), language="en", beam_size=1,
                                           condition_on_previous_text=False, without_timestamps=True,
                                           initial_prompt=self.prompt or None)
        return " ".join(s.text.strip() for s in segments).strip()


# ---------------------------------------------------------------------------
# text to speech
# ---------------------------------------------------------------------------

class KokoroTTS:
    name = "kokoro"

    def __init__(self, models_dir: Path, voice: str = "bm_george", speed: float = 1.0):
        from kokoro_onnx import Kokoro  # heavy optional import

        d = models_dir / "kokoro"
        self.k = Kokoro(str(d / "kokoro-v1.0.int8.onnx"), str(d / "voices-v1.0.bin"))
        self.voice, self.speed = voice, speed
        self.lang = "en-gb" if voice.startswith("b") else "en-us"

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        samples, rate = self.k.create(text, voice=self.voice, speed=self.speed, lang=self.lang)
        return np.asarray(samples, dtype=np.float32), int(rate)


class SapiTTS:
    """Windows' built-in voices through SAPI (pywin32). Prefers a British male voice (George)."""

    name = "sapi"
    PREFER = ("george", "ryan", "thomas", "daniel", "david", "mark", "guy")

    def __init__(self, voice_hint: str = ""):
        import win32com.client  # pywin32, Windows only

        self._w = win32com.client
        self.voice = win32com.client.Dispatch("SAPI.SpVoice")
        voices = list(self.voice.GetVoices())
        names = [v.GetDescription() for v in voices]
        pick = None
        for hint in ([voice_hint] if voice_hint else []) + list(self.PREFER):
            pick = next((v for v, n in zip(voices, names) if hint.lower() in n.lower()), None)
            if pick is not None:
                break
        if pick is not None:
            self.voice.Voice = pick
        self.voice_name = pick.GetDescription() if pick is not None else (names[0] if names else "default")

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "say.wav")
            stream = self._w.Dispatch("SAPI.SpFileStream")
            fmt = self._w.Dispatch("SAPI.SpAudioFormat")
            fmt.Type = 22  # SAFT22kHz16BitMono
            stream.Format = fmt
            stream.Open(path, 3)  # SSFMCreateForWrite
            self.voice.AudioOutputStream = stream
            self.voice.Speak(text)
            stream.Close()
            return read_wav(path)


class ElevenLabsTTS:
    """Optional paid voice (only when ELEVENLABS_API_KEY is set). Raw PCM at 22.05 kHz."""

    name = "elevenlabs"

    def __init__(self, api_key: str, voice_id: str):
        if not api_key:
            raise ValueError("no ElevenLabs API key")
        self.api_key, self.voice_id = api_key, voice_id

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        import httpx

        r = httpx.post(f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}",
                       params={"output_format": "pcm_22050"},
                       headers={"xi-api-key": self.api_key, "accept": "audio/pcm"},
                       json={"text": text, "model_id": "eleven_flash_v2_5"}, timeout=20)
        r.raise_for_status()
        return audio.to_float(np.frombuffer(r.content, dtype=np.int16)), 22050


def read_wav(path: str | Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        rate, n, width, ch = w.getframerate(), w.getnframes(), w.getsampwidth(), w.getnchannels()
        data = np.frombuffer(w.readframes(n), dtype=np.int16 if width == 2 else np.uint8)
    if ch > 1:
        data = data.reshape(-1, ch)[:, 0]
    return audio.to_float(data.astype(np.int16)), rate


class ChainTTS:
    """Try each engine in order; one that fails is latched off for the rest of the session."""

    def __init__(self, engines: Sequence[TTS]):
        if not engines:
            raise ValueError("no text-to-speech engine")
        self.engines = list(engines)
        self.dead: set[str] = set()

    @property
    def name(self) -> str:
        return next((e.name for e in self.engines if e.name not in self.dead), "none")

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        last: Exception | None = None
        for e in self.engines:
            if e.name in self.dead:
                continue
            try:
                return e.synthesize(text)
            except Exception as exc:
                log.warning("voice %s failed (%s); switching to the next one", e.name, exc)
                self.dead.add(e.name)
                last = exc
        raise RuntimeError(f"every voice failed: {last}")


# ---------------------------------------------------------------------------
# build from settings
# ---------------------------------------------------------------------------

def _try(label: str, fn: Any, problems: list[str]) -> Any:
    try:
        return fn()
    except Exception as exc:
        problems.append(f"{label}: {type(exc).__name__}: {exc}")
        log.warning("%s unavailable: %s", label, exc)
        return None


def build_tts(s: VoiceSettings, problems: list[str]) -> ChainTTS:
    order = {"elevenlabs": ["elevenlabs", "kokoro", "sapi"], "sapi": ["sapi", "kokoro"]}.get(s.tts_engine,
                                                                                         ["kokoro", "sapi"])
    makers = {"kokoro": lambda: KokoroTTS(s.models_dir, s.tts_voice, s.tts_speed),
              "sapi": lambda: SapiTTS(s.sapi_voice),
              "elevenlabs": lambda: ElevenLabsTTS(s.elevenlabs_api_key, s.elevenlabs_voice_id)}
    engines = [e for e in (_try(f"voice {n}", makers[n], problems) for n in order) if e is not None]
    return ChainTTS(engines)


def build_engines(s: VoiceSettings) -> tuple[dict[str, Any], list[str]]:
    """-> ({"wake", "vad", "stt", "name_stt", "tts", "player"}, problems). Raises when the
    loop cannot work at all (no speech recognition or no voice)."""
    problems: list[str] = []
    wake = None
    if s.wake_mode in ("both", "hey_jarvis"):
        wake = _try("wake word (openWakeWord)", lambda: OpenWakeWord(s.models_dir, threshold=s.wake_threshold),
                    problems)
    vad = _try("webrtcvad", WebRtcVad, problems) or EnergyVad()
    device = "cpu" if s.whisper_device == "cpu" else "auto"
    stt = WhisperSTT(s.stt_model, device=device, prompt=f"{s.assistant_name}.")
    name_stt = None
    if s.wake_mode in ("both", "name") or wake is None:
        name_stt = WhisperSTT(s.name_model, device="cpu", prompt=f"{s.assistant_name}.")
    tts = build_tts(s, problems)
    return {"wake": wake or NoWake(), "vad": vad, "stt": stt, "name_stt": name_stt, "tts": tts,
            "player": audio.SoundDevicePlayer(s.output_device)}, problems
