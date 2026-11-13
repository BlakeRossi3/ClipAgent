"""Speech-to-text with word timestamps (pipeline stage ``fetched -> transcribed``).

The audio track is first extracted with ffmpeg to 16 kHz mono PCM WAV
(``downloads_dir/<id>/audio.wav``): faster-whisper resamples to that anyway, and
decoding once with ffmpeg is more robust to odd containers/codecs than PyAV.
The transcript is written to ``transcripts_dir/<id>.json`` in the shape
documented in CLAUDE.md::

    {"source_id", "media_path", "backend", "language", "duration_sec", "created_at",
     "segments": [{"id", "start", "end", "text",
                   "words": [{"start", "end", "word", "prob"}, ...]}, ...]}

Times are seconds from the start of the media, rounded to 3 dp. The backend is
pluggable (:class:`Backend`); the default is faster-whisper configured from
settings and built lazily on first use. Tests call :func:`set_backend` with a fake
and monkeypatch :func:`extract_audio` when they do not want to run ffmpeg.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

import db
from config import Settings, get_settings

log = logging.getLogger(__name__)

MODULE = "transcribe"
SAMPLE_RATE = 16_000
FFMPEG_TIMEOUT_SEC = 3600
_EPS = 1e-6


class TranscribeError(Exception):
    """Transcription failed (the source is ``failed``) or the source is in the wrong state."""


@runtime_checkable
class Backend(Protocol):
    """Speech-to-text engine.

    ``transcribe`` returns ``{"language": str | None, "segments": [...]}`` where
    each segment has ``start``, ``end``, ``text`` and ``words`` (each word:
    ``start``, ``end``, ``word``, ``prob``; ``probability`` is accepted too).
    It may also return ``duration_sec`` (media duration). Output is normalised
    by :func:`normalize_segments`, so backends need not round or strip.
    """

    name: str

    def transcribe(self, media_path: Path) -> dict: ...


# ---------------------------------------------------------------------------
# faster-whisper backend
# ---------------------------------------------------------------------------

class FasterWhisperBackend:
    """faster-whisper (CTranslate2) with word timestamps and Silero VAD.

    The model is loaded on the first :meth:`transcribe` call, never at construction
    or import time (it may download weights on first use).
    """

    def __init__(
        self,
        model_size: str = "small.en",
        *,
        device: str = "auto",
        compute_type: str = "int8",
        beam_size: int = 5,
        vad_filter: bool = True,
        language: str | None = None,
    ):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.beam_size = beam_size
        self.vad_filter = vad_filter
        # English-only checkpoints (*.en) cannot do language detection.
        self.language = language or ("en" if model_size.endswith(".en") else None)
        self.name = f"faster-whisper:{model_size}"
        self._model: Any = None

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "FasterWhisperBackend":
        s = settings or get_settings()
        return cls(s.whisper_model, device=s.whisper_device, compute_type=s.whisper_compute_type)

    def _load(self) -> Any:
        if self._model is None:
            from faster_whisper import WhisperModel  # heavy optional import

            log.info("loading faster-whisper model %s (device=%s, compute_type=%s)",
                     self.model_size, self.device, self.compute_type)
            self._model = WhisperModel(self.model_size, device=self.device, compute_type=self.compute_type)
        return self._model

    def _fall_back_to_cpu(self, exc: BaseException) -> bool:
        """GPU (``cuda`` / ``auto``) failed - typically missing cuBLAS/cuDNN DLLs on Windows, which only
        shows up when decoding starts. Switch this backend to CPU int8 for good; True if switched."""
        if self.device == "cpu":
            return False
        db.log_event("transcribe", f"GPU transcription failed ({type(exc).__name__}); using the CPU instead",
                     level="warning", data={"device": self.device, "error": str(exc)[:300]})
        self.device, self.compute_type, self._model = "cpu", "int8", None
        return True

    def transcribe(self, media_path: Path) -> dict:
        try:
            return self._transcribe(media_path)
        except (RuntimeError, ValueError) as exc:  # ctranslate2: missing cuBLAS DLL, no CUDA device...
            if not self._fall_back_to_cpu(exc):
                raise
            return self._transcribe(media_path)

    def _transcribe(self, media_path: Path) -> dict:
        model = self._load()
        segments, info = model.transcribe(
            str(media_path),
            language=self.language,
            beam_size=self.beam_size,
            word_timestamps=True,
            vad_filter=self.vad_filter,
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,  # avoids runaway repetition loops on long media
        )
        out_segments: list[dict[str, Any]] = []
        for seg in segments:  # generator: decoding happens while iterating
            out_segments.append({
                "start": seg.start,
                "end": seg.end,
                "text": seg.text,
                "words": [{"start": w.start, "end": w.end, "word": w.word, "prob": w.probability}
                          for w in (seg.words or [])],
            })
        return {
            "language": getattr(info, "language", None),
            "duration_sec": getattr(info, "duration", None),
            "segments": out_segments,
        }


# ---------------------------------------------------------------------------
# backend registry
# ---------------------------------------------------------------------------

_backend: Backend | None = None
_default: tuple[tuple[str, str, str], FasterWhisperBackend] | None = None


def set_backend(backend: Backend | None) -> None:
    """Install a backend for all later calls; ``None`` restores faster-whisper from settings."""
    global _backend
    _backend = backend


def get_backend() -> Backend:
    """The installed backend, else a (cached) faster-whisper backend for the current settings."""
    global _default
    if _backend is not None:
        return _backend
    s = get_settings()
    key = (s.whisper_model, s.whisper_device, s.whisper_compute_type)
    if _default is None or _default[0] != key:
        _default = (key, FasterWhisperBackend.from_settings(s))
    return _default[1]


# ---------------------------------------------------------------------------
# audio extraction
# ---------------------------------------------------------------------------

def build_extract_cmd(media_path: Path, out_path: Path, ffmpeg_bin: str = "ffmpeg") -> list[str]:
    """ffmpeg command: first audio stream -> 16 kHz mono 16-bit PCM WAV."""
    return [
        ffmpeg_bin, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-i", str(media_path),
        "-map", "0:a:0", "-vn", "-sn", "-dn",
        "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le",
        "-f", "wav", str(out_path),
    ]


def extract_audio(media_path: Path, out_path: Path) -> Path:
    """Decode ``media_path``'s audio to ``out_path`` (written atomically). Raises :class:`TranscribeError`."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.stem + ".partial.wav")
    cmd = build_extract_cmd(media_path, tmp, get_settings().ffmpeg_bin)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=FFMPEG_TIMEOUT_SEC)
    except FileNotFoundError as exc:
        raise TranscribeError(f"ffmpeg not found ({cmd[0]!r})") from exc
    except subprocess.TimeoutExpired as exc:
        tmp.unlink(missing_ok=True)
        raise TranscribeError(f"ffmpeg timed out after {FFMPEG_TIMEOUT_SEC}s") from exc
    if proc.returncode != 0 or not tmp.is_file() or tmp.stat().st_size <= 44:  # 44 = bare WAV header
        tmp.unlink(missing_ok=True)
        err = (proc.stderr or "").strip().splitlines()
        detail = err[-1] if err else f"exit code {proc.returncode}"
        if "matches no streams" in (proc.stderr or ""):
            detail = "media has no audio stream"
        raise TranscribeError(f"audio extraction failed: {detail}")
    os.replace(tmp, out_path)
    return out_path


# ---------------------------------------------------------------------------
# transcript shape helpers
# ---------------------------------------------------------------------------

def _r(x: Any) -> float:
    return round(float(x), 3)


def _attr(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def normalize_segments(segments: Iterable[Any]) -> list[dict[str, Any]]:
    """Coerce backend output to the canonical segment/word shape.

    Rounds times to 3 dp, strips words (punctuation stays attached), drops empty
    words/segments, clamps ``end >= start``, sorts by time and renumbers ids.
    """
    out: list[dict[str, Any]] = []
    for seg in segments:
        words: list[dict[str, Any]] = []
        for w in _attr(seg, "words") or []:
            text = str(_attr(w, "word", "") or "").strip()
            ws, we = _attr(w, "start"), _attr(w, "end")
            if not text or ws is None or we is None:
                continue
            prob = _attr(w, "prob", _attr(w, "probability"))
            start = max(0.0, _r(ws))
            words.append({"start": start, "end": max(start, _r(we)), "word": text,
                          "prob": _r(prob) if prob is not None else None})
        words.sort(key=lambda w: (w["start"], w["end"]))
        text = str(_attr(seg, "text", "") or "").strip()
        if not text and words:
            text = " ".join(w["word"] for w in words)
        if not text:
            continue
        ss, se = _attr(seg, "start"), _attr(seg, "end")
        start = _r(ss) if ss is not None else (words[0]["start"] if words else 0.0)
        end = _r(se) if se is not None else (words[-1]["end"] if words else start)
        start = max(0.0, start)
        out.append({"start": start, "end": max(start, end), "text": text, "words": words})
    out.sort(key=lambda s: (s["start"], s["end"]))
    return [{"id": i, **s} for i, s in enumerate(out)]


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def transcript_path_for(source_id: int) -> Path:
    return get_settings().transcripts_dir / f"{source_id}.json"


def load_transcript(path: Path | str) -> dict:
    """Read a transcript JSON file. Raises :class:`TranscribeError` if missing or malformed."""
    p = Path(path)
    try:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise TranscribeError(f"cannot read transcript {p}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("segments"), list):
        raise TranscribeError(f"transcript {p} has no 'segments' list")
    return data


def iter_words(transcript: Mapping[str, Any]) -> list[dict]:
    """All words of a transcript in time order."""
    words = [w for seg in transcript.get("segments", []) for w in seg.get("words", [])]
    return sorted(words, key=lambda w: (w["start"], w["end"]))


def words_between(transcript: Mapping[str, Any], start: float, end: float) -> list[dict]:
    """Words lying fully inside ``[start, end]`` (inclusive, tolerant to float noise), in time order.

    Words straddling either boundary are excluded.
    """
    if end < start:
        return []
    return [w for w in iter_words(transcript)
            if w["start"] >= start - _EPS and w["end"] <= end + _EPS]


# ---------------------------------------------------------------------------
# stage
# ---------------------------------------------------------------------------

def _fail(source_id: int, reason: str, **data: Any) -> TranscribeError:
    with db.transaction() as conn:
        db.set_source_status(source_id, "failed", reason, conn=conn)
        db.log_event(MODULE, f"source {source_id} failed: {reason}", level="error",
                     data={"source_id": source_id, **data}, conn=conn)
    return TranscribeError(f"source {source_id}: {reason}")


def transcribe_source(source_id: int) -> Path:
    """Transcribe a ``fetched`` source; returns the absolute transcript path.

    * Already ``transcribed`` (or later) with the transcript on disk: returns it.
    * Any other non-``fetched`` status: :class:`TranscribeError`, row untouched.
    * Missing video, ffmpeg or backend failure: status ``failed`` + :class:`TranscribeError`.
    * No speech: an empty-``segments`` transcript is still written and the source
      becomes ``transcribed`` (``pick`` then marks it ``done``); a warning is logged.

    An ``audio.wav`` left by an interrupted run is reused.
    """
    src = db.require_source(source_id)
    if src.status in ("transcribed", "picked", "done"):
        if src.transcript_path and Path(src.transcript_path).is_file():
            return Path(src.transcript_path)
        raise TranscribeError(f"source {source_id} is {src.status!r} but its transcript "
                              f"{src.transcript_path!r} is missing")
    if src.status != "fetched":
        raise TranscribeError(f"source {source_id} is {src.status!r}; only fetched sources can be transcribed")

    if not src.video_path or not Path(src.video_path).is_file():
        raise _fail(source_id, f"video file missing: {src.video_path!r}")
    video = Path(src.video_path).resolve()

    audio = video.parent / "audio.wav"
    try:
        if not (audio.is_file() and audio.stat().st_size > 44):
            audio = extract_audio(video, audio)
    except TranscribeError as exc:
        raise _fail(source_id, str(exc), video_path=str(video))

    try:
        backend = get_backend()
        raw = backend.transcribe(audio)
        if not isinstance(raw, Mapping):
            raise TypeError(f"backend returned {type(raw).__name__}, expected dict")
        segments = normalize_segments(raw.get("segments") or [])
    except Exception as exc:  # backend/model errors of any kind (ImportError, RuntimeError, OOM...)
        raise _fail(source_id, f"transcription failed: {type(exc).__name__}: {exc}",
                    backend=getattr(_backend, "name", None))

    duration = raw.get("duration_sec") or raw.get("duration") or src.duration_sec
    if duration is None and segments:
        duration = segments[-1]["end"]
    transcript = {
        "source_id": source_id,
        "media_path": str(video),
        "backend": backend.name,
        "language": raw.get("language"),
        "duration_sec": _r(duration) if duration is not None else None,
        "created_at": db.now_iso(),
        "segments": segments,
    }
    out = transcript_path_for(source_id).resolve()
    try:
        _write_json_atomic(out, transcript)
    except OSError as exc:
        raise _fail(source_id, f"could not write transcript: {exc}")

    n_words = sum(len(s["words"]) for s in segments)
    with db.transaction() as conn:
        db.update_source(source_id, transcript_path=str(out), audio_path=str(audio.resolve()), conn=conn)
        db.set_source_status(source_id, "transcribed", conn=conn)
        if segments:
            db.log_event(MODULE, f"transcribed source {source_id}: {len(segments)} segments, {n_words} words",
                         data={"source_id": source_id, "backend": backend.name, "language": transcript["language"],
                               "segments": len(segments), "words": n_words}, conn=conn)
        else:
            db.log_event(MODULE, f"source {source_id}: no speech detected", level="warning",
                         data={"source_id": source_id, "backend": backend.name}, conn=conn)
    return out
