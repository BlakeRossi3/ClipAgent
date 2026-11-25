"""YouTube channel banner: lettered options, one comparison sheet, and upload.

Backlog #57: the old preview files (``channel_banner_desktop_preview.png`` /
``..._safe_preview.png``) were crops of ONE banner, which read like competing versions.
Here each option is a genuinely different banner with a letter, and a single sheet
(``choose_banner.png``) shows every option the way viewers see it: the full image (TV)
and the strip that phones and desktops actually show (the 1546x423 centre safe area).

* :func:`make_options` renders ``banner_A.png`` .. ``banner_C.png`` (2560x1440) from a base
  artwork (default: the planet art in ``<data>/media/avatar/channel_banner.png``) plus the sheet.
* :func:`set_banner` uploads one with ``channelBanners.insert`` and points the channel at it
  (``channels.update`` brandingSettings.image.bannerExternalUrl). Public: ask him first.

CLI: ``python -m assistant.channel_banner [options|set PATH_OR_LETTER]``.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

BANNER_SIZE = (2560, 1440)
SAFE_SIZE = (1546, 423)               # visible on every device
MAX_BYTES = 6 * 1024 * 1024           # YouTube's banner file limit
CHANNEL_NAME = "DAILY ORBIT"
TAGLINE = "Space and science, every day"
TEXT_SHIFT = 170                      # px the art moves right in the text options

#: letter -> (short label shown on the sheet, what changes)
VARIANTS: dict[str, tuple[str, str]] = {
    "A": ("Art only", "the planet scene with no text"),
    "B": ("Name", "DAILY ORBIT on the left of the planet"),
    "C": ("Name + tagline", "DAILY ORBIT with 'Space and science, every day' under it"),
}


class BannerError(RuntimeError):
    """The banner could not be made or uploaded (the message is safe to show)."""


def default_base() -> Path:
    import config
    return config.get_settings().data_dir / "media" / "avatar" / "channel_banner.png"


def default_out_dir() -> Path:
    """His Pictures folder (OneDrive's when present), subfolder ``Channel banner``."""
    home = Path.home()
    pics = home / "OneDrive" / "Pictures"
    if not pics.is_dir():
        pics = home / "Pictures"
    return pics / "Channel banner"


def safe_box(size: tuple[int, int] = BANNER_SIZE) -> tuple[int, int, int, int]:
    """The centre safe area scaled to ``size`` (left, top, right, bottom)."""
    w, h = size
    sw, sh = round(SAFE_SIZE[0] * w / BANNER_SIZE[0]), round(SAFE_SIZE[1] * h / BANNER_SIZE[1])
    left, top = (w - sw) // 2, (h - sh) // 2
    return left, top, left + sw, top + sh


def _font(size: int) -> Any:
    from PIL import ImageFont

    try:
        import edit
        for fam in ("Montserrat ExtraBold", "Montserrat Black", "Arial Black", "Segoe UI Black", "DejaVu Sans"):
            path = edit.font_file_for(fam)
            if path is not None:
                return ImageFont.truetype(str(path), size)
    except Exception as exc:   # noqa: BLE001 - any font trouble falls back to Pillow's own font
        log.debug("banner font lookup failed, using Pillow's default: %s", exc)
    try:
        return ImageFont.load_default(size)
    except TypeError:   # Pillow < 10.1
        return ImageFont.load_default()


def _text(img: Any, xy: tuple[int, int], text: str, size: int, fill: tuple[int, int, int]) -> int:
    """Draw ``text`` with a soft shadow; returns its height."""
    from PIL import Image, ImageDraw, ImageFilter

    font = _font(size)
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).text((xy[0] + 4, xy[1] + 6), text, font=font, fill=(0, 0, 20, 200))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(10)))
    d = ImageDraw.Draw(img)
    d.text(xy, text, font=font, fill=fill + (255,))
    box = d.textbbox(xy, text, font=font)
    return int(box[3] - box[1])


def render_variant(base: Any, letter: str) -> Any:
    """One banner option as an RGB 2560x1440 image. For the text options the art slides right
    (the planet sits right of centre) and the words go low on the left over a soft dark scrim,
    clear of the planet and the big star."""
    from PIL import Image, ImageDraw, ImageFilter

    if letter not in VARIANTS:
        raise BannerError(f"No option {letter}; there are {', '.join(VARIANTS)}.")
    img = base.convert("RGBA").resize(BANNER_SIZE, Image.Resampling.LANCZOS)
    if letter == "A":
        return img.convert("RGB")
    h = BANNER_SIZE[1]
    dx = TEXT_SHIFT
    shifted = Image.new("RGBA", BANNER_SIZE)
    shifted.paste(img, (dx, 0))
    shifted.paste(img.crop((0, 0, dx, h)).transpose(Image.Transpose.FLIP_LEFT_RIGHT), (0, 0))
    img = shifted
    left, _, _, bottom = safe_box()
    x = left + 50
    name_size, tag_size = (96, 0) if letter == "B" else (88, 44)
    block = name_size + (tag_size + 22 if tag_size else 0)
    y = bottom - 45 - block
    scrim = Image.new("RGBA", BANNER_SIZE, (0, 0, 0, 0))
    ImageDraw.Draw(scrim).rounded_rectangle([x - 60, y - 40, x + 820, y + block + 50], 60, fill=(4, 4, 20, 150))
    img.alpha_composite(scrim.filter(ImageFilter.GaussianBlur(40)))
    nh = _text(img, (x, y), CHANNEL_NAME, name_size, (255, 248, 225))
    if tag_size:
        _text(img, (x + 4, y + nh + 34), TAGLINE, tag_size, (150, 230, 255))
    return img.convert("RGB")


def _save(img: Any, path: Path) -> Path:
    img.save(path, "PNG", optimize=True)
    if path.stat().st_size > MAX_BYTES:        # too big for YouTube: JPEG instead
        path.unlink()
        path = path.with_suffix(".jpg")
        img.save(path, "JPEG", quality=92)
    return path


def comparison_sheet(options: dict[str, Any]) -> Any:
    """One image: per option a big letter, its label, the full banner (TV) and the safe strip
    (what phones and desktops show)."""
    from PIL import Image, ImageDraw

    full_w, strip_w, pad, head = 640, 1100, 40, 70
    full_h = full_w * BANNER_SIZE[1] // BANNER_SIZE[0]
    strip_h = strip_w * SAFE_SIZE[1] // SAFE_SIZE[0]
    row_h = head + max(full_h, strip_h) + pad
    sheet = Image.new("RGB", (pad * 3 + full_w + strip_w, pad + 60 + row_h * len(options)), (18, 18, 28))
    d = ImageDraw.Draw(sheet)
    small = _font(26)
    d.text((pad, pad), "Which banner?  Left: full image (TVs).  Right: what phones and desktops show.",
           font=small, fill=(200, 200, 215))
    y = pad + 60
    for letter, img in options.items():
        label, what = VARIANTS[letter]
        d.text((pad, y), letter, font=_font(52), fill=(255, 176, 92))
        d.text((pad + 70, y + 18), f"{label}: {what}", font=small, fill=(235, 235, 245))
        sheet.paste(img.resize((full_w, full_h), Image.Resampling.LANCZOS), (pad, y + head))
        strip = img.crop(safe_box(img.size)).resize((strip_w, strip_h), Image.Resampling.LANCZOS)
        sheet.paste(strip, (pad * 2 + full_w, y + head))
        y += row_h
    return sheet


def make_options(base_path: str | Path | None = None, out_dir: str | Path | None = None) -> dict[str, Any]:
    """Render every option and the comparison sheet; returns their paths."""
    from PIL import Image

    base_path = Path(base_path) if base_path else default_base()
    if not base_path.is_file():
        raise BannerError(f"No base artwork at {base_path}; make one first (e.g. with Higgsfield).")
    out = Path(out_dir) if out_dir else default_out_dir()
    out.mkdir(parents=True, exist_ok=True)
    with Image.open(base_path) as base:
        imgs = {k: render_variant(base, k) for k in VARIANTS}
    files = {k: str(_save(im, out / f"banner_{k}.png")) for k, im in imgs.items()}
    sheet = out / "choose_banner.png"
    comparison_sheet(imgs).save(sheet, "PNG", optimize=True)
    return {"options": {k: {"file": files[k], "label": VARIANTS[k][0], "what": VARIANTS[k][1]} for k in VARIANTS},
            "sheet": str(sheet), "folder": str(out),
            "ask": "Which banner, A, B or C? The sheet shows each one on a TV and on a phone."}


def resolve_choice(choice: str, out_dir: str | Path | None = None) -> Path:
    """A letter (A/B/C) in the options folder, or a path to an image file."""
    c = (choice or "").strip()
    if c.upper() in VARIANTS:
        folder = Path(out_dir) if out_dir else default_out_dir()
        for ext in (".png", ".jpg"):
            p = folder / f"banner_{c.upper()}{ext}"
            if p.is_file():
                return p
        raise BannerError(f"Option {c.upper()} hasn't been made yet; make the options first.")
    p = Path(c)
    if not p.is_file():
        raise BannerError(f"No image at {c}.")
    return p


def set_banner(choice: str, out_dir: str | Path | None = None) -> dict[str, Any]:
    """Upload the chosen banner and set it on the channel. Public change: only after he says yes."""
    import youtube_auth
    from assistant import channel_branding as cb

    path = resolve_choice(choice, out_dir)
    if path.stat().st_size > MAX_BYTES:
        raise BannerError("That image is over YouTube's 6 MB banner limit.")
    if not youtube_auth.quota_allows("channelBanners.insert", reserve=0):
        raise BannerError("Today's YouTube API quota is used up; try after midnight Pacific.")
    yt = cb._service()
    try:
        from googleapiclient.http import MediaFileUpload
        media = MediaFileUpload(str(path), mimetype="image/jpeg" if path.suffix.lower() == ".jpg" else "image/png")
    except ImportError:   # tests without the Google client
        media = str(path)
    url = yt.channelBanners().insert(media_body=media).execute().get("url")
    youtube_auth.record_quota("channelBanners.insert")
    if not url:
        raise BannerError("YouTube took the image but returned no banner URL.")
    item = cb._mine(yt)
    branding = dict(item.get("brandingSettings") or {})
    branding["image"] = dict(branding.get("image") or {}, bannerExternalUrl=url)
    yt.channels().update(part="brandingSettings", body={"id": item["id"], "brandingSettings": branding}).execute()
    youtube_auth.record_quota("channels.update")
    import db
    db.log_event("youtube_channel", "Channel banner updated", data={"file": str(path), "url": url})
    return {"ok": True, "channel_id": item["id"], "file": str(path), "banner_url": url}


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    p = argparse.ArgumentParser(prog="python -m assistant.channel_banner", description="YouTube channel banner")
    p.add_argument("cmd", nargs="?", default="options", choices=("options", "set"))
    p.add_argument("arg", nargs="?", default="")
    p.add_argument("--base", default=None)
    p.add_argument("--out", default=None)
    args = p.parse_args(argv)
    try:
        out = set_banner(args.arg, args.out) if args.cmd == "set" else make_options(args.base, args.out)
    except BannerError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
