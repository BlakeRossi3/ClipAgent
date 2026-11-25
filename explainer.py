"""Animated whiteboard explainer Shorts, drawn entirely in code (no footage, no video model).

Prototype from backlog item #37 (Lemo Opuscar). That project's style specs are CC BY-NC, so none of
its text is copied here: this is our own renderer for the same *genre* - a glossy dry-erase board,
handwriting that is written out left to right with a marker nib, red doodles drawn stroke by stroke,
an eraser wipe between scenes, and a slow camera push-in.

A script is a list of scenes::

    {"title": "Why is the sky blue?", "lines": ["..."], "doodle": "circle|underline|arrow|check|none",
     "say": "narration for this scene"}

:func:`plan` turns a script into a timeline (pure, tested), :func:`render_frame` draws one frame with
Pillow, :func:`render` streams raw frames into ffmpeg with a narration track (local Kokoro, else
Windows SAPI; silent when neither is there). Output is a 1080x1920 30 fps MP4 under
``renders_dir/explainers``. Rendering never uploads: Blake reviews the file first.

These Shorts belong on their OWN YouTube channel, not Daily Orbit (backlog #45). :func:`upload`
sends a reviewed file to :data:`EXPLAINER_CHANNEL` (token from ``python youtube_auth.py --channel
lemo``) as private or unlisted only; making it public is his call in YouTube Studio.

    python explainer.py [script.json] [out.mp4]
    python explainer.py upload <file.mp4> "<title>" [description]
"""

from __future__ import annotations

import json
import logging
import math
import random
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from PIL import Image, ImageDraw, ImageFilter, ImageFont

log = logging.getLogger(__name__)

W, H, FPS = 1080, 1920, 30
WRITE_CPS = 22.0          # handwriting speed, characters per second
DOODLE_SEC = 0.9
ERASE_SEC = 0.45
GAP_SEC = 0.15            # pause between written elements
TAIL_SEC = 0.5            # after narration ends, before the eraser
PUSH_IN = 0.05            # camera zooms 5 % over a scene
DOODLES = ("circle", "underline", "arrow", "check", "none")

BOARD = (247, 247, 242)
INK_TITLE = (28, 30, 36)
INK_BODY = (24, 78, 168)
INK_DOODLE = (205, 40, 40)

FONT_DIRS = (Path("C:/Windows/Fonts"), Path.home() / "AppData/Local/Microsoft/Windows/Fonts")
TITLE_FONTS = ("segoeprb.ttf", "Inkfree.ttf", "comicbd.ttf")
BODY_FONTS = ("segoepr.ttf", "Inkfree.ttf", "comic.ttf")

DEMO_SCRIPT: list[dict[str, Any]] = [
    {"title": "Why is the sky blue?", "lines": [], "doodle": "circle",
     "say": "Why is the sky blue? It comes down to how sunlight bounces off the air."},
    {"title": "Sunlight = every colour", "lines": ["red, orange, yellow", "green, blue, violet", "all mixed together"],
     "doodle": "underline", "say": "Sunlight looks white, but it's really every colour of the rainbow mixed together."},
    {"title": "Air scatters short waves", "lines": ["blue light has short waves", "they bounce off air molecules",
                                                    "in every direction"],
     "doodle": "arrow", "say": "Blue light travels in short waves, and short waves bounce off the molecules in the "
                               "air, scattering in every direction."},
    {"title": "Blue scatters ~5x more", "lines": ["than red light does", "so blue reaches your eyes", "from all over the sky"],
     "doodle": "circle", "say": "Blue scatters about five times more than red, so blue light reaches your eyes from "
                                "all over the sky."},
    {"title": "So why not violet?", "lines": ["the sun sends less violet", "our eyes are weaker at it"],
     "doodle": "check", "say": "Violet scatters even more, but the sun sends less of it, and our eyes are less "
                               "sensitive to it."},
    {"title": "And sunsets?", "lines": ["light crosses more air", "blue scatters away", "red and orange survive"],
     "doodle": "underline", "say": "At sunset, light crosses much more air. The blue gets scattered away, and the "
                                   "reds and oranges make it through. Follow for more."},
]


EXPLAINER_CHANNEL = "lemo"   # youtube_auth extra-channel name; never the main (Daily Orbit) token
UPLOAD_PRIVACY = ("private", "unlisted")
EDUCATION_CATEGORY = "27"


class ExplainerError(RuntimeError):
    pass


@dataclass
class Element:
    kind: str                 # "title" | "line" | "doodle"
    text: str
    start: float
    end: float
    doodle: str = ""


@dataclass
class ScenePlan:
    index: int
    start: float
    end: float
    say: str
    elements: list[Element] = field(default_factory=list)
    erase_start: float = 0.0


# ---------------------------------------------------------------------------
# script + timeline (pure)
# ---------------------------------------------------------------------------

def validate_script(script: Any) -> list[str]:
    """Problems with a script (empty list = fine)."""
    if not isinstance(script, list) or not script:
        return ["script must be a non-empty list of scenes"]
    errs = []
    for i, s in enumerate(script):
        if not isinstance(s, dict):
            errs.append(f"scene {i + 1}: not an object")
            continue
        if not str(s.get("title") or "").strip():
            errs.append(f"scene {i + 1}: no title")
        if not isinstance(s.get("lines", []), list):
            errs.append(f"scene {i + 1}: lines must be a list")
        if s.get("doodle", "none") not in DOODLES:
            errs.append(f"scene {i + 1}: doodle must be one of {', '.join(DOODLES)}")
    return errs


def plan(script: Sequence[dict[str, Any]], narration_sec: Sequence[float] | None = None) -> list[ScenePlan]:
    """Timeline for ``script``. Each scene lasts as long as its writing or its narration, whichever is
    longer, plus a short tail and the eraser wipe (not after the last scene)."""
    errs = validate_script(list(script))
    if errs:
        raise ExplainerError("; ".join(errs))
    out: list[ScenePlan] = []
    t = 0.0
    for i, s in enumerate(script):
        sp = ScenePlan(index=i, start=t, end=t, say=str(s.get("say") or ""))
        cur = t + 0.2
        for kind, text in [("title", str(s["title"]))] + [("line", str(x)) for x in s.get("lines", [])]:
            dur = max(0.4, len(text) / WRITE_CPS)
            sp.elements.append(Element(kind, text, cur, cur + dur))
            cur += dur + GAP_SEC
        if s.get("doodle", "none") != "none":
            sp.elements.append(Element("doodle", "", cur, cur + DOODLE_SEC, doodle=s["doodle"]))
            cur += DOODLE_SEC
        spoken = float(narration_sec[i]) if narration_sec and i < len(narration_sec) else 0.0
        board_done = max(cur, t + spoken) + TAIL_SEC
        last = i == len(script) - 1
        sp.erase_start = board_done
        sp.end = board_done + (0.6 if last else ERASE_SEC)
        out.append(sp)
        t = sp.end
    return out


def total_duration(scenes: Sequence[ScenePlan]) -> float:
    return scenes[-1].end if scenes else 0.0


def wrap(text: str, font: Any, width: float) -> list[str]:
    """Greedy word wrap so no line is wider than ``width`` pixels in ``font``."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if cur and font.getlength(trial) > width:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines or [""]


def ease(p: float) -> float:
    p = min(1.0, max(0.0, p))
    return p * p * (3 - 2 * p)


# ---------------------------------------------------------------------------
# drawing
# ---------------------------------------------------------------------------

def _font(names: Sequence[str], size: int) -> Any:
    for d in FONT_DIRS:
        for n in names:
            p = d / n
            if p.exists():
                try:
                    return ImageFont.truetype(str(p), size)
                except OSError as e:
                    log.debug("font %s unreadable: %s", p, e)
    return ImageFont.load_default(size)


class Board:
    """Fonts, the empty board and per-scene layout for one output size (``scale`` = width / 1080)."""

    def __init__(self, width: int = W, height: int = H, seed: int = 7):
        self.w, self.h = width, height
        k = width / W
        self.k = k
        self.title_font = _font(TITLE_FONTS, max(8, int(118 * k)))
        self.body_font = _font(BODY_FONTS, max(6, int(82 * k)))
        self.margin = int(90 * k)
        self.base = self._base(seed)
        self._layouts: dict[int, list[tuple[Element, list[tuple[Image.Image, int, int]]]]] = {}

    def _base(self, seed: int) -> Image.Image:
        w, h, k = self.w, self.h, self.k
        img = Image.new("RGB", (w, h), BOARD)
        rnd = random.Random(seed)
        ghost = Image.new("L", (w, h), 0)
        gd = ImageDraw.Draw(ghost)
        for _ in range(14):     # faint ghosts of old, badly erased writing
            x, y = rnd.uniform(0, w), rnd.uniform(0, h)
            gd.line([(x, y), (x + rnd.uniform(80, 360) * k, y + rnd.uniform(-30, 30) * k)],
                    fill=rnd.randint(10, 22), width=max(1, int(rnd.uniform(10, 30) * k)))
        ghost = ghost.filter(ImageFilter.GaussianBlur(max(1, int(14 * k))))
        img = Image.composite(Image.new("RGB", (w, h), (205, 207, 210)), img, ghost)
        gloss = Image.new("L", (w, h), 0)  # a soft diagonal sheen across the glossy surface
        ImageDraw.Draw(gloss).polygon([(w * 0.55, 0), (w * 0.8, 0), (w * 0.25, h), (0, h)], fill=38)
        gloss = gloss.filter(ImageFilter.GaussianBlur(max(1, int(90 * k))))
        img = Image.composite(Image.new("RGB", (w, h), (255, 255, 255)), img, gloss)
        d = ImageDraw.Draw(img)
        edge = max(2, int(18 * k))   # brushed aluminium frame
        d.rectangle([0, 0, w - 1, h - 1], outline=(170, 174, 180), width=edge)
        d.rectangle([edge, edge, w - 1 - edge, h - 1 - edge], outline=(214, 216, 220), width=max(1, edge // 4))
        tray_y = h - int(150 * k)    # marker tray at the bottom
        d.rounded_rectangle([int(60 * k), tray_y, w - int(60 * k), tray_y + int(26 * k)], radius=int(8 * k),
                            fill=(186, 190, 196))
        for j, col in enumerate((INK_TITLE, INK_BODY, INK_DOODLE)):
            x0 = int((180 + j * 150) * k)
            d.rounded_rectangle([x0, tray_y - int(30 * k), x0 + int(120 * k), tray_y], radius=int(10 * k), fill=col)
        return img

    def _text_layer(self, text: str, font: Any, ink: tuple[int, int, int]) -> Image.Image:
        bbox = font.getbbox(text or " ")
        tw, th = max(1, int(bbox[2]) + 4), max(1, int(bbox[3]) + 8)
        layer = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        ImageDraw.Draw(layer).text((2, 2), text, font=font, fill=ink + (255,))
        return layer

    def layout(self, scene: ScenePlan) -> list[tuple[Element, list[tuple[Image.Image, int, int]]]]:
        """Each element with its wrapped text pieces as (layer, x, y)."""
        if scene.index in self._layouts:
            return self._layouts[scene.index]
        k, width = self.k, self.w - 2 * self.margin
        y = int(330 * k)
        out = []
        for el in scene.elements:
            pieces = []
            if el.kind in ("title", "line"):
                font = self.title_font if el.kind == "title" else self.body_font
                ink = INK_TITLE if el.kind == "title" else INK_BODY
                for ln in wrap(el.text, font, width):
                    layer = self._text_layer(ln, font, ink)
                    x = (self.w - layer.width) // 2 if el.kind == "title" else self.margin + int(30 * k)
                    pieces.append((layer, x, y))
                    y += int(layer.height * (1.15 if el.kind == "title" else 1.35))
                y += int((70 if el.kind == "title" else 10) * k)
            out.append((el, pieces))
        # centre the written block in the board above the marker tray (leaving room for an arrow)
        top, bottom = int(220 * k), self.h - int(560 * k)
        used = y - int(330 * k)
        shift = max(top - int(330 * k), (top + bottom - used) // 2 - int(330 * k))
        out = [(el, [(l, x, yy + shift) for l, x, yy in ps]) for el, ps in out]
        self._layouts[scene.index] = out
        return out

    def doodle_points(self, scene: ScenePlan, kind: str) -> list[tuple[float, float]]:
        lay = self.layout(scene)
        title = [p for el, ps in lay if el.kind == "title" for p in ps]
        lines = [p for el, ps in lay if el.kind == "line" for p in ps]
        k = self.k
        tx0 = min(x for _, x, _ in title)
        tx1 = max(x + l.width for l, x, _ in title)
        ty0 = min(y for _, _, y in title)
        ty1 = max(y + l.height for l, _, y in title)
        rnd = random.Random(scene.index)
        if kind == "circle":
            cx, cy = (tx0 + tx1) / 2, (ty0 + ty1) / 2
            rx, ry = (tx1 - tx0) / 2 + 40 * k, (ty1 - ty0) / 2 + 40 * k
            return [(cx + rx * math.cos(a) * (1 + 0.02 * math.sin(3 * a)), cy + ry * math.sin(a))
                    for a in [-2.6 + i * (2 * math.pi + 0.35) / 60 for i in range(61)]]
        if kind == "underline":
            y = ty1 + 12 * k
            return [(tx0 + (tx1 - tx0) * i / 30, y + 5 * k * math.sin(i / 4 + rnd.random())) for i in range(31)]
        if kind == "check":
            l, x, y = (lines or title)[-1]
            bx, by = x + l.width + 40 * k, y + l.height * 0.55
            return [(bx, by), (bx + 26 * k, by + 34 * k), (bx + 90 * k, by - 50 * k)]
        # arrow: a curve from the lower board up to the last line, then the head
        l, x, y = (lines or title)[-1]
        end = (x + l.width * 0.5, y + l.height + 30 * k)
        start = (self.w * 0.72, min(end[1] + 330 * k, self.h - 230 * k))
        ctrl = (self.w * 0.85, end[1] + 80 * k)
        pts = [((1 - u) ** 2 * start[0] + 2 * (1 - u) * u * ctrl[0] + u * u * end[0],
                (1 - u) ** 2 * start[1] + 2 * (1 - u) * u * ctrl[1] + u * u * end[1]) for u in [i / 30 for i in range(31)]]
        ang = math.atan2(end[1] - pts[-3][1], end[0] - pts[-3][0])
        head = [(end[0] - 50 * k * math.cos(ang + s), end[1] - 50 * k * math.sin(ang + s)) for s in (0.5, -0.5)]
        return pts + [head[0], end, head[1]]


def _nib(d: ImageDraw.ImageDraw, x: float, y: float, ink: tuple[int, int, int], k: float) -> None:
    """A marker held at an angle, its tip at (x, y)."""
    body = [(x, y), (x + 34 * k, y + 70 * k), (x + 150 * k, y + 230 * k)]
    d.line(body[1:], fill=(60, 62, 68), width=max(2, int(46 * k)))
    d.line(body[:2], fill=ink, width=max(1, int(22 * k)))
    d.ellipse([x - 6 * k, y - 6 * k, x + 6 * k, y + 6 * k], fill=ink)


def _draw_element(frame: Image.Image, d: ImageDraw.ImageDraw, board: Board, scene: ScenePlan, el: Element,
                  pieces: list[tuple[Image.Image, int, int]], p: float) -> tuple[float, float, tuple] | None:
    """Draw ``el`` at progress ``p`` (0..1) on ``frame``; returns the nib position while writing."""
    if el.kind == "doodle":
        pts = board.doodle_points(scene, el.doodle)
        n = max(2, int(round(len(pts) * p)))
        d.line(pts[:n], fill=INK_DOODLE, width=max(2, int(11 * board.k)), joint="curve")
        return (pts[n - 1][0], pts[n - 1][1], INK_DOODLE) if p < 1 else None
    total = sum(l.width for l, _, _ in pieces) or 1
    shown = total * p
    for layer, x, y in pieces:
        if shown <= 0:
            break
        cut = int(min(layer.width, shown))
        frame.paste(layer.crop((0, 0, cut, layer.height)), (x, y), layer.crop((0, 0, cut, layer.height)))
        shown -= layer.width
        if 0 < cut < layer.width and p < 1:
            ink = INK_TITLE if el.kind == "title" else INK_BODY
            return (x + cut, y + layer.height * 0.6, ink)
    return None


def render_frame(board: Board, scenes: Sequence[ScenePlan], t: float) -> Image.Image:
    """The frame at time ``t`` seconds."""
    scene = next((s for s in scenes if s.start <= t < s.end), scenes[-1])
    frame = board.base.copy()
    d = ImageDraw.Draw(frame)
    nib = None
    for el, pieces in board.layout(scene):
        if t < el.start:
            continue
        p = 1.0 if el.end <= el.start else (t - el.start) / (el.end - el.start)
        got = _draw_element(frame, d, board, scene, el, pieces, min(1.0, p))
        nib = got or nib
    is_last = scene is scenes[-1]
    if t >= scene.erase_start and not is_last:   # eraser sweeps the board clean top to bottom
        p = ease((t - scene.erase_start) / ERASE_SEC)
        cut = int(board.h * p)
        frame.paste(board.base.crop((0, 0, board.w, cut)), (0, 0))
        ey = cut
        d.rounded_rectangle([board.w * 0.12, ey - 70 * board.k, board.w * 0.88, ey + 20 * board.k],
                            radius=int(16 * board.k), fill=(52, 54, 60))
        d.rectangle([board.w * 0.12, ey + 5 * board.k, board.w * 0.88, ey + 30 * board.k], fill=(232, 228, 214))
        nib = None
    if nib:
        _nib(d, nib[0], nib[1], nib[2], board.k)
    # camera: a slow push-in across the scene, centred a little above the middle
    span = max(1e-6, scene.end - scene.start)
    z = 1 + PUSH_IN * ease((t - scene.start) / span)
    cw, ch = board.w / z, board.h / z
    cx, cy = board.w / 2, board.h * 0.45
    x0, y0 = cx - cw / 2, max(0.0, cy - ch * 0.45)
    box = (int(x0), int(y0), int(x0 + cw), int(min(board.h, y0 + ch)))
    return frame.crop(box).resize((board.w, board.h), Image.Resampling.BILINEAR)


def frames(board: Board, scenes: Sequence[ScenePlan], fps: int = FPS) -> Iterator[Image.Image]:
    n = int(math.ceil(total_duration(scenes) * fps))
    for i in range(n):
        yield render_frame(board, scenes, i / fps)


# ---------------------------------------------------------------------------
# narration + encode
# ---------------------------------------------------------------------------

TTS = Callable[[str], "tuple[Any, int]"]


def default_tts() -> TTS | None:
    """The laptop's local voices only (Kokoro, else Windows SAPI) - never a paid service."""
    try:
        from voice.engines import KokoroTTS, SapiTTS
        from voice.settings import get_voice_settings

        vs = get_voice_settings()
    except Exception as e:
        log.warning("explainer: no voice stack: %s", e)
        return None
    for make in (lambda: KokoroTTS(vs.models_dir, vs.tts_voice, vs.tts_speed), lambda: SapiTTS(vs.sapi_voice)):
        try:
            return make().synthesize
        except Exception as e:
            log.warning("explainer: voice unavailable: %s", e)
    return None


def narrate(script: Sequence[dict[str, Any]], tts: TTS | None) -> tuple[list[Any], int]:
    """Per-scene narration clips (float32 numpy arrays) and their sample rate; empty when no voice."""
    if tts is None:
        return [], 24000
    clips, rate = [], 24000
    for s in script:
        say = str(s.get("say") or "").strip()
        if not say:
            clips.append(None)
            continue
        samples, rate = tts(say)
        clips.append(samples)
    return clips, rate


def mix_track(scenes: Sequence[ScenePlan], clips: Sequence[Any], rate: int) -> Any:
    """One float32 track the length of the video with each scene's narration at its start."""
    import numpy as np

    track = np.zeros(int(math.ceil(total_duration(scenes) * rate)) + 1, dtype=np.float32)
    for sc, clip in zip(scenes, clips):
        if clip is None or len(clip) == 0:
            continue
        a = int((sc.start + 0.15) * rate)
        b = min(len(track), a + len(clip))
        track[a:b] += np.asarray(clip, dtype=np.float32)[: b - a]
    return np.clip(track, -1.0, 1.0)


def write_wav(path: Path, track: Any, rate: int) -> None:
    import numpy as np

    pcm = (np.asarray(track) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def ffmpeg_cmd(ffmpeg: str, out: Path, wav: Path, *, width: int = W, height: int = H, fps: int = FPS) -> list[str]:
    return [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", str(fps), "-i", "-",
            "-i", str(wav),
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
            "-shortest", "-movflags", "+faststart", "-f", "mp4", str(out)]


def _pipe_ffmpeg(cmd: list[str], images: Iterator[Image.Image]) -> None:
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as e:
        raise ExplainerError(f"ffmpeg not found: {cmd[0]}") from e
    assert proc.stdin is not None
    try:
        for img in images:
            proc.stdin.write(img.tobytes())
    except BrokenPipeError:
        log.warning("explainer: ffmpeg closed its input early")
    finally:
        proc.stdin.close()
    err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
    if proc.wait() != 0:
        raise ExplainerError(f"ffmpeg exited {proc.returncode}: {err[-600:]}")


def render(script: Sequence[dict[str, Any]], out: Path | None = None, *, tts: TTS | None | str = "default",
           runner: Callable[[list[str], Iterator[Image.Image]], None] | None = None,
           width: int = W, height: int = H, fps: int = FPS, ffmpeg: str | None = None) -> dict[str, Any]:
    """Render ``script`` to an MP4. Returns ``{"path", "duration_sec", "scenes", "narrated"}``."""
    import config
    import db

    if out is None:
        stamp = db.utcnow().strftime("%Y%m%d-%H%M%S")
        out = config.get_settings().renders_dir / "explainers" / f"explainer-{stamp}.mp4"
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    voice = default_tts() if tts == "default" else tts
    clips, rate = narrate(script, voice)  # type: ignore[arg-type]
    secs = [len(c) / rate if c is not None else 0.0 for c in clips]
    scenes = plan(script, secs)
    board = Board(width, height)
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "narration.wav"
        write_wav(wav, mix_track(scenes, clips, rate), rate)
        cmd = ffmpeg_cmd(ffmpeg or config.get_settings().ffmpeg_bin, out, wav, width=width, height=height, fps=fps)
        (runner or _pipe_ffmpeg)(cmd, frames(board, scenes, fps))
    result = {"path": str(out), "duration_sec": round(total_duration(scenes), 2), "scenes": len(scenes),
              "narrated": bool(clips)}
    db.log_event("explainer", f"rendered explainer {out.name} ({result['duration_sec']}s)", data=result)
    return result


def _channel_service(channel: str) -> Any:
    import youtube_auth

    return youtube_auth.youtube_service(youtube_auth.load_credentials(channel=channel))


def upload(path: Path | str, *, title: str, description: str = "", tags: Sequence[str] = (),
           channel: str = EXPLAINER_CHANNEL, privacy: str = "private") -> dict[str, Any]:
    """Upload a rendered explainer to its own channel (never Daily Orbit). Returns id, url, privacy."""
    import db
    import youtube_auth

    if not channel:
        raise ExplainerError("explainers never go to the main (Daily Orbit) channel; pass an extra channel name")
    if privacy not in UPLOAD_PRIVACY:
        raise ExplainerError(f"privacy must be one of {UPLOAD_PRIVACY}; going public is his call")
    path = Path(path)
    if not path.is_file():
        raise ExplainerError(f"video not found: {path}")
    if not youtube_auth.quota_allows("videos.insert"):
        raise ExplainerError("YouTube API quota for today is used up; try after midnight Pacific")
    desc = (description.strip() + "\n\n#Shorts").strip()
    body = {
        "snippet": {"title": title.strip()[:100], "description": desc[:4900],
                    "tags": [t for t in tags if t][:15], "categoryId": EDUCATION_CATEGORY},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    try:
        youtube = _channel_service(channel)
    except youtube_auth.YouTubeAuthError as exc:
        raise ExplainerError(str(exc)) from exc
    from googleapiclient.http import MediaFileUpload

    media = MediaFileUpload(str(path), mimetype="video/mp4", resumable=True, chunksize=8 * 1024 * 1024)
    resp = youtube.videos().insert(part="snippet,status", body=body, media_body=media).execute()
    youtube_auth.record_quota("videos.insert")
    vid = resp["id"]
    result = {"video_id": vid, "channel": channel,
              "privacy": (resp.get("status") or {}).get("privacyStatus", privacy),
              "url": f"https://youtube.com/shorts/{vid}"}
    db.log_event("explainer", f"uploaded explainer {path.name} to channel {channel} ({result['privacy']})",
                 data=result)
    return result


def main(argv: Sequence[str]) -> int:
    logging.basicConfig(level=logging.INFO)
    if argv and argv[0] == "upload":
        if len(argv) < 3:
            print("usage: python explainer.py upload <file.mp4> \"<title>\" [description]", file=sys.stderr)
            return 2
        print(json.dumps(upload(argv[1], title=argv[2], description=argv[3] if len(argv) > 3 else ""), indent=2))
        return 0
    script = json.loads(Path(argv[0]).read_text(encoding="utf-8")) if argv else DEMO_SCRIPT
    out = Path(argv[1]) if len(argv) > 1 else None
    print(json.dumps(render(script, out), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
