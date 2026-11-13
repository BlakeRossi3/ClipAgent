"""Watch a reel the owner sends: download, transcribe, keyframes, metadata, a vault note.

``watch(url) -> ReelReport`` does the plain-code part (zero tokens):

1. **Download** with yt-dlp (Instagram, YouTube Shorts, TikTok, X and anything else yt-dlp
   knows) into ``<data_dir>/assistant/reels/<id>/``. When ``REELS_BROWSER_COOKIES=chrome`` (or
   edge/firefox...) the browser's cookies are used for login-walled reels; if they cannot be read
   (on Windows Chrome locks its cookie database while it is running) it retries without them and
   says so. Duration and file-size caps keep a stray 3-hour video out.
2. **Transcript** with the pipeline's speech-to-text backend (faster-whisper; ``transcribe.set_backend``).
3. **Keyframes**: ffmpeg scene-change detection merged with a ~1 fps sample, thinned to
   ``REELS_MAX_FRAMES`` (8-12) evenly spread JPEGs scaled to ~768 px - enough to *see* the reel.
4. **Metadata**: title, uploader, duration, caption/description, link.
5. **Vault note** ``Inbox/Reels/<date>-<slug>.md`` with all of the above.

The judgment (what the reel shows, how it applies to Sterling or the channel) is the model's
job; ``skills/watch-reel/SKILL.md`` says how. Tool: ``reel_watch``. CLI:
``python -m assistant.reels watch <url> [--refresh] [--json]`` and ``... cleanup``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import config
import db

log = logging.getLogger(__name__)

MODULE = "assistant.reels"
FFMPEG_TIMEOUT_SEC = 300
SCENE_THRESHOLD = 0.3
MIN_FRAME_GAP_SEC = 0.8
MAX_TRANSCRIPT_CHARS = 12_000
FORMAT_SELECTOR = "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/bv*[height<=1080]+ba/b[height<=1080]/b"
REPORT_FILE = "report.json"

# yt-dlp failure text -> (code, message for the owner with the fix)
_ERRORS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"private (video|account|post)|this (video|account) is private|is private", re.I), "private",
     "That reel is private. Ask the owner to make it public, or turn on REELS_BROWSER_COOKIES with a "
     "browser that is logged into an account that follows them."),
    (re.compile(r"log ?in|sign ?in|login required|cookies|rate-limit reached|authentication|age[- ]restrict|"
                r"confirm your age|requested content is not available", re.I), "login_required",
     "The site wants a login to show that reel. Set REELS_BROWSER_COOKIES=chrome in Settings (close Chrome "
     "first on Windows), make sure you are logged in there, then send it again."),
    (re.compile(r"removed|deleted|no longer available|not available|unavailable|does not exist|404|"
                r"page not found|has been terminated", re.I), "removed",
     "That reel has been removed or is not available any more."),
    (re.compile(r"unsupported url|no video formats|no video could be found|there'?s no video", re.I), "unsupported",
     "I could not find a video at that link. Send the reel's own share link (not a profile or a search page)."),
    (re.compile(r"geo|not available in your country", re.I), "geo_blocked", "That reel is blocked in this region."),
]
_COOKIE_ERR = re.compile(r"cookie|dpapi|could not copy|permission denied|database is locked|keyring|decrypt", re.I)


class ReelError(Exception):
    """A reel could not be watched; ``code`` is machine-readable, the message says what to do."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class ReelReport:
    id: str
    url: str
    dir: str
    video_path: str
    duration_sec: float | None
    metadata: dict[str, Any]
    frames: list[dict[str, Any]] = field(default_factory=list)   # [{"path", "t"}] in time order
    transcript: str = ""
    segments: list[dict[str, Any]] = field(default_factory=list)  # [{"start","end","text"}]
    language: str | None = None
    note_path: str | None = None
    cookies_used: str = ""
    warnings: list[str] = field(default_factory=list)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def for_model(self, transcript_chars: int = 6000) -> dict[str, Any]:
        """Compact view for a tool result: frame paths to Read, transcript, metadata."""
        t = self.transcript
        if len(t) > transcript_chars:
            t = t[:transcript_chars] + "…[truncated; full text in the vault note]"
        md = {k: v for k, v in self.metadata.items() if k != "description"}
        desc = str(self.metadata.get("description") or "")
        md["caption"] = desc[:1500] + ("…" if len(desc) > 1500 else "")
        return {"id": self.id, "url": self.url, "metadata": md, "duration_sec": self.duration_sec,
                "frames": [f"{f['path']} (t={f['t']:.1f}s)" for f in self.frames],
                "transcript": t or "(no speech)", "language": self.language, "vault_note": self.note_path,
                "warnings": self.warnings,
                "how_to_watch": "Read each frame image in order (they are JPEG files), alongside the transcript, "
                                "then follow the watch-reel skill."}


# ---------------------------------------------------------------------------
# paths / ids
# ---------------------------------------------------------------------------

def reels_root() -> Path:
    return config.get_settings().data_dir / "assistant" / "reels"


def validate_url(url: str) -> str:
    u = (url or "").strip().strip("<>")
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ReelError("bad_url", "That is not a web link. Send the reel's share link (https://...).")
    host = p.hostname.lower()
    if host in ("localhost",) or host.endswith(".local") or re.fullmatch(r"[\d.]+|\[?[0-9a-f:]+\]?", host):
        raise ReelError("bad_url", "That link points at a local address, not a reel.")
    return u


def canonical_url(url: str) -> str:
    """Strip tracking query strings (igshid, si, utm_*) so the same reel maps to one id."""
    p = urlparse(url)
    keep = [q for q in p.query.split("&") if q and not re.match(r"(igshid|igsh|si|utm_[a-z]+|feature|_t|_r)=", q, re.I)]
    host = (p.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    return f"https://{host}{p.path.rstrip('/')}" + (("?" + "&".join(keep)) if keep else "")


def reel_id(url: str) -> str:
    return hashlib.sha1(canonical_url(url).encode()).hexdigest()[:12]


def slugify(text: str, max_len: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:max_len].rstrip("-") or "reel")


# ---------------------------------------------------------------------------
# yt-dlp (tests replace ``ydl_extract``)
# ---------------------------------------------------------------------------

def ydl_extract(url: str, opts: dict[str, Any], download: bool) -> dict[str, Any]:
    import yt_dlp  # heavy optional import

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=download)
        return ydl.sanitize_info(info)


def _ydl_opts(outdir: Path | None, cookies: str) -> dict[str, Any]:
    from assistant.toolkit_settings import get_toolkit_settings

    t = get_toolkit_settings()
    opts: dict[str, Any] = {
        "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
        "socket_timeout": 30, "retries": 3, "fragment_retries": 3,
        "format": FORMAT_SELECTOR, "merge_output_format": "mp4",
        "max_filesize": t.reels_max_filesize_mb * 1024 * 1024,
        "writeinfojson": False, "writesubtitles": False, "writethumbnail": False, "overwrites": True,
    }
    ff = config.get_settings().ffmpeg_bin
    if "/" in ff or "\\" in ff:
        opts["ffmpeg_location"] = ff
    if cookies:
        opts["cookiesfrombrowser"] = (cookies,)
    if outdir is not None:
        opts["outtmpl"] = {"default": str(outdir / "video.%(ext)s")}
        opts["paths"] = {"home": str(outdir), "temp": str(outdir)}
        opts["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]
    return opts


def classify_error(text: str) -> ReelError:
    for pat, code, msg in _ERRORS:
        if pat.search(text or ""):
            return ReelError(code, msg)
    return ReelError("download_failed", f"I could not download that reel ({(text or 'unknown error').strip()[:200]}).")


def _extract(url: str, outdir: Path | None, warnings: list[str]) -> tuple[dict[str, Any], str]:
    """Run yt-dlp with browser cookies if configured, falling back to none. Returns (info, cookies_used)."""
    from assistant.toolkit_settings import get_toolkit_settings

    browser = get_toolkit_settings().reels_browser_cookies
    attempts = [browser, ""] if browser else [""]
    last: Exception | None = None
    for cookies in attempts:
        try:
            return ydl_extract(url, _ydl_opts(outdir, cookies), outdir is not None), cookies
        except Exception as exc:  # yt_dlp.utils.DownloadError and friends
            last = exc
            text = str(exc)
            if cookies and _COOKIE_ERR.search(text):
                warnings.append(f"Could not read {cookies} cookies ({text.strip()[:120]}); on Windows close "
                                f"{cookies.title()} completely and try again. Tried without cookies.")
                continue
            if cookies:  # a real failure even with cookies: try once more without (cookies can hurt, e.g. expired)
                continue
            break
    raise classify_error(str(last))


# ---------------------------------------------------------------------------
# ffmpeg: probe, keyframes
# ---------------------------------------------------------------------------

def _ffprobe() -> str:
    from edit import ffprobe_bin

    return ffprobe_bin()


def probe(path: Path) -> dict[str, Any]:
    """{"duration", "width", "height", "has_audio"} via ffprobe."""
    try:
        out = subprocess.run([_ffprobe(), "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
                              "-of", "json", str(path)], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=60, check=True).stdout
        data = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise ReelError("bad_media", f"The downloaded file could not be read ({type(exc).__name__}).") from exc
    streams = data.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    dur = (data.get("format") or {}).get("duration")
    return {"duration": float(dur) if dur not in (None, "N/A") else None, "width": v.get("width"),
            "height": v.get("height"), "has_audio": any(s.get("codec_type") == "audio" for s in streams),
            "has_video": bool(v)}


def scene_times(path: Path, threshold: float = SCENE_THRESHOLD) -> list[float]:
    """Timestamps (s) where ffmpeg's scene score exceeds ``threshold``."""
    cmd = [config.get_settings().ffmpeg_bin, "-hide_banner", "-nostdin", "-i", str(path), "-an", "-sn",
           "-vf", f"select='gt(scene,{threshold})',showinfo", "-fps_mode", "vfr", "-f", "null", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=FFMPEG_TIMEOUT_SEC)
    except (OSError, subprocess.SubprocessError):
        return []
    return [float(m) for m in re.findall(r"pts_time:([\d.]+)", proc.stderr or "")]


def choose_frame_times(duration: float, scenes: list[float], max_frames: int) -> list[float]:
    """Up to ``max_frames`` timestamps: the opening frame and scene cuts first (that is where something
    new appears), then ~1 fps samples that fill the biggest gaps. At least 0.8 s apart."""
    if duration <= 0:
        return [0.0]
    end = max(0.0, duration - 0.15)
    target = max(1, min(max_frames, int(duration / MIN_FRAME_GAP_SEC) or 1))

    def spaced(ts: list[float], picked: list[float]) -> list[float]:
        for t in ts:
            if all(abs(t - p) >= MIN_FRAME_GAP_SEC for p in picked):
                picked.append(t)
        return picked

    first = round(min(0.3, end), 2)
    scene = spaced(sorted({round(min(end, max(0.0, s + 0.25)), 2) for s in scenes}), [first])
    if len(scene) > target:  # more cuts than slots: keep the first frame, thin the cuts evenly
        rest = scene[1:]
        step = len(rest) / (target - 1) if target > 1 else 0
        scene = [first] + [rest[int(i * step)] for i in range(target - 1)]
    picked = sorted(scene)
    uniform = [round(min(end, i + 0.5), 2) for i in range(int(duration) + 1)]
    while len(picked) < target:
        cands = [t for t in uniform if all(abs(t - p) >= MIN_FRAME_GAP_SEC for p in picked)]
        if not cands:
            break
        best = max(cands, key=lambda t: min(abs(t - p) for p in picked))
        picked.append(best)
        picked.sort()
    return picked


def extract_frame(path: Path, t: float, out: Path, px: int) -> bool:
    scale = f"scale='if(gt(iw,ih),{px},-2)':'if(gt(iw,ih),-2,{px})'"
    cmd = [config.get_settings().ffmpeg_bin, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
           "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1", "-vf", scale, "-q:v", "4", str(out)]
    try:
        subprocess.run(cmd, capture_output=True, timeout=120, check=True)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.is_file() and out.stat().st_size > 0


def keyframes(path: Path, duration: float, outdir: Path, *, max_frames: int, px: int) -> list[dict[str, Any]]:
    outdir.mkdir(parents=True, exist_ok=True)
    for old in outdir.glob("frame_*.jpg"):
        old.unlink()
    times = choose_frame_times(duration, scene_times(path), max_frames)
    frames = []
    for i, t in enumerate(times, 1):
        out = outdir / f"frame_{i:02d}_{t:06.1f}s.jpg"
        if extract_frame(path, t, out, px):
            frames.append({"path": str(out.resolve()), "t": float(t)})
    return frames


# ---------------------------------------------------------------------------
# transcript
# ---------------------------------------------------------------------------

def transcribe_media(path: Path, workdir: Path, warnings: list[str]) -> tuple[str, list[dict[str, Any]], str | None]:
    import transcribe

    wav = workdir / "audio.wav"
    try:
        transcribe.extract_audio(path, wav)
        raw = transcribe.get_backend().transcribe(wav)
        segs = transcribe.normalize_segments(raw.get("segments") or [])
    except transcribe.TranscribeError as exc:
        if "no audio" not in str(exc):
            warnings.append(f"no transcript: {exc}")
        return "", [], None
    except Exception as exc:  # whisper missing / model failure: frames still work
        warnings.append(f"no transcript ({type(exc).__name__}: {str(exc)[:120]})")
        return "", [], None
    finally:
        wav.unlink(missing_ok=True)
    text = " ".join(s["text"] for s in segs).strip()
    return text[:MAX_TRANSCRIPT_CHARS], [{"start": s["start"], "end": s["end"], "text": s["text"]} for s in segs], \
        raw.get("language")


# ---------------------------------------------------------------------------
# vault note
# ---------------------------------------------------------------------------

def _fmt_t(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"


def write_vault_note(rep: ReelReport) -> str | None:
    from assistant import vault

    md = rep.metadata
    day = db.local_today().isoformat()
    rel = f"Inbox/Reels/{day}-{slugify(md.get('title') or md.get('uploader') or rep.id)}.md"
    lines = [f"# {md.get('title') or 'Reel'}", "",
             f"- Link: {rep.url}", f"- From: {md.get('uploader') or 'unknown'} ({md.get('platform') or '?'})",
             f"- Length: {_fmt_t(rep.duration_sec or 0)}", f"- Files: `{rep.dir}`", ""]
    if md.get("description"):
        lines += ["## Caption", "", str(md["description"])[:3000], ""]
    lines += ["## Transcript", ""]
    lines += [f"- [{_fmt_t(s['start'])}] {s['text']}" for s in rep.segments] or ["(no speech)"]
    lines += ["", "## Keyframes", ""] + [f"- {_fmt_t(f['t'])} `{f['path']}`" for f in rep.frames]
    lines += ["", "## Takeaways", "", "_(to be filled in after watching)_", ""]
    try:
        vault.ensure_vault()
        note = vault.write_note(rel, "\n".join(lines), meta={"tags": ["reel"], "url": rep.url},
                                source="reel", note_type="reel")
        return note.path
    except Exception as exc:  # the report is still useful without the note
        rep.warnings.append(f"vault note not written ({type(exc).__name__})")
        log.warning("reel note failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# the front door
# ---------------------------------------------------------------------------

def _platform(info: dict[str, Any], url: str) -> str:
    key = str(info.get("extractor_key") or info.get("extractor") or "").lower()
    host = (urlparse(url).hostname or "").lower()
    for name, needles in (("instagram", ("instagram",)), ("tiktok", ("tiktok",)),
                          ("youtube", ("youtube", "youtu.be")), ("x", ("twitter", "x.com"))):
        if any(n in key or n in host for n in needles):
            return name
    return key or host


def _metadata(info: dict[str, Any], url: str) -> dict[str, Any]:
    return {
        "title": (info.get("title") or "").strip()[:300],
        "uploader": info.get("uploader") or info.get("channel") or info.get("uploader_id"),
        "uploader_url": info.get("uploader_url") or info.get("channel_url"),
        "platform": _platform(info, url),
        "webpage_url": info.get("webpage_url") or url,
        "upload_date": info.get("upload_date"),
        "duration": info.get("duration"),
        "description": (info.get("description") or "")[:5000],
        "view_count": info.get("view_count"), "like_count": info.get("like_count"),
        "tags": (info.get("tags") or [])[:20],
    }


def load_report(rid: str) -> ReelReport | None:
    p = reels_root() / rid / REPORT_FILE
    if not p.is_file():
        return None
    try:
        return ReelReport(**json.loads(p.read_text(encoding="utf-8")))
    except (ValueError, TypeError):
        return None


def watch(url: str, *, refresh: bool = False) -> ReelReport:
    """Download + transcribe + keyframes + vault note. Cached per reel unless ``refresh``."""
    from assistant.toolkit_settings import get_toolkit_settings

    t = get_toolkit_settings()
    url = validate_url(url)
    rid = reel_id(url)
    if not refresh and (cached := load_report(rid)) and all(Path(f["path"]).is_file() for f in cached.frames):
        return cached
    workdir = reels_root() / rid
    if workdir.exists():
        shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    try:
        meta_info, _ = _extract(url, None, warnings)
        if meta_info.get("_type") == "playlist" or meta_info.get("entries"):
            entries = [e for e in (meta_info.get("entries") or []) if e]
            if not entries:
                raise ReelError("unsupported", "That link is a collection with no video in it.")
            meta_info = entries[0]
            warnings.append("The link held several videos; I watched the first.")
        dur = meta_info.get("duration")
        if dur and float(dur) > t.reels_max_duration_sec:
            raise ReelError("too_long", f"That video is {int(float(dur) // 60)} minutes long; I only watch videos "
                                        f"up to {t.reels_max_duration_sec // 60} minutes (REELS_MAX_DURATION_SEC).")
        target = meta_info.get("webpage_url") or url
        info, cookies = _extract(target, workdir, warnings)
        video = next((p for p in sorted(workdir.glob("video.*")) if p.suffix in (".mp4", ".mkv", ".webm", ".mov")), None)
        if video is None:
            raise ReelError("download_failed", "The download finished but no video file was written "
                                               "(it may be over the size cap, REELS_MAX_FILESIZE_MB).")
        pr = probe(video)
        if not pr["has_video"]:
            raise ReelError("bad_media", "That link only has audio or an image, not a video.")
        duration = pr["duration"] or float(info.get("duration") or 0)
        frames = keyframes(video, duration, workdir / "frames", max_frames=t.reels_max_frames, px=t.reels_frame_px)
        if not frames:
            warnings.append("no keyframes could be extracted")
        text, segs, lang = (transcribe_media(video, workdir, warnings) if pr["has_audio"] else ("", [], None))
        rep = ReelReport(id=rid, url=url, dir=str(workdir.resolve()), video_path=str(video.resolve()),
                         duration_sec=round(duration, 2), metadata=_metadata({**meta_info, **info}, url),
                         frames=frames, transcript=text, segments=segs, language=lang, cookies_used=cookies,
                         warnings=warnings, created_at=db.now_iso())
        rep.note_path = write_vault_note(rep)
        (workdir / REPORT_FILE).write_text(json.dumps(rep.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
    except ReelError as exc:
        db.log_event("assistant", f"reel not watched: {exc.code}", level="warning", data={"url": url[:300]})
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    db.log_event("assistant", f"reel watched: {rep.metadata.get('title') or rid}",
                 data={"frames": len(rep.frames), "words": len(rep.transcript.split()), "note": rep.note_path})
    return rep


def cleanup(days: int | None = None) -> int:
    """Delete downloaded reels older than ``days`` (default REELS_KEEP_DAYS). Vault notes stay."""
    from assistant.toolkit_settings import get_toolkit_settings

    keep = days if days is not None else get_toolkit_settings().reels_keep_days
    root = reels_root()
    if not root.is_dir():
        return 0
    cutoff = time.time() - keep * 86400
    n = 0
    for d in root.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n


# ---------------------------------------------------------------------------
# tool + CLI
# ---------------------------------------------------------------------------

def _tool_watch(ctx: Any, a: dict[str, Any]) -> Any:
    from assistant.tools import ToolError

    try:
        return watch(a["url"], refresh=bool(a.get("refresh"))).for_model()
    except ReelError as exc:
        raise ToolError(f"{exc.code}: {exc}") from None


def register_tools(register: Callable[..., None]) -> None:
    from assistant.tools import Tool, _obj

    register(Tool("reel_watch",
                  "Watch a reel/short the owner sent (Instagram, YouTube Shorts, TikTok, X): downloads it, returns "
                  "keyframe image paths in time order, the transcript, caption and uploader, and files a note in "
                  "Inbox/Reels. Then view the frames (Read each JPEG) and follow the watch-reel skill. Takes up to a "
                  "minute or two.",
                  _obj({"url": {"type": "string"}, "refresh": {"type": "boolean"}}, ["url"]), _tool_watch,
                  owner=MODULE, side_effects=True))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m assistant.reels", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("watch", help="download + transcribe + keyframes + vault note")
    w.add_argument("url")
    w.add_argument("--refresh", action="store_true")
    w.add_argument("--json", action="store_true", help="print the full report as JSON")
    c = sub.add_parser("cleanup", help="delete downloaded reels older than N days")
    c.add_argument("--days", type=int)
    args = ap.parse_args(argv)
    if args.cmd == "cleanup":
        print(f"deleted {cleanup(args.days)} reel folder(s)")
        return 0
    try:
        rep = watch(args.url, refresh=args.refresh)
    except ReelError as exc:
        print(f"error ({exc.code}): {exc}", file=sys.stderr)
        return 2
    print(json.dumps(rep.to_dict() if args.json else rep.for_model(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
