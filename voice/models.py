"""First-run model downloads into ``<data>/models`` (never at import).

* openWakeWord feature models + ``hey_jarvis`` (about 4 MB) from the openWakeWord GitHub release.
* Kokoro-82M int8 + voices (about 120 MB) from the kokoro-onnx GitHub release.
* faster-whisper models download themselves into the Hugging Face cache on first use (``small.en``
  is shared with the clip pipeline; ``tiny.en`` for the name spotter is about 75 MB).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

OWW_RELEASE = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"
OWW_FILES = ("melspectrogram.onnx", "embedding_model.onnx", "hey_jarvis_v0.1.onnx")
KOKORO_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
KOKORO_FILES = ("kokoro-v1.0.int8.onnx", "voices-v1.0.bin")

Progress = Callable[[str], None]


def missing(models_dir: Path, *, wake: bool = True, kokoro: bool = True) -> list[Path]:
    out = []
    if wake:
        out += [models_dir / "openwakeword" / f for f in OWW_FILES if not (models_dir / "openwakeword" / f).is_file()]
    if kokoro:
        out += [models_dir / "kokoro" / f for f in KOKORO_FILES if not (models_dir / "kokoro" / f).is_file()]
    return out


def download(url: str, dest: Path, say: Progress) -> None:
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        got, shown = 0, -1
        with open(part, "wb") as fh:
            for chunk in r.iter_bytes(1 << 16):
                fh.write(chunk)
                got += len(chunk)
                pct = int(got * 100 / total) if total else -1
                if pct >= 0 and pct // 10 != shown:
                    shown = pct // 10
                    say(f"  {dest.name}: {pct}% of {total / 1e6:.0f} MB")
    part.replace(dest)


def ensure_models(models_dir: Path, *, wake: bool = True, kokoro: bool = True, say: Progress | None = None) -> list[str]:
    """Download what is missing. Returns problems (empty = all there). Never raises for network trouble."""
    say = say or (lambda m: log.info("%s", m))
    todo = missing(models_dir, wake=wake, kokoro=kokoro)
    if not todo:
        return []
    say(f"Downloading voice models into {models_dir} (one time, about "
        f"{(4 if wake else 0) + (120 if kokoro and any('kokoro' in str(p) for p in todo) else 0)} MB)...")
    problems = []
    for dest in todo:
        url = (OWW_RELEASE if dest.parent.name == "openwakeword" else KOKORO_RELEASE) + dest.name
        try:
            download(url, dest, say)
        except Exception as exc:
            problems.append(f"{dest.name}: {exc}")
            say(f"  could not download {dest.name}: {exc}")
    if not problems:
        say("Voice models ready.")
    return problems
