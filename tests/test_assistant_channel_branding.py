"""YouTube channel branding tool: description read/write and profile picture steps."""

from __future__ import annotations

from typing import Any

import pytest
from assistant_fixtures import assistant_env  # noqa: F401

import youtube_auth
from assistant import channel_branding as cb
from assistant import tools

pytestmark = pytest.mark.usefixtures("assistant_env")


class _Req:
    def __init__(self, value: Any):
        self.value = value

    def execute(self):
        return self.value


class FakeChannels:
    def __init__(self, items: list[dict[str, Any]]):
        self.items = items
        self.updates: list[dict[str, Any]] = []

    def list(self, **kw):
        assert kw.get("mine") is True
        return _Req({"items": self.items})

    def update(self, part: str, body: dict[str, Any]):
        assert part == "brandingSettings"
        self.updates.append(body)
        return _Req(body)


class FakeYT:
    def __init__(self, items):
        self.ch = FakeChannels(items)

    def channels(self):
        return self.ch


def _item():
    return {"id": "UC123", "snippet": {"title": "Space Clips", "description": "old",
                                       "thumbnails": {"default": {"url": "https://yt/a.jpg"}}},
            "brandingSettings": {"channel": {"title": "Space Clips", "description": "old",
                                             "keywords": "space nasa", "country": "US"}}}


@pytest.fixture
def yt():
    fake = FakeYT([_item()])
    cb.set_service_factory(lambda: fake)
    yield fake
    cb.set_service_factory(None)


def test_show_reads_channel(yt):
    out = cb.show()
    assert out == {"channel_id": "UC123", "title": "Space Clips", "description": "old",
                   "keywords": "space nasa", "avatar_url": "https://yt/a.jpg"}


def test_set_description_keeps_other_fields_and_counts_quota(yt):
    before = youtube_auth.quota_used()
    out = cb.set_description("  Daily space and science Shorts.  ")
    body = yt.ch.updates[0]
    assert body["id"] == "UC123"
    assert body["brandingSettings"]["channel"] == {"title": "Space Clips", "description": "Daily space and science Shorts.",
                                                   "keywords": "space nasa", "country": "US"}
    assert out["previous_description"] == "old"
    assert youtube_auth.quota_used() == before + 1 + 50


@pytest.mark.parametrize("text, msg", [("", "empty"), ("a <b>", "< or >"), ("x" * 1001, "1000")])
def test_bad_descriptions_are_refused_without_calling_youtube(yt, text, msg):
    with pytest.raises(cb.ChannelError, match=msg):
        cb.set_description(text)
    assert yt.ch.updates == []


def test_no_channel():
    cb.set_service_factory(lambda: FakeYT([]))
    try:
        with pytest.raises(cb.ChannelError, match="no YouTube channel"):
            cb.show()
    finally:
        cb.set_service_factory(None)


def test_profile_picture_gives_studio_steps():
    out = cb.profile_picture()
    assert out["api_supported"] is False
    assert "Customization" in out["steps"] and "Branding" in out["steps"]


def test_tool_registered_for_main_and_dispatches(yt):
    tools.load_extensions()
    from assistant import registry

    main = registry.load_agents(skills=registry.load_skills(), known_tools=tools.known_tool_names())["main"]
    assert "youtube_channel" in main.tools
    ctx = tools.ToolContext(channel="dashboard", agent="main", source="owner")
    r = tools.dispatch("youtube_channel", {"action": "set_description", "text": "New text"}, ctx,
                       allowed=set(main.tools))
    assert r.ok and "New text" in r.content
    r = tools.dispatch("youtube_channel", {"action": "set_description", "text": ""}, ctx, allowed=set(main.tools))
    assert not r.ok and "empty" in r.content
