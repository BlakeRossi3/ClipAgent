"""YouTube channel branding: read and set the channel description; profile picture steps; banner.

Tool ``youtube_channel`` (CLI ``python -m assistant.channel_branding [show|describe TEXT]``):

* ``show`` - title, description, keywords and avatar URL (``channels.list``, 1 unit).
* ``set_description`` - writes ``brandingSettings.channel.description`` with ``channels.update``
  (50 units). ``brandingSettings.channel`` is replaced wholesale by the API, so the current
  values (keywords, country, ...) are read first and sent back unchanged.
* ``profile_picture`` - the YouTube Data API has no way to set a channel avatar (only banners),
  so this returns the YouTube Studio steps; the owner uploads the image himself.
* ``banner_options`` / ``set_banner`` - see :mod:`assistant.channel_banner`.

Needs the ``youtube`` (manage) scope, which ``youtube_auth.SCOPES`` already requests.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

import youtube_auth

DESCRIPTION_MAX = 1000          # YouTube's limit for a channel description
_FORBIDDEN = ("<", ">")         # rejected by channels.update

PROFILE_PICTURE_STEPS = (
    "YouTube's API can't change a channel's profile picture; it's a one-minute manual step: "
    "open studio.youtube.com, choose Customization, then Branding, click Change next to Picture, "
    "pick the image (at least 98 by 98 pixels, PNG or JPG, 4 MB or less, square works best) and press Publish."
)

_service_factory: Callable[[], Any] | None = None


class ChannelError(RuntimeError):
    """The channel could not be read or changed (the message is safe to show)."""


def set_service_factory(fn: Callable[[], Any] | None) -> None:
    """Inject a callable returning a youtube v3 service (tests); ``None`` restores OAuth."""
    global _service_factory
    _service_factory = fn


def _service() -> Any:
    if _service_factory is not None:
        return _service_factory()
    try:
        return youtube_auth.youtube_service()
    except youtube_auth.YouTubeAuthError as exc:
        raise ChannelError(f"YouTube isn't signed in: {exc}") from None


def clean_description(text: str) -> str:
    """Validate a channel description; returns it trimmed or raises :class:`ChannelError`."""
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        raise ChannelError("The description is empty.")
    if any(ch in text for ch in _FORBIDDEN):
        raise ChannelError("YouTube doesn't allow < or > in a channel description.")
    if len(text) > DESCRIPTION_MAX:
        raise ChannelError(f"That's {len(text)} characters; YouTube allows {DESCRIPTION_MAX}.")
    return text


def _mine(yt: Any) -> dict[str, Any]:
    resp = yt.channels().list(part="snippet,brandingSettings", mine=True).execute()
    youtube_auth.record_quota("channels.list")
    items = resp.get("items") or []
    if not items:
        raise ChannelError("This Google account has no YouTube channel.")
    return items[0]


def _summary(item: dict[str, Any]) -> dict[str, Any]:
    snip = item.get("snippet") or {}
    chan = (item.get("brandingSettings") or {}).get("channel") or {}
    thumbs = snip.get("thumbnails") or {}
    avatar = (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url")
    return {"channel_id": item.get("id"), "title": snip.get("title") or chan.get("title"),
            "description": chan.get("description", snip.get("description", "")),
            "keywords": chan.get("keywords", ""), "avatar_url": avatar}


def show() -> dict[str, Any]:
    return _summary(_mine(_service()))


def set_description(text: str) -> dict[str, Any]:
    """Replace the channel description, keeping every other brandingSettings.channel field."""
    text = clean_description(text)
    if not youtube_auth.quota_allows("channels.update", reserve=0):
        raise ChannelError("Today's YouTube API quota is used up; try after midnight Pacific.")
    yt = _service()
    item = _mine(yt)
    channel = dict((item.get("brandingSettings") or {}).get("channel") or {})
    before = channel.get("description", "")
    channel["description"] = text
    # The API replaces brandingSettings wholesale: send the banner (image) back too, or it is dropped.
    branding = {k: v for k, v in (item.get("brandingSettings") or {}).items() if k == "image"}
    branding["channel"] = channel
    yt.channels().update(part="brandingSettings",
                         body={"id": item["id"], "brandingSettings": branding}).execute()
    youtube_auth.record_quota("channels.update")
    import db
    db.log_event("youtube_channel", "Channel description updated",
                 data={"before": before[:300], "after": text[:300]})
    return {"ok": True, "channel_id": item["id"], "title": (item.get("snippet") or {}).get("title"),
            "description": text, "previous_description": before}


def profile_picture() -> dict[str, Any]:
    return {"api_supported": False, "steps": PROFILE_PICTURE_STEPS,
            "where": "studio.youtube.com -> Customization -> Branding -> Picture"}


DESCRIPTION = (
    "His YouTube channel's branding. action 'show' reads title, description, keywords and profile picture URL; "
    "'set_description' with 'text' (max 1000 characters, no < or >) replaces the channel description and keeps "
    "the keywords; 'profile_picture' returns the YouTube Studio steps, because the API cannot set a channel avatar "
    "(make the image yourself, e.g. with Higgsfield, save it under his Pictures, and put the steps on his screen). "
    "'banner_options' renders banner options A, B, C (art only / name / name + tagline; optional 'base' artwork path) "
    "into Pictures/Channel banner plus ONE sheet choose_banner.png showing each on a TV and a phone: show him the "
    "sheet and ask 'A, B or C?'. 'set_banner' with 'choice' (a letter or an image path) uploads it as the channel "
    "banner; that is public, so only after he picks one."
)


def _banner_error() -> type[Exception]:
    from assistant.channel_banner import BannerError
    return BannerError


def _tool(ctx: Any, a: dict[str, Any]) -> Any:
    from assistant.tools import ToolError

    action = str(a.get("action") or "show")
    try:
        if action == "set_description":
            return set_description(str(a.get("text") or ""))
        if action == "profile_picture":
            return profile_picture()
        if action == "banner_options":
            from assistant import channel_banner
            return channel_banner.make_options(a.get("base") or None)
        if action == "set_banner":
            from assistant import channel_banner
            return channel_banner.set_banner(str(a.get("choice") or ""))
        return show()
    except (ChannelError, _banner_error()) as exc:
        raise ToolError(str(exc)) from None


def register_tools(register: Any) -> None:
    from assistant.tools import Tool, _obj

    register(Tool("youtube_channel", DESCRIPTION, _obj({
        "action": {"type": "string", "enum": ["show", "set_description", "profile_picture", "banner_options",
                                              "set_banner"]},
        "text": {"type": "string"},
        "choice": {"type": "string"},
        "base": {"type": "string"},
    }), _tool, owner=__name__, side_effects=True), replace=True)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    p = argparse.ArgumentParser(prog="python -m assistant.channel_branding", description="YouTube channel branding")
    p.add_argument("cmd", nargs="?", default="show", choices=("show", "describe", "picture"))
    p.add_argument("text", nargs="?", default="")
    args = p.parse_args(argv)
    try:
        out = (set_description(args.text) if args.cmd == "describe"
               else profile_picture() if args.cmd == "picture" else show())
    except ChannelError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
