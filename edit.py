"""Render a picked clip into a 1080x1920 YouTube Short with ffmpeg.

Pipeline for one clip (:func:`render_clip`):

1. validate the clip status (``picked``, ``failed`` or ``rendered`` = re-render),
2. load clip + source + transcript, probe the source with ffprobe,
3. write ``renders_dir/<clip_id>.ass``: animated word-by-word captions, a hook
   overlay for the first ~3 s and an always-visible source credit (a CC BY
   licence requirement - it is never optional),
4. run ffmpeg: fast input seek + accurate trim, 30 fps, framing to 9:16
   (``center_crop`` / ``blur_fill`` / ``fit``), burn the ASS file with libass,
   H.264 (yuv420p, CRF, faststart) + AAC; silent audio is added when the
   source has no audio track,
5. verify the output with ffprobe (1080x1920, duration, audio), atomically
   move it to ``renders_dir/<clip_id>.mp4``, set ``render_path`` and status
   ``rendered``.

On failure the clip becomes ``failed`` (reason = stderr tail), an ``error``
event is logged and :class:`RenderError` is raised.

Framing is chosen by ``caption_style["framing"]`` when present
(``auto`` | ``center_crop`` | ``blur_fill`` | ``fit``; ``auto`` is the default:
16:9-ish sources are centre-cropped, near-9:16 sources are scaled, anything
else gets the blurred fill) or by the ``framing=`` argument of
:func:`render_clip`. Horizontal crop placement goes through the pluggable
:data:`crop_locator` seam (default: centre) so face tracking can plug in later.

Pure helpers (:func:`build_ass`, :func:`build_ffmpeg_cmd`) are unit-tested
without ffmpeg; :func:`run_ffmpeg` and :func:`probe_media` are the only
subprocess users and tests monkeypatch them.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import textwrap
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import db
from config import Settings, get_settings

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

OUT_W, OUT_H = 1080, 1920
FPS = 30
CRF = 20
PRESET = "veryfast"           # ~2-3x realtime for 1080x1920 on 4 cores; CRF keeps quality
AUDIO_BITRATE = "160k"
AUDIO_RATE = 48000
MAX_SHORT_SEC = 180.0         # YouTube Shorts hard limit
DURATION_TOLERANCE_SEC = 0.35
HOOK_SEC = 3.0

FRAMING_MODES = ("auto", "center_crop", "blur_fill", "fit")
RENDERABLE_STATUSES = ("picked", "failed", "rendered")

# Shorts-safe layout (in the 1080x1920 frame). The bottom ~20% holds the title /
# channel / music UI and the right ~120 px the like/comment/share column.
SAFE_SIDE_MARGIN = 120
CAPTION_BOX_W = OUT_W - 2 * SAFE_SIDE_MARGIN         # 840 px
CAPTION_Y = {"lower_third": 1440, "center": 960, "top": 700}   # anchor y per position
CAPTION_ALIGN = {"lower_third": 2, "center": 5, "top": 8}      # ASS numpad alignment
HOOK_Y = 250
HOOK_BOX_W = 920
CREDIT_Y = 112

DEFAULT_CAPTION_STYLE: dict[str, Any] = {
    "font": "Inter Black",
    "font_size": 78,
    "primary_color": "#FFFFFF",
    "highlight_color": "#FFD400",
    "outline_color": "#000000",
    "outline_px": 5,
    "max_words_per_line": 3,
    "position": "lower_third",
    "uppercase": True,
    "animation": "word_pop",
    "framing": "auto",
}

# Heavy, legible fonts tried in order when the configured font is not installed. Plain
# "Montserrat" matches the variable-weight install (Bold=-1 then selects a heavy weight);
# the last two ship with almost every Linux box, so captions always render.
FONT_FALLBACKS = ("Montserrat ExtraBold", "Montserrat Black", "Montserrat", "Anton", "Inter Black",
                  "Inter ExtraBold", "Inter Display Black", "Liberation Sans", "DejaVu Sans")
ASSETS_FONTS_DIR = Path(__file__).resolve().parent / "assets" / "fonts"

_SENTENCE_END = re.compile(r"[.?!;:]['\")\]]*$")
_CLAUSE_END = re.compile(r"[,]['\")\]]*$")
_HEX = re.compile(r"^#?([0-9A-Fa-f]{6})([0-9A-Fa-f]{2})?$")


class RenderError(Exception):
    """A clip could not be rendered (the clip row is ``failed`` unless status was not renderable)."""


# ---------------------------------------------------------------------------
# media probing + crop seam
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class MediaInfo:
    width: int                 # display width (rotation and SAR applied)
    height: int                # display height
    duration: float | None
    has_audio: bool
    fps: float | None = None

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0


def ffprobe_bin(settings: Settings | None = None) -> str:
    """``ffprobe`` next to the configured ``ffmpeg`` binary (or on PATH)."""
    ff = (settings or get_settings()).ffmpeg_bin
    p = Path(ff)
    if p.parent != Path("."):
        cand = p.with_name(p.name.replace("ffmpeg", "ffprobe"))
        if cand.exists():
            return str(cand)
    return shutil.which("ffprobe") or "ffprobe"


def _parse_rate(text: str | None) -> float | None:
    if not text or text in ("0/0",):
        return None
    try:
        if "/" in text:
            n, d = text.split("/", 1)
            return float(n) / float(d) if float(d) else None
        return float(text)
    except ValueError:
        return None


def parse_probe(data: dict[str, Any]) -> MediaInfo:
    """Turn ``ffprobe -show_streams -show_format -of json`` output into :class:`MediaInfo` (pure)."""
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    if video is None:
        raise RenderError("no video stream in source")
    w, h = int(video.get("width") or 0), int(video.get("height") or 0)
    if w <= 0 or h <= 0:
        raise RenderError(f"bad video dimensions {w}x{h}")
    sar = _parse_rate((video.get("sample_aspect_ratio") or "1:1").replace(":", "/")) or 1.0
    if sar > 0 and abs(sar - 1.0) > 1e-3:
        w = int(round(w * sar))
    rotation = 0.0
    for sd in video.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                rotation = float(sd["rotation"])
            except (TypeError, ValueError):
                pass
    tag_rot = (video.get("tags") or {}).get("rotate")
    if tag_rot:
        try:
            rotation = float(tag_rot)
        except ValueError:
            pass
    if int(round(abs(rotation))) % 180 == 90:
        w, h = h, w
    dur = None
    for cand in ((data.get("format") or {}).get("duration"), video.get("duration")):
        try:
            if cand is not None:
                dur = float(cand)
                break
        except (TypeError, ValueError):
            continue
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    fps = _parse_rate(video.get("avg_frame_rate")) or _parse_rate(video.get("r_frame_rate"))
    return MediaInfo(width=w, height=h, duration=dur, has_audio=has_audio, fps=fps)


def probe_media(path: Path | str, settings: Settings | None = None) -> MediaInfo:
    """ffprobe ``path`` (subprocess; tests monkeypatch this)."""
    cmd = [ffprobe_bin(settings), "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RenderError(f"ffprobe failed for {path}: {e}") from e
    if proc.returncode != 0:
        raise RenderError(f"ffprobe failed for {path}: {_tail(proc.stderr)}")
    try:
        return parse_probe(json.loads(proc.stdout or "{}"))
    except json.JSONDecodeError as e:
        raise RenderError(f"ffprobe returned invalid JSON for {path}") from e


# Seam for smarter framing (face / speaker tracking). A locator receives the source
# path, the clip time range and the source display size, and returns the horizontal
# centre of interest as a fraction of the source width (0.0 = left edge, 0.5 = centre).
CropLocator = Callable[[Path, float, float, int, int], float]


def center_locator(src: Path, start: float, end: float, width: int, height: int) -> float:
    return 0.5


crop_locator: CropLocator = center_locator


def set_crop_locator(fn: CropLocator | None) -> None:
    """Install a crop locator (``None`` restores the centre default)."""
    global crop_locator
    crop_locator = fn or center_locator


def crop_x_offset(src: Path, start: float, end: float, info: MediaInfo) -> int:
    """x-offset (in pixels of the source scaled to 1920 high) of the 1080-wide 9:16 crop window."""
    scaled_w = _even(info.width * OUT_H / info.height)
    if scaled_w <= OUT_W:
        return 0
    try:
        frac = float(crop_locator(src, start, end, info.width, info.height))
    except Exception:  # a broken locator must never break a render
        log.exception("crop locator failed; using centre")
        frac = 0.5
    if not math.isfinite(frac):
        frac = 0.5
    frac = min(1.0, max(0.0, frac))
    x = int(round(frac * scaled_w - OUT_W / 2))
    return max(0, min(scaled_w - OUT_W, x))


def resolve_framing(mode: str | None, info: MediaInfo) -> str:
    """Concrete framing for ``mode`` and the source aspect (``auto`` -> crop / fit / blur)."""
    mode = (mode or "auto").strip().lower()
    if mode not in FRAMING_MODES:
        log.warning("unknown framing %r; using auto", mode)
        mode = "auto"
    target = OUT_W / OUT_H
    if abs(info.aspect - target) / target < 0.03:
        return "fit"   # already vertical 9:16: nothing to crop or fill
    if mode != "auto":
        return mode
    if info.aspect >= 1.6:          # 16:9 (1.778), 16:10 (1.6), wider
        return "center_crop"
    return "blur_fill"              # 4:3, square, non-9:16 vertical ...


# ---------------------------------------------------------------------------
# ASS captions
# ---------------------------------------------------------------------------

def _even(x: float) -> int:
    n = int(round(x))
    return n - (n % 2)


def ass_color(value: str | None, default: str = "#FFFFFF", alpha: int = 0) -> str:
    """``#RRGGBB`` (or ``#RRGGBBAA``) -> ASS ``&HAABBGGRR``. Invalid input falls back to ``default``."""
    m = _HEX.match((value or "").strip()) or _HEX.match(default)
    assert m is not None
    rgb = m.group(1)
    if m.group(2):  # CSS-style alpha (FF = opaque) -> ASS alpha (00 = opaque)
        alpha = 255 - int(m.group(2), 16)
    r, g, b = rgb[0:2], rgb[2:4], rgb[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _inline_color(value: str | None, default: str) -> str:
    """Colour for an override tag: ``&HBBGGRR&``."""
    full = ass_color(value, default)
    return f"&H{full[4:]}&"


def ass_escape(text: str) -> str:
    """Make arbitrary transcript text safe inside an ASS Dialogue ``Text`` field.

    ``{``/``}`` would open override blocks and ``\\`` starts escapes (``\\N``, ``\\h``),
    so they are replaced with look-alikes; newlines/tabs become spaces.
    """
    text = (text or "").replace("\\", "/").replace("{", "(").replace("}", ")")
    text = re.sub(r"[\r\n\t\v\f]+", " ", text)
    return re.sub(r" {2,}", " ", text)


def _font_field(name: str) -> str:
    """A Style ``Fontname``: no field separators or line breaks (it comes from strategy.json)."""
    clean = re.sub(r"[,\x00-\x1f\x7f\u2028\u2029{}\\]+", " ", name or "")
    return re.sub(r" {2,}", " ", clean).strip()[:64] or "Sans"


def ass_time(t: float) -> str:
    cs = max(0, int(round(t * 100)))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def merged_caption_style(caption_style: dict[str, Any] | None) -> dict[str, Any]:
    style = dict(DEFAULT_CAPTION_STYLE)
    for k, v in (caption_style or {}).items():
        if not str(k).startswith("_") and v is not None:
            style[k] = v
    try:
        style["font_size"] = max(24, min(200, int(style["font_size"])))
    except (TypeError, ValueError):
        style["font_size"] = DEFAULT_CAPTION_STYLE["font_size"]
    try:
        style["max_words_per_line"] = max(1, min(8, int(style["max_words_per_line"])))
    except (TypeError, ValueError):
        style["max_words_per_line"] = DEFAULT_CAPTION_STYLE["max_words_per_line"]
    try:
        style["outline_px"] = max(0, min(20, int(style["outline_px"])))
    except (TypeError, ValueError):
        style["outline_px"] = DEFAULT_CAPTION_STYLE["outline_px"]
    if style.get("position") not in CAPTION_Y:
        style["position"] = "lower_third"
    if style.get("animation") not in ("word_pop", "karaoke", "none"):
        style["animation"] = "word_pop"
    return style


def max_line_chars(font_size: int, uppercase: bool) -> int:
    """Rough character budget for one caption line inside the safe caption box."""
    per_char = font_size * (0.70 if uppercase else 0.58)
    return max(6, int(CAPTION_BOX_W / per_char))


@dataclass(slots=True)
class _Word:
    start: float
    end: float
    text: str


def _clean_words(words: Sequence[dict[str, Any]], clip_start: float, duration: float, upper: bool) -> list[_Word]:
    out: list[_Word] = []
    for w in words or []:
        try:
            s = float(w["start"]) - clip_start
            e = float(w["end"]) - clip_start
        except (KeyError, TypeError, ValueError):
            continue
        text = str(w.get("word") or "").strip()
        if not text or not (math.isfinite(s) and math.isfinite(e)):
            continue
        if e <= 0 or s >= duration:
            continue
        s, e = max(0.0, s), min(duration, max(e, s))
        if upper:
            text = text.upper()
        out.append(_Word(s, e, ass_escape(text)))
    out.sort(key=lambda w: (w.start, w.end))
    return out


def group_words(words: list[_Word], max_words: int, max_chars: int, max_gap: float = 0.6) -> list[list[_Word]]:
    """Split words into short caption lines (word / char budget, pauses, sentence ends)."""
    groups: list[list[_Word]] = []
    cur: list[_Word] = []
    chars = 0
    for w in words:
        if cur:
            prev = cur[-1]
            new_chars = chars + 1 + len(w.text)
            if (len(cur) >= max_words or new_chars > max_chars or w.start - prev.end > max_gap
                    or _SENTENCE_END.search(prev.text) or (_CLAUSE_END.search(prev.text) and len(cur) >= 2)):
                groups.append(cur)
                cur, chars = [], 0
        cur.append(w)
        chars = len(w.text) if len(cur) == 1 else chars + 1 + len(w.text)
    if cur:
        groups.append(cur)
    return groups


def _wrap(text: str, width: int, max_lines: int) -> list[str]:
    lines = textwrap.wrap(text, width=width, break_long_words=True, break_on_hyphens=False) or [""]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        lines[-1] = (last[: max(0, width - 1)].rstrip() + "…") if len(last) >= width else last + "…"
    return lines


def build_ass(words: list[dict], clip_start: float, caption_style: dict, *, hook_text: str = "",
              credit_text: str = "", duration: float, hook_sec: float = HOOK_SEC) -> str:
    """Build the ASS subtitle file (pure).

    ``words`` are transcript words with absolute source times (``start``, ``end``,
    ``word``); they are shifted by ``clip_start`` and clipped to ``[0, duration]``.
    Captions: lines of up to ``max_words_per_line`` words, the spoken word
    highlighted (``word_pop``: colour + pop scale; ``karaoke``: progressive colour
    fill; ``none``: plain lines). Hook: big boxed text in the top third for
    ``hook_sec``. Credit: small line at the top, visible for the whole clip.
    """
    duration = max(0.01, float(duration))
    st = merged_caption_style(caption_style)
    fs = st["font_size"]
    font = _font_field(st["font"])
    upper = bool(st["uppercase"])
    primary = ass_color(st["primary_color"], "#FFFFFF")
    highlight = ass_color(st["highlight_color"], "#FFD400")
    outline = ass_color(st["outline_color"], "#000000")
    shadow = ass_color(st["outline_color"], "#000000", alpha=0x60)
    # heavy outline: at least ~9% of the font size whatever the strategy says (legibility on busy video)
    opx = max(st["outline_px"], round(fs * 0.09))
    pos = st["position"]
    anim = st["animation"]

    hook_fs = max(56, min(110, round(fs * 1.08)))
    credit_fs = 34
    # Karaoke fills from SecondaryColour to PrimaryColour, so its style swaps them.
    styles = [
        ("Caption", font, fs, primary, highlight, outline, shadow, -1, 1, opx, 3, CAPTION_ALIGN[pos]),
        ("CaptionK", font, fs, highlight, primary, outline, shadow, -1, 1, opx, 3, CAPTION_ALIGN[pos]),
        ("Hook", font, hook_fs, "&H00FFFFFF", "&H00FFFFFF", "&H28000000", "&H64000000", -1, 3, 16, 0, 8),
        ("Credit", font, credit_fs, "&H20FFFFFF", "&H20FFFFFF", "&H50000000", "&HA0000000", -1, 1, 2, 1, 8),
    ]
    lines = [
        "[Script Info]",
        "; Generated by edit.py (Auto Shorts Agent)",
        "ScriptType: v4.00+",
        f"PlayResX: {OUT_W}",
        f"PlayResY: {OUT_H}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
    ]
    for name, fnt, size, pc, sc, oc, bc, bold, border, out_w, shad, align in styles:
        lines.append(f"Style: {name},{fnt},{size},{pc},{sc},{oc},{bc},{bold},0,0,0,100,100,0,0,{border},"
                     f"{out_w},{shad},{align},{SAFE_SIDE_MARGIN},{SAFE_SIDE_MARGIN},0,1")
    lines += ["", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]

    def dialogue(layer: int, start: float, end: float, style: str, text: str) -> None:
        if ass_time(end) == ass_time(start) or end <= start:
            return
        lines.append(f"Dialogue: {layer},{ass_time(start)},{ass_time(end)},{style},,0,0,0,,{text}")

    # --- credit (always visible; licence requirement) ---------------------------
    credit = ass_escape(credit_text).strip()
    if credit:
        if len(credit) > 60:
            credit = credit[:59].rstrip() + "…"
        dialogue(3, 0.0, duration, "Credit", f"{{\\pos({OUT_W // 2},{CREDIT_Y})}}{credit}")

    # --- hook --------------------------------------------------------------------
    hook = ass_escape(hook_text).strip()
    if hook and hook_sec > 0:
        width = max(8, int(HOOK_BOX_W / (hook_fs * 0.60)))
        text = "\\N".join(_wrap(hook, width, 4))
        hook_end = min(duration, hook_sec)
        dialogue(2, 0.0, hook_end, "Hook",
                 f"{{\\pos({OUT_W // 2},{HOOK_Y})\\fad(120,250)\\fscx85\\fscy85"
                 f"\\t(0,160,\\fscx104\\fscy104)\\t(160,260,\\fscx100\\fscy100)}}{text}")

    # --- captions ------------------------------------------------------------------
    ws = _clean_words(words, clip_start, duration, upper)
    groups = group_words(ws, st["max_words_per_line"], max_line_chars(fs, upper))
    cy = CAPTION_Y[pos]
    pos_tag = f"\\pos({OUT_W // 2},{cy})"
    hl = _inline_color(st["highlight_color"], "#FFD400")
    for gi, g in enumerate(groups):
        g_start = g[0].start
        nxt = groups[gi + 1][0].start if gi + 1 < len(groups) else duration
        g_end = min(nxt, max(g[-1].end + 0.35, g_start + 0.3), duration)
        if anim == "karaoke":
            parts = []
            for i, w in enumerate(g):
                until = g[i + 1].start if i + 1 < len(g) else w.end
                k = max(1, int(round((until - w.start) * 100)))
                parts.append(f"{{\\kf{k}}}{w.text}")
            dialogue(1, g_start, g_end, "CaptionK", "{" + pos_tag + "}" + " ".join(parts))
        elif anim == "none":
            dialogue(1, g_start, g_end, "Caption", "{" + pos_tag + "}" + " ".join(w.text for w in g))
        else:  # word_pop: one event per spoken word with that word coloured + popped
            for i, w in enumerate(g):
                ev_start = g_start if i == 0 else w.start
                ev_end = g[i + 1].start if i + 1 < len(g) else g_end
                parts = []
                for j, o in enumerate(g):
                    if j == i:
                        parts.append(f"{{\\c{hl}\\fscx108\\fscy108\\t(0,70,\\fscx120\\fscy120)"
                                     f"\\t(70,150,\\fscx112\\fscy112)}}{o.text}{{\\r}}")
                    else:
                        parts.append(o.text)
                dialogue(1, ev_start, ev_end, "Caption", "{" + pos_tag + "}" + " ".join(parts))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# ffmpeg command
# ---------------------------------------------------------------------------

def _escape_filter_value(value: str) -> str:
    """Escape a filter option value for an ffmpeg filtergraph string (both quoting levels)."""
    # level 1: option value (av_get_token): backslash-escape \ ' :
    lvl1 = re.sub(r"([\\':])", r"\\\1", value)
    # level 2: filtergraph description: backslash-escape \ ' [ ] , ;
    return re.sub(r"([\\'\[\],;])", r"\\\1", lvl1)


def framing_filters(framing: str, info: MediaInfo, crop_x: int | None = None) -> str:
    """Filter chain from ``[v0]`` (fps-normalised source) to ``[vf]`` (1080x1920, SAR 1)."""
    if framing == "fit":
        return (f"[v0]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={OUT_W}:{OUT_H},setsar=1[vf]")
    if framing == "center_crop":
        if info.aspect >= OUT_W / OUT_H:   # wider than 9:16: fit height, crop width
            sw = max(OUT_W, _even(info.width * OUT_H / info.height))
            x = (sw - OUT_W) // 2 if crop_x is None else max(0, min(sw - OUT_W, int(crop_x)))
            return f"[v0]scale={sw}:{OUT_H}:flags=lanczos,crop={OUT_W}:{OUT_H}:{x}:0,setsar=1[vf]"
        sh = max(OUT_H, _even(info.height * OUT_W / info.width))   # taller: fit width, crop height
        return f"[v0]scale={OUT_W}:{sh}:flags=lanczos,crop={OUT_W}:{OUT_H}:0:{(sh - OUT_H) // 2},setsar=1[vf]"
    if framing == "blur_fill":
        return (
            "[v0]split=2[bgsrc][fgsrc];"
            f"[bgsrc]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H},"
            f"scale={OUT_W // 4}:{OUT_H // 4},gblur=sigma=12,eq=brightness=-0.16:saturation=1.1,"
            f"scale={OUT_W}:{OUT_H},setsar=1[bg];"
            f"[fgsrc]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease:flags=lanczos,setsar=1[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2:format=auto[vf]"
        )
    raise ValueError(f"unknown framing {framing!r}")


def build_ffmpeg_cmd(src: Path, ass_path: Path, out: Path, start: float, end: float,
                     settings: Settings, *, info: MediaInfo | None = None, framing: str = "auto",
                     crop_x: int | None = None, fonts_dir: Path | None = None) -> list[str]:
    """ffmpeg argv for one render (pure).

    ``-ss`` before ``-i`` (fast keyframe seek; ffmpeg then decodes and discards up
    to the exact start, so the cut is frame-accurate and A/V stay in sync) and
    ``-t`` on the output. ``info`` is the probed source (default: 1920x1080 with
    audio); without audio a silent ``anullsrc`` track is muxed in.
    """
    info = info or MediaInfo(1920, 1080, None, True)
    dur = round(max(0.0, end - start), 3)
    if dur <= 0:
        raise ValueError("end must be after start")
    mode = resolve_framing(framing, info)
    ass_opt = f"filename={_escape_filter_value(str(ass_path))}"
    if fonts_dir is not None:
        ass_opt += f":fontsdir={_escape_filter_value(str(fonts_dir))}"
    graph = (f"[0:v:0]fps={FPS},format=yuv420p[v0];"
             + framing_filters(mode, info, crop_x)
             + f";[vf]ass={ass_opt},format=yuv420p[vout]")
    cmd = [settings.ffmpeg_bin, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-ss", f"{max(0.0, start):.3f}", "-i", str(src)]
    if info.has_audio:
        graph += f";[0:a:0]aresample={AUDIO_RATE}:async=1:first_pts=0[aout]"
    else:
        cmd += ["-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_RATE}"]
    cmd += ["-filter_complex", graph, "-map", "[vout]", "-map", "[aout]" if info.has_audio else "1:a:0",
            "-t", f"{dur:.3f}",
            "-c:v", "libx264", "-preset", PRESET, "-crf", str(CRF), "-profile:v", "high",
            "-pix_fmt", "yuv420p", "-r", str(FPS), "-g", str(FPS * 2),
            "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", str(AUDIO_RATE), "-ac", "2",
            "-movflags", "+faststart", "-map_metadata", "-1", "-f", "mp4", str(out)]
    return cmd


def _tail(text: str | None, n: int = 1200) -> str:
    text = (text or "").strip()
    return text[-n:] if len(text) > n else text


def run_ffmpeg(cmd: list[str], *, timeout: float | None = 1800) -> None:
    """Run ffmpeg; raise :class:`RenderError` with the stderr tail on failure (tests monkeypatch)."""
    log.debug("ffmpeg: %s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise RenderError(f"ffmpeg not found: {cmd[0]}") from e
    except subprocess.TimeoutExpired as e:
        raise RenderError(f"ffmpeg timed out after {timeout}s") from e
    if proc.returncode != 0:
        raise RenderError(f"ffmpeg exited {proc.returncode}: {_tail(proc.stderr) or '(no stderr)'}")


# ---------------------------------------------------------------------------
# fonts, transcript, credit
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _installed_families() -> frozenset[str] | None:
    exe = shutil.which("fc-list")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, ":", "family"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    fams: set[str] = set()
    for line in out.splitlines():
        for name in line.split(","):
            if name.strip():
                fams.add(name.strip().lower())
    return frozenset(fams)


def _bundled_families() -> set[str]:
    """Family names guessed from ``assets/fonts/*.ttf|otf`` file names.

    ``Montserrat-ExtraBold.ttf`` -> {"montserrat extrabold", "montserrat"};
    ``Montserrat[wght].ttf`` -> {"montserrat"}.
    """
    if not ASSETS_FONTS_DIR.is_dir():
        return set()
    out: set[str] = set()
    for p in ASSETS_FONTS_DIR.iterdir():
        if p.suffix.lower() not in (".ttf", ".otf"):
            continue
        stem = re.sub(r"\[.*?\]", "", p.stem).strip()
        out.add(stem.replace("-", " ").lower())
        out.add(stem.split("-", 1)[0].lower())
    return out


def resolve_font(name: str | None) -> str:
    """First installed font among ``name`` and :data:`FONT_FALLBACKS` (``name`` if fonts can't be listed)."""
    fams = _installed_families()
    if fams is None:
        return name or DEFAULT_CAPTION_STYLE["font"]
    available = set(fams) | _bundled_families()
    for cand in ([name] if name else []) + list(FONT_FALLBACKS):
        if cand and cand.lower() in available:
            return cand
    return name or "Sans"


def _local_words_between(transcript: dict, start: float, end: float) -> list[dict]:
    eps = 1e-3
    out: list[dict] = []
    for seg in transcript.get("segments") or []:
        words = seg.get("words")
        if not words and seg.get("text"):
            # no word timestamps: spread the segment text evenly over its time span
            toks = str(seg["text"]).split()
            s0, s1 = float(seg.get("start", 0.0)), float(seg.get("end", 0.0))
            step = (s1 - s0) / len(toks) if toks and s1 > s0 else 0.0
            words = [{"start": s0 + i * step, "end": s0 + (i + 1) * step, "word": t} for i, t in enumerate(toks)]
        for w in words or []:
            try:
                ws, we = float(w["start"]), float(w["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if ws >= start - eps and we <= end + eps:
                out.append(w)
    return out


def words_for_clip(transcript: dict, start: float, end: float) -> list[dict]:
    """Transcript words inside the clip (``transcribe.words_between`` when available)."""
    try:
        import transcribe  # late import: optional at edit-time
        fn = getattr(transcribe, "words_between")
        words = fn(transcript, start, end)
        if words or not transcript.get("segments"):
            return list(words)
    except Exception:  # module missing / half-written: use the local reader
        pass
    return _local_words_between(transcript, start, end)


def _load_transcript(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("transcript JSON is not an object")
    return data


_LICENCE_LABEL = {"us_gov_public_domain": "public domain", "cc_by": "CC BY"}


def credit_for(clip: db.Clip, source: db.Source) -> str:
    """On-screen credit: the clip's ``credit_text``, else ``pick.credit_line``, else a local fallback."""
    if clip.credit_text and clip.credit_text.strip():
        return clip.credit_text.strip()
    try:
        import pick
        text = pick.credit_line(source)
        if text and text.strip():
            return text.strip()
    except Exception:
        pass
    who = source.channel or source.platform or "original source"
    label = _LICENCE_LABEL.get(clip.licence or source.licence or "", clip.licence or "licensed")
    return f"Video: {who} ({label})"


# ---------------------------------------------------------------------------
# render_clip
# ---------------------------------------------------------------------------

def _fail(clip_id: int, reason: str, data: dict[str, Any] | None = None) -> RenderError:
    reason = _tail(reason, 1500)
    try:
        db.set_clip_status(clip_id, "failed", reason)
    except Exception:
        log.exception("could not mark clip %s failed", clip_id)
    db.log_event("edit", f"render failed for clip {clip_id}", level="error",
                 data={"clip_id": clip_id, "reason": reason, **(data or {})})
    return RenderError(f"clip {clip_id}: {reason}")


def verify_output(path: Path, expected_duration: float, settings: Settings | None = None) -> MediaInfo:
    """ffprobe the render; raise :class:`RenderError` unless it is a valid 1080x1920 Short with audio."""
    if not path.exists() or path.stat().st_size == 0:
        raise RenderError(f"no output written at {path}")
    info = probe_media(path, settings)
    problems = []
    if (info.width, info.height) != (OUT_W, OUT_H):
        problems.append(f"size {info.width}x{info.height} != {OUT_W}x{OUT_H}")
    if not info.has_audio:
        problems.append("no audio stream")
    if info.duration is None or abs(info.duration - expected_duration) > DURATION_TOLERANCE_SEC:
        problems.append(f"duration {info.duration} != {expected_duration:.2f}")
    if problems:
        raise RenderError("output check failed: " + "; ".join(problems))
    return info


def render_clip(clip_id: int, strategy: Any | None = None, *, framing: str | None = None) -> Path:
    """Render clip ``clip_id`` to ``renders_dir/<clip_id>.mp4``; returns the path.

    Allowed from ``picked``, ``failed`` (retry) or ``rendered`` (idempotent re-render).
    ``framing`` overrides ``caption_style["framing"]``.
    """
    settings = get_settings()
    clip = db.require_clip(clip_id)
    if clip.status not in RENDERABLE_STATUSES:
        raise RenderError(f"clip {clip_id} is {clip.status!r}; renderable from {RENDERABLE_STATUSES}")
    if strategy is None:
        import strategy as strategy_mod
        strategy = strategy_mod.load_strategy()
    source = db.require_source(clip.source_id)

    start, end = float(clip.start_sec), float(clip.end_sec)
    duration = round(end - start, 3)
    if duration <= 0:
        raise _fail(clip_id, f"invalid clip range {start}..{end}")
    if duration > MAX_SHORT_SEC:
        raise _fail(clip_id, f"clip is {duration:.1f}s; YouTube Shorts must be <= {MAX_SHORT_SEC:.0f}s")
    max_len = (strategy.get("clip_length_sec") or {}).get("max")
    if max_len and duration > float(max_len) + 0.5:
        db.log_event("edit", f"clip {clip_id} is {duration:.1f}s, longer than strategy max {max_len}s",
                     level="warning", data={"clip_id": clip_id, "duration": duration, "max": max_len})

    if not source.video_path or not Path(source.video_path).exists():
        raise _fail(clip_id, f"source video missing: {source.video_path!r}")
    src = Path(source.video_path)

    # transcript -> words (captions are skipped, with a warning, if it is unreadable)
    words: list[dict] = []
    tpath = Path(source.transcript_path) if source.transcript_path else settings.transcripts_dir / f"{source.id}.json"
    try:
        words = words_for_clip(_load_transcript(tpath), start, end)
    except (OSError, ValueError) as e:
        db.log_event("edit", f"clip {clip_id}: transcript unavailable, rendering without captions",
                     level="warning", data={"clip_id": clip_id, "transcript_path": str(tpath), "error": str(e)})

    try:
        info = probe_media(src, settings)
    except RenderError as e:
        raise _fail(clip_id, str(e)) from e
    if info.duration is not None and start >= info.duration:
        raise _fail(clip_id, f"clip starts at {start}s but source is {info.duration:.1f}s long")
    if info.duration is not None and end > info.duration + 0.05:
        # never render past the end of the source (verification would fail on a short tail)
        end = info.duration
        duration = round(end - start, 3)

    style = merged_caption_style(strategy.get("caption_style"))
    style["font"] = resolve_font(style.get("font"))
    mode = resolve_framing(framing or style.get("framing"), info)
    crop_x = crop_x_offset(src, start, end, info) if mode == "center_crop" and info.aspect >= OUT_W / OUT_H else None

    settings.renders_dir.mkdir(parents=True, exist_ok=True)
    out = settings.renders_dir / f"{clip_id}.mp4"
    tmp = settings.renders_dir / f"{clip_id}.tmp.mp4"
    ass_path = settings.renders_dir / f"{clip_id}.ass"
    credit = credit_for(clip, source)
    ass_text = build_ass(words, start, style, hook_text=clip.hook_text or "", credit_text=credit,
                         duration=duration)
    ass_path.write_text(ass_text, encoding="utf-8")

    fonts_dir = ASSETS_FONTS_DIR if ASSETS_FONTS_DIR.is_dir() else None
    cmd = build_ffmpeg_cmd(src, ass_path, tmp, start, end, settings, info=info, framing=mode,
                           crop_x=crop_x, fonts_dir=fonts_dir)
    t0 = time.monotonic()
    try:
        run_ffmpeg(cmd, timeout=max(600.0, duration * 30))
        verify_output(tmp, duration, settings)
        os.replace(tmp, out)
    except RenderError as e:
        tmp.unlink(missing_ok=True)
        raise _fail(clip_id, str(e), {"framing": mode}) from e
    elapsed = time.monotonic() - t0

    with db.transaction() as conn:
        db.update_clip(clip_id, render_path=str(out.resolve()), conn=conn)
        db.set_clip_status(clip_id, "rendered", conn=conn)
    db.log_event("edit", f"rendered clip {clip_id} ({duration:.1f}s, {mode})",
                 data={"clip_id": clip_id, "render_path": str(out), "duration": duration, "framing": mode,
                       "font": style["font"], "words": len(words), "render_sec": round(elapsed, 1)})
    return out
