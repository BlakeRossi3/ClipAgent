"""Channel banner options (A/B/C + one comparison sheet) and banner upload (backlog #57)."""

from __future__ import annotations

from typing import Any

import pytest
from assistant_fixtures import assistant_env  # noqa: F401
from PIL import Image

import youtube_auth
from assistant import channel_banner as cbn
from assistant import channel_branding as cb
from assistant import tools

pytestmark = pytest.mark.usefixtures("assistant_env")


@pytest.fixture
def base(tmp_path):
    p = tmp_path / "base.png"
    Image.new("RGB", (1280, 720), (20, 20, 60)).save(p)
    return p


def test_make_options_writes_lettered_banners_and_one_sheet(base, tmp_path):
    out = cbn.make_options(base, tmp_path / "out")
    assert set(out["options"]) == {"A", "B", "C"}
    for k, opt in out["options"].items():
        assert opt["file"].endswith(f"banner_{k}.png")
        with Image.open(opt["file"]) as im:
            assert im.size == cbn.BANNER_SIZE
    with Image.open(out["sheet"]) as sheet:
        assert sheet.width > 1000 and sheet.height > 1000
    assert "A, B or C" in out["ask"]


def test_options_actually_differ(base):
    with Image.open(base) as b:
        a, bb, c = (cbn.render_variant(b, k) for k in "ABC")
    assert a.tobytes() != bb.tobytes() != c.tobytes()
    # text sits inside the safe area: outside it, B's top band matches the shifted art
    top = cbn.safe_box()[1]
    assert bb.crop((0, 0, 2560, top - 60)).getextrema() == a.crop((0, 0, 2560, top - 60)).getextrema()


def test_missing_base_and_unknown_letter_are_plain_errors(tmp_path):
    with pytest.raises(cbn.BannerError, match="No base artwork"):
        cbn.make_options(tmp_path / "nope.png", tmp_path)
    with pytest.raises(cbn.BannerError, match="No option D"):
        cbn.render_variant(Image.new("RGB", (10, 10)), "D")
    with pytest.raises(cbn.BannerError, match="hasn't been made"):
        cbn.resolve_choice("b", tmp_path)


class _Req:
    def __init__(self, value: Any):
        self.value = value

    def execute(self):
        return self.value


class FakeYT:
    def __init__(self):
        self.inserted: list[Any] = []
        self.updates: list[dict[str, Any]] = []

    def channelBanners(self):
        yt = self

        class B:
            def insert(self, media_body):
                yt.inserted.append(media_body)
                return _Req({"url": "https://yt/banner123"})
        return B()

    def channels(self):
        yt = self

        class C:
            def list(self, **kw):
                return _Req({"items": [{"id": "UC1", "snippet": {"title": "Daily Orbit"},
                                        "brandingSettings": {"channel": {"description": "d", "keywords": "k"}}}]})

            def update(self, part, body):
                yt.updates.append(body)
                return _Req(body)
        return C()


def test_set_banner_uploads_and_keeps_channel_fields(base, tmp_path):
    cbn.make_options(base, tmp_path)
    fake = FakeYT()
    cb.set_service_factory(lambda: fake)
    try:
        before = youtube_auth.quota_used()
        out = cbn.set_banner("C", tmp_path)
    finally:
        cb.set_service_factory(None)
    assert out["banner_url"] == "https://yt/banner123" and out["file"].endswith("banner_C.png")
    assert len(fake.inserted) == 1
    branding = fake.updates[0]["brandingSettings"]
    assert branding["image"] == {"bannerExternalUrl": "https://yt/banner123"}
    assert branding["channel"] == {"description": "d", "keywords": "k"}
    assert youtube_auth.quota_used() == before + 50 + 1 + 50


class FakeYTWithBanner(FakeYT):
    def channels(self):
        yt = self
        item = {"id": "UC1", "snippet": {}, "brandingSettings": {"channel": {"description": "old"},
                                                                 "image": {"bannerExternalUrl": "https://yt/b"}}}

        class C:
            def list(self, **_kw):
                return _Req({"items": [item]})

            def update(self, part, body):
                yt.updates.append(body)
                return _Req(body)
        return C()


def test_set_description_keeps_the_banner():
    fake = FakeYTWithBanner()
    cb.set_service_factory(lambda: fake)
    try:
        cb.set_description("new")
    finally:
        cb.set_service_factory(None)
    assert fake.updates[0]["brandingSettings"]["image"] == {"bannerExternalUrl": "https://yt/b"}
    assert fake.updates[0]["brandingSettings"]["channel"]["description"] == "new"


def test_tool_banner_actions(base, tmp_path, monkeypatch):
    monkeypatch.setattr(cbn, "default_out_dir", lambda: tmp_path / "pics")
    out = cb._tool(None, {"action": "banner_options", "base": str(base)})
    assert set(out["options"]) == {"A", "B", "C"}
    with pytest.raises(tools.ToolError, match="No image"):
        cb._tool(None, {"action": "set_banner", "choice": str(tmp_path / "missing.png")})
