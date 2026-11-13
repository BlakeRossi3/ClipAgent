"""Tests for fetch.py: the licence gate (pure) and the fetch stage (yt-dlp faked)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

import db
import fetch
from fetch import LICENCE_CC_BY, LICENCE_US_GOV, FetchError, LicenceRejected, check_licence

NASA_CHANNEL_ID = "UCLA_DiR1FfKNvjuUpBHmylQ"


def yt_info(**over: Any) -> dict[str, Any]:
    """A realistic yt-dlp info dict for an official NASA YouTube upload."""
    info: dict[str, Any] = {
        "id": "abc123",
        "title": "Artemis II Crew Q&A",
        "webpage_url": "https://www.youtube.com/watch?v=abc123",
        "extractor_key": "Youtube",
        "extractor": "youtube",
        "channel": "NASA",
        "channel_id": NASA_CHANNEL_ID,
        "uploader": "NASA",
        "uploader_id": "@NASA",
        "license": None,
        "description": "Join the Artemis II astronauts as they answer your questions. Credit: NASA",
        "duration": 900,
        "live_status": "not_live",
        "is_live": False,
        "age_limit": 0,
        "availability": "public",
        "upload_date": "20261110",
        "width": 1920,
        "height": 1080,
    }
    info.update(over)
    return info


def cc_info(**over: Any) -> dict[str, Any]:
    base = yt_info(
        id="cc0001", channel="Some Science Channel", channel_id="UCsomething", uploader="Some Science Channel",
        uploader_id="@somescience", license="Creative Commons Attribution license (reuse allowed)",
        description="A great physics explainer. Licensed CC BY.", webpage_url="https://www.youtube.com/watch?v=cc0001",
    )
    base.update(over)
    return base


def nasa_file_info(**over: Any) -> dict[str, Any]:
    info: dict[str, Any] = {
        "id": "video",
        "title": "video",
        "webpage_url": "https://images-assets.nasa.gov/video/KSC-2026-launch/KSC-2026-launch~orig.mp4",
        "extractor_key": "Generic",
        "extractor": "generic",
        "duration": None,
        "description": None,
    }
    info.update(over)
    return info


# ---------------------------------------------------------------------------
# check_licence: US government works
# ---------------------------------------------------------------------------

class TestGovLicence:
    def test_official_nasa_youtube_channel_approved(self, strategy):
        d = check_licence(yt_info(), strategy, "nasa")
        assert d.ok and d.licence == LICENCE_US_GOV
        assert "17 U.S.C. 105" in d.reason
        ev = d.evidence
        for key in ("extractor", "channel", "channel_id", "uploader", "license", "webpage_url",
                    "matched_rule", "description_excerpt"):
            assert key in ev
        assert ev["channel_id"] == NASA_CHANNEL_ID
        assert "channel id" in ev["matched_rule"]

    def test_nasa_handle_match_without_pinned_id(self, strategy):
        d = check_licence(yt_info(channel="NASA Jet Propulsion Laboratory", uploader="NASA Jet Propulsion Laboratory",
                                  channel_id="UCjpl", uploader_id="@NASAJPL"), strategy, "nasa")
        assert d.ok, d.reason

    def test_impostor_channel_named_nasa_rejected(self, strategy):
        d = check_licence(yt_info(channel_id="UCfakefakefake", uploader_id="@NASA_fan"), strategy, "nasa")
        assert not d.ok and d.licence is None
        assert "channel_id" in d.reason

    def test_youtube_url_never_passes_on_domain_alone(self, strategy):
        # A random channel on YouTube is not a government publisher even though
        # the strategy's nasa domains would be checked against the URL host.
        d = check_licence(yt_info(channel="Space Fan", uploader="Space Fan", channel_id="UCx", uploader_id="@fan"),
                          strategy, "nasa")
        assert not d.ok
        assert "publisher not allowed" in d.reason

    def test_youtube_missing_channel_id_rejected(self, strategy):
        d = check_licence(yt_info(channel="NASA Goddard", uploader="NASA Goddard", channel_id=None,
                                  uploader_id="@NASAGoddard"), strategy, "nasa")
        assert not d.ok and "channel_id" in d.reason

    @pytest.mark.parametrize("channel", ["C-SPAN", "CSPAN", "c-span 2", "C-SPAN3", "Forbes Breaking News", "Reuters"])
    def test_rebroadcasters_rejected(self, strategy, channel):
        info = yt_info(channel=channel, uploader=channel, channel_id="UCcspan", uploader_id="@cspan",
                       title="Senate hearing on AI")
        d = check_licence(info, strategy, "congress")
        assert not d.ok
        assert "blocklist" in d.reason or "not allowed" in d.reason

    def test_blocklisted_uploader_even_if_channel_differs(self, strategy):
        d = check_licence(yt_info(uploader="C-SPAN"), strategy, "nasa")
        assert not d.ok and "blocklist" in d.reason

    def test_official_committee_channel_approved(self, strategy):
        name = "House Committee on Science, Space, and Technology"
        info = yt_info(channel=name, uploader=name, channel_id="UChouse", uploader_id="@HouseScience",
                       description="Full committee hearing.")
        # shipped entry has neither channel_id nor handle: the name alone must not admit it
        d = check_licence(info, strategy, "congress")
        assert not d.ok and "not allowed" in d.reason
        allow = json.loads(json.dumps(strategy["allowlist"]))
        allow["congress"]["channels"][0]["channel_id"] = "UChouse"
        d = check_licence(info, strategy.updated(allowlist=allow), "congress")
        assert d.ok and d.licence == LICENCE_US_GOV

    @pytest.mark.parametrize("info,stype", [
        # anyone can create a YouTube channel *named* like an allowlisted committee / JPL
        (dict(channel="House Committee on Oversight and Accountability",
              uploader="House Committee on Oversight and Accountability", channel_id="UCattacker",
              uploader_id="@attacker"), "congress"),
        (dict(channel="NASA Jet Propulsion Laboratory", uploader="NASA Jet Propulsion Laboratory",
              channel_id="UCattacker", uploader_id="@jplfan"), "nasa"),
        # official handle but a different channel id than the pinned one
        (dict(channel="Totally NASA", uploader="Totally NASA", channel_id="UCattacker", uploader_id="@NASA"), "nasa"),
    ])
    def test_name_only_and_handle_spoofs_rejected(self, strategy, info, stype):
        d = check_licence(yt_info(**info), strategy, stype)
        assert not d.ok, d.reason

    def test_non_youtube_uploader_named_nasa_rejected(self, strategy):
        info = {"webpage_url": "https://vimeo.com/12345", "extractor_key": "Vimeo", "uploader": "NASA",
                "channel": None, "duration": 600, "description": "cool launch"}
        assert not check_licence(info, strategy, "nasa").ok

    @pytest.mark.parametrize("url", [
        "ftp://images.nasa.gov/x.mp4",
        "https://evil.example\\@images.nasa.gov/x.mp4",
        "https://nasa:pw@images.nasa.gov/x.mp4",
    ])
    def test_odd_urls_on_allowed_hosts_rejected(self, strategy, url):
        assert not check_licence(nasa_file_info(webpage_url=url), strategy, "nasa").ok

    @pytest.mark.parametrize("channel", ["\u0421-SPAN", "ＣＳＰＡＮ", "Daily CSPAN Clips", "C\u2011SPAN"])
    def test_rebroadcaster_lookalikes_rejected_for_cc(self, strategy, channel):
        d = check_licence(cc_info(channel=channel, uploader=channel), strategy, "cc_by_youtube")
        assert not d.ok and "blocklisted" in d.reason

    def test_house_gov_direct_url_approved(self, strategy):
        d = check_licence(nasa_file_info(webpage_url="https://science.house.gov/media/hearing.mp4", duration=3600),
                          strategy, "congress")
        assert d.ok and "house.gov" in d.evidence["matched_rule"]

    def test_nasa_direct_file_without_duration_approved(self, strategy):
        d = check_licence(nasa_file_info(), strategy, "nasa")
        assert d.ok, d.reason
        assert d.evidence["host"] == "images-assets.nasa.gov"

    @pytest.mark.parametrize("url", [
        "https://nasa.gov.evil.example/video.mp4",
        "https://notnasa.gov/video.mp4",
        "https://nasa.gov@evil.example/video.mp4",
        "https://example.com/nasa.gov/video.mp4",
    ])
    def test_lookalike_domains_rejected(self, strategy, url):
        d = check_licence(nasa_file_info(webpage_url=url), strategy, "nasa")
        assert not d.ok

    def test_wrong_source_type_for_domain(self, strategy):
        # house.gov is not on the nasa allowlist
        d = check_licence(nasa_file_info(webpage_url="https://science.house.gov/a.mp4"), strategy, "nasa")
        assert not d.ok

    def test_source_type_not_enabled_in_strategy(self, strategy):
        d = check_licence(nasa_file_info(webpage_url="https://www.dvidshub.net/video/1"), strategy, "dvids")
        assert not d.ok and "not enabled" in d.reason

    def test_unknown_source_type(self, strategy):
        assert not check_licence(yt_info(), strategy, "tiktok").ok

    @pytest.mark.parametrize("desc,flag", [
        ("Footage (c) 2025 Caltech. All rights reserved.", "all rights reserved"),
        ("© 2026 Some Contractor", "copyright symbol"),
        ("This video is copyrighted by its owner.", "copyright notice"),
        ("Music: 'Lift Off' by Someone, Universal Production Music", "commercial music library"),
        ("Music credit: Killer Tracks", "music credit"),
        ("Contains licensed music.", "licensed music"),
        ("Animation used with permission of ESA.", "used with permission"),
    ])
    def test_description_red_flags_reject_gov(self, strategy, desc, flag):
        d = check_licence(yt_info(description=desc), strategy, "nasa")
        assert not d.ok
        assert flag in [f["flag"] for f in d.evidence["red_flags"]]
        assert d.evidence["description_excerpt"]  # context around the flag, for audit

    def test_not_subject_to_copyright_is_fine(self, strategy):
        desc = ("NASA content is generally not subject to copyright in the United States. "
                "This material is not copyrighted.")
        d = check_licence(yt_info(description=desc), strategy, "nasa")
        assert d.ok, d.reason

    def test_gov_ignores_youtube_licence_field(self, strategy):
        assert check_licence(yt_info(license="Standard YouTube License"), strategy, "nasa").ok


# ---------------------------------------------------------------------------
# check_licence: CC BY
# ---------------------------------------------------------------------------

class TestCcByLicence:
    @pytest.mark.parametrize("lic", [
        "Creative Commons Attribution license (reuse allowed)",
        "Creative Commons Attribution 4.0 International",
        "CC BY 4.0",
        "cc-by",
    ])
    def test_cc_by_approved(self, strategy, lic):
        d = check_licence(cc_info(license=lic), strategy, "cc_by_youtube")
        assert d.ok and d.licence == LICENCE_CC_BY
        assert d.evidence["license"] == lic

    @pytest.mark.parametrize("lic,needle", [
        (None, "no licence"),
        ("", "no licence"),
        ("Standard YouTube License", "Standard YouTube"),
        ("Creative Commons Attribution-NonCommercial 4.0", "NonCommercial"),
        ("Creative Commons Attribution-NoDerivatives 4.0", "NonCommercial/NoDerivs"),
        ("Creative Commons Attribution-ShareAlike 4.0", "ShareAlike"),
        ("CC BY-NC-SA 4.0", "NonCommercial"),
        ("CC BY-ND", "NoDerivs"),
        ("CC BY-SA 3.0", "ShareAlike"),
        ("Creative Commons Zero (CC0)", "not Creative Commons Attribution"),
        ("Public domain", "not Creative Commons Attribution"),
    ])
    def test_non_cc_by_rejected(self, strategy, lic, needle):
        d = check_licence(cc_info(license=lic), strategy, "cc_by_youtube")
        assert not d.ok and d.licence is None
        assert needle in d.reason
        assert d.evidence["license"] == lic

    def test_cc_by_must_be_on_youtube(self, strategy):
        d = check_licence(cc_info(webpage_url="https://vimeo.com/123", extractor_key="Vimeo"), strategy,
                          "cc_by_youtube")
        assert not d.ok

    def test_cc_by_blocklisted_channel_rejected(self, strategy):
        d = check_licence(cc_info(channel="Associated Press", uploader="Associated Press"), strategy, "cc_by_youtube")
        assert not d.ok and "blocklist" in d.reason

    def test_cc_by_all_rights_reserved_contradiction(self, strategy):
        d = check_licence(cc_info(description="© 2026 Me. All rights reserved."), strategy, "cc_by_youtube")
        assert not d.ok and "all rights reserved" in d.reason

    @pytest.mark.parametrize("desc", ["Free for non-commercial use only.", "Licensed CC BY-NC 4.0",
                                      "Please do not reupload.", "Footage courtesy of C-SPAN."])
    def test_cc_by_description_restrictions_reject(self, strategy, desc):
        assert not check_licence(cc_info(description=desc), strategy, "cc_by_youtube").ok

    def test_cc_by_copyright_symbol_alone_ok(self, strategy):
        assert check_licence(cc_info(description="© 2026 Me, licensed CC BY 4.0"), strategy, "cc_by_youtube").ok

    def test_cc_by_youtube_url_with_non_youtube_extractor(self, strategy):
        assert not check_licence(cc_info(extractor_key="Generic"), strategy, "cc_by_youtube").ok


# ---------------------------------------------------------------------------
# check_licence: universal rejections
# ---------------------------------------------------------------------------

class TestUniversalRejections:
    @pytest.mark.parametrize("over,needle", [
        ({"is_live": True, "live_status": "is_live"}, "live stream"),
        ({"live_status": "is_upcoming"}, "upcoming"),
        ({"live_status": "post_live"}, "still processing"),
        ({"age_limit": 18}, "age-restricted"),
        ({"availability": "subscriber_only"}, "availability"),
        ({"availability": "premium_only"}, "availability"),
        ({"duration": 5}, "shorter"),
        ({"duration": 4 * 3600}, "exceeds"),
        ({"duration": None}, "no duration"),
        ({"duration": "abc"}, "invalid duration"),
        ({"_type": "playlist", "entries": []}, "playlist"),
        ({"webpage_url": None, "original_url": None}, "no webpage URL"),
    ])
    def test_rejections(self, strategy, over, needle):
        d = check_licence(yt_info(**over), strategy, "nasa")
        assert not d.ok and d.licence is None
        assert needle in d.reason
        assert isinstance(d.evidence, dict) and d.evidence["source_type"] == "nasa"

    def test_was_live_vod_is_fine(self, strategy):
        assert check_licence(yt_info(live_status="was_live"), strategy, "nasa").ok

    def test_direct_file_too_long_rejected_when_duration_known(self, strategy):
        assert not check_licence(nasa_file_info(duration=5 * 3600), strategy, "nasa").ok

    def test_pure_does_not_mutate_info(self, strategy):
        info = yt_info()
        snapshot = json.dumps(info, sort_keys=True)
        check_licence(info, strategy, "nasa")
        assert json.dumps(info, sort_keys=True) == snapshot

    def test_evidence_is_json_serialisable(self, strategy):
        for info, st in ((yt_info(), "nasa"), (cc_info(license=None), "cc_by_youtube"), (yt_info(age_limit=18), "nasa")):
            json.dumps(check_licence(info, strategy, st).evidence)


# ---------------------------------------------------------------------------
# fetch_source
# ---------------------------------------------------------------------------

class FakeYtDlp:
    """Stand-in for fetch.extract_info: records calls and writes a fake video on download."""

    def __init__(self, probe: dict[str, Any], downloaded: dict[str, Any] | None = None,
                 probe_error: Exception | None = None, download_error: Exception | None = None,
                 write_file: bool = True):
        self.probe = probe
        self.downloaded = downloaded if downloaded is not None else probe
        self.probe_error = probe_error
        self.download_error = download_error
        self.write_file = write_file
        self.calls: list[tuple[str, bool, Path | None]] = []

    def __call__(self, url: str, *, download: bool, outdir: Path | None = None) -> dict[str, Any]:
        self.calls.append((url, download, outdir))
        if not download:
            if self.probe_error:
                raise self.probe_error
            return dict(self.probe)
        if self.download_error:
            raise self.download_error
        assert outdir is not None
        if self.write_file:
            (outdir / "video.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42 fake video")
        return dict(self.downloaded)

    @property
    def downloads(self) -> int:
        return sum(1 for _, d, _ in self.calls if d)


@pytest.fixture
def fake_ytdlp(monkeypatch):
    def install(*args: Any, **kwargs: Any) -> FakeYtDlp:
        fake = FakeYtDlp(*args, **kwargs)
        monkeypatch.setattr(fetch, "extract_info", fake)
        return fake
    return install


_REAL_PROBE_DURATION = fetch.probe_duration


@pytest.fixture(autouse=True)
def _no_ffprobe(monkeypatch):
    monkeypatch.setattr(fetch, "probe_duration", lambda path: None)


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs ffmpeg")
def test_probe_duration_real_ffprobe(tmp_path):
    video = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc=size=64x36:rate=5:duration=3", "-pix_fmt", "yuv420p", str(video)],
                   check=True, capture_output=True)
    assert _REAL_PROBE_DURATION(video) == pytest.approx(3.0, abs=0.3)
    garbage = tmp_path / "g.mp4"
    garbage.write_bytes(b"not a video")
    assert _REAL_PROBE_DURATION(garbage) is None


class TestFetchSource:
    def test_happy_path_youtube(self, make_source, fake_ytdlp, settings, clock):
        sid = make_source(url="https://www.youtube.com/watch?v=abc123", licence=None, licence_evidence=None,
                          metadata={"scout": {"query": "NASA Artemis"}})
        fake = fake_ytdlp(yt_info())
        path = fetch.fetch_source(sid)

        assert path == (settings.downloads_dir / str(sid) / "video.mp4").resolve()
        assert path.is_file() and path.is_absolute()
        assert [c[1] for c in fake.calls] == [False, True]  # probe first, then download
        assert fake.calls[1][2] == settings.downloads_dir / str(sid)

        src = db.require_source(sid)
        assert src.status == "fetched"
        assert src.licence == LICENCE_US_GOV
        assert src.licence_evidence["channel_id"] == NASA_CHANNEL_ID
        assert src.video_path == str(path)
        assert src.duration_sec == 900
        assert src.title == "Artemis II Crew Q&A"
        assert src.external_id == "abc123"
        assert src.platform == "youtube"
        assert src.published_at == "2026-11-10T00:00:00Z"
        assert src.metadata["scout"] == {"query": "NASA Artemis"}  # existing metadata preserved
        assert src.metadata["fetch"]["height"] == 1080
        assert src.metadata["fetch"]["fetched_at"] == "2026-11-13T15:00:00Z"

        info = json.loads((path.parent / "info.json").read_text())
        assert info["id"] == "abc123" and "formats" not in info
        events = db.list_events(module="fetch")
        assert any("fetched source" in e.message for e in events)

    def test_rejected_licence_never_downloads(self, make_source, fake_ytdlp, settings):
        sid = make_source(source_type="cc_by_youtube", url="https://www.youtube.com/watch?v=cc0001")
        fake = fake_ytdlp(cc_info(license="Creative Commons Attribution-NonCommercial"))
        with pytest.raises(LicenceRejected) as exc:
            fetch.fetch_source(sid)
        assert fake.downloads == 0
        assert not (settings.downloads_dir / str(sid) / "video.mp4").exists()
        assert exc.value.decision is not None and not exc.value.decision.ok
        src = db.require_source(sid)
        assert src.status == "rejected"
        assert "NonCommercial" in src.status_reason
        assert src.licence is None  # make_source's default licence is cleared
        assert src.licence_evidence["license"] == "Creative Commons Attribution-NonCommercial"
        ev = db.list_events(module="fetch", level="warning")
        assert ev and ev[0].data["evidence"]["license"] == "Creative Commons Attribution-NonCommercial"

    def test_cspan_rejected_before_download(self, make_source, fake_ytdlp):
        sid = make_source(source_type="congress")
        fake = fake_ytdlp(yt_info(channel="C-SPAN", uploader="C-SPAN", channel_id="UCcspan", uploader_id="@cspan"))
        with pytest.raises(LicenceRejected):
            fetch.fetch_source(sid)
        assert fake.downloads == 0
        assert db.require_source(sid).status == "rejected"

    def test_cc_by_happy_path(self, make_source, fake_ytdlp):
        sid = make_source(source_type="cc_by_youtube", url="https://www.youtube.com/watch?v=cc0001", licence=None)
        fake_ytdlp(cc_info())
        fetch.fetch_source(sid)
        src = db.require_source(sid)
        assert src.status == "fetched" and src.licence == LICENCE_CC_BY
        assert src.channel == "Some Science Channel"

    def test_direct_nasa_file(self, make_source, fake_ytdlp, monkeypatch):
        url = "https://images-assets.nasa.gov/video/KSC-2026-launch/KSC-2026-launch~orig.mp4"
        sid = make_source(url=url, platform="nasa_images", channel="NASA", channel_id=None, duration_sec=None)
        fake_ytdlp(nasa_file_info())
        monkeypatch.setattr(fetch, "probe_duration", lambda p: 312.5)
        path = fetch.fetch_source(sid)
        src = db.require_source(sid)
        assert src.status == "fetched" and path.is_file()
        assert src.platform == "nasa_images"
        assert src.duration_sec == 312.5
        assert src.channel == "NASA"  # kept from scout when metadata has none

    def test_direct_nasa_file_uses_stored_scout_description(self, make_source, fake_ytdlp, settings):
        url = "https://images-assets.nasa.gov/video/KSC-2026-launch/KSC-2026-launch~orig.mp4"
        sid = make_source(url=url, platform="nasa_images", description="Launch replay. Music: Killer Tracks",
                          licence=None)
        fake = fake_ytdlp(nasa_file_info())          # generic extractor: no description at all
        with pytest.raises(LicenceRejected):
            fetch.fetch_source(sid)
        assert fake.downloads == 0
        src = db.require_source(sid)
        assert src.status == "rejected" and "music" in src.status_reason
        sid2 = make_source(url=url + "?2", platform="nasa_images", description="Liftoff of Artemis II.",
                           metadata={"secondary_creator": "NASA/Joel Kowsky (c) 2026 Some Studio"})
        with pytest.raises(LicenceRejected):
            fetch.fetch_source(sid2)
        assert "copyright" in db.require_source(sid2).status_reason

    def test_licence_info_is_pure_and_deduplicated(self, make_source):
        src = db.require_source(make_source(description="Same text", metadata={"secondary_creator": "NASA"}))
        info = {"description": "Same text"}
        merged = fetch.licence_info(info, src)
        assert merged["description"] == "Same text\n\nsecondary creator: NASA" and info == {"description": "Same text"}
        assert fetch.licence_info({"description": "x NASA y Same text"}, src)["description"] == "x NASA y Same text"

    def test_direct_file_too_long_after_download_rejected(self, make_source, fake_ytdlp, monkeypatch, settings):
        sid = make_source(url="https://images-assets.nasa.gov/video/x/x~orig.mp4")
        fake_ytdlp(nasa_file_info())
        monkeypatch.setattr(fetch, "probe_duration", lambda p: 6 * 3600.0)
        with pytest.raises(LicenceRejected):
            fetch.fetch_source(sid)
        assert db.require_source(sid).status == "rejected"
        assert not (settings.downloads_dir / str(sid) / "video.mp4").exists()

    def test_download_metadata_rechecked(self, make_source, fake_ytdlp, settings):
        sid = make_source(source_type="cc_by_youtube")
        # licence switched between probe and download
        fake_ytdlp(cc_info(), downloaded=cc_info(license="Standard YouTube License"))
        with pytest.raises(LicenceRejected):
            fetch.fetch_source(sid)
        assert db.require_source(sid).status == "rejected"
        assert not (settings.downloads_dir / str(sid) / "video.mp4").exists()

    def test_probe_error_marks_failed(self, make_source, fake_ytdlp):
        sid = make_source()
        fake = fake_ytdlp(yt_info(), probe_error=RuntimeError("ERROR: [youtube] abc123: Video unavailable"))
        with pytest.raises(FetchError, match="Video unavailable"):
            fetch.fetch_source(sid)
        assert fake.downloads == 0
        src = db.require_source(sid)
        assert src.status == "failed" and "Video unavailable" in src.status_reason
        assert db.list_events(module="fetch", level="error")

    def test_age_gate_probe_error_is_rejection(self, make_source, fake_ytdlp):
        sid = make_source()
        fake_ytdlp(yt_info(), probe_error=RuntimeError("Sign in to confirm your age. This video may be inappropriate"))
        with pytest.raises(LicenceRejected):
            fetch.fetch_source(sid)
        src = db.require_source(sid)
        assert src.status == "rejected" and "age-restricted" in src.status_reason

    def test_download_error_marks_failed(self, make_source, fake_ytdlp):
        sid = make_source()
        fake_ytdlp(yt_info(), download_error=RuntimeError("HTTP Error 403: Forbidden"))
        with pytest.raises(FetchError, match="403"):
            fetch.fetch_source(sid)
        src = db.require_source(sid)
        assert src.status == "failed" and "download failed" in src.status_reason
        assert src.licence_evidence == {"uploader": "NASA"}  # untouched by a failed download

    def test_download_without_file_marks_failed(self, make_source, fake_ytdlp):
        sid = make_source()
        fake_ytdlp(yt_info(), write_file=False)
        with pytest.raises(FetchError, match="no non-empty mp4"):
            fetch.fetch_source(sid)
        assert db.require_source(sid).status == "failed"

    def test_non_default_download_filename_is_moved(self, make_source, monkeypatch, settings):
        sid = make_source()

        def fake(url: str, *, download: bool, outdir: Path | None = None) -> dict[str, Any]:
            if not download:
                return yt_info()
            assert outdir is not None
            other = outdir / "video.f137+f140.mp4"
            other.write_bytes(b"data")
            return yt_info(requested_downloads=[{"filepath": str(other)}])

        monkeypatch.setattr(fetch, "extract_info", fake)
        path = fetch.fetch_source(sid)
        assert path.name == "video.mp4" and path.read_bytes() == b"data"

    def test_duplicate_fetch_is_idempotent(self, make_source, fake_ytdlp):
        sid = make_source()
        fake = fake_ytdlp(yt_info())
        first = fetch.fetch_source(sid)
        second = fetch.fetch_source(sid)
        assert first == second
        assert len(fake.calls) == 2  # second call did no probing/downloading

    def test_fetched_but_file_missing_raises(self, make_source, fake_ytdlp):
        sid = make_source(status="fetched")
        db.update_source(sid, video_path="/nonexistent/video.mp4")
        fake = fake_ytdlp(yt_info())
        with pytest.raises(FetchError, match="missing"):
            fetch.fetch_source(sid)
        assert fake.calls == []
        assert db.require_source(sid).status == "fetched"

    def test_rejected_source_raises_without_work(self, make_source, fake_ytdlp):
        sid = make_source()
        db.set_source_status(sid, "rejected", "licence NC")
        fake = fake_ytdlp(yt_info())
        with pytest.raises(LicenceRejected, match="licence NC"):
            fetch.fetch_source(sid)
        assert fake.calls == []

    def test_failed_source_requires_reset(self, make_source, fake_ytdlp):
        sid = make_source(status="failed")
        fake = fake_ytdlp(yt_info())
        with pytest.raises(FetchError, match="only candidates"):
            fetch.fetch_source(sid)
        assert fake.calls == [] and db.require_source(sid).status == "failed"
        # retry path: failed -> candidate -> fetched
        db.set_source_status(sid, "candidate")
        fetch.fetch_source(sid)
        assert db.require_source(sid).status == "fetched"

    def test_unknown_source_raises_not_found(self):
        with pytest.raises(db.NotFound):
            fetch.fetch_source(9999)

    def test_resume_reuses_complete_video(self, make_source, fake_ytdlp, settings):
        sid = make_source()
        d = settings.downloads_dir / str(sid)
        d.mkdir(parents=True)
        (d / "video.mp4").write_bytes(b"complete video from an interrupted run")
        fake = fake_ytdlp(yt_info())
        fetch.fetch_source(sid)
        assert fake.downloads == 0 and len(fake.calls) == 1  # licence still probed
        assert db.require_source(sid).status == "fetched"

    def test_resume_still_checks_licence(self, make_source, fake_ytdlp, settings):
        sid = make_source(source_type="cc_by_youtube")
        d = settings.downloads_dir / str(sid)
        d.mkdir(parents=True)
        (d / "video.mp4").write_bytes(b"x")
        fake_ytdlp(cc_info(license=None))
        with pytest.raises(LicenceRejected):
            fetch.fetch_source(sid)

    def test_explicit_strategy_argument(self, make_source, fake_ytdlp, strategy):
        sid = make_source()
        fake_ytdlp(yt_info())
        narrowed = strategy.updated(blocklist_channels=["NASA"])
        with pytest.raises(LicenceRejected):
            fetch.fetch_source(sid, strategy=narrowed)


class TestExtractInfoWrapper:
    def test_options_cap_resolution_and_merge(self, tmp_path):
        opts = fetch._ydl_options(True, tmp_path)
        assert "height<=?1080" in opts["format"] and "+ba" in opts["format"]
        assert opts["merge_output_format"] == "mp4"
        assert opts["noplaylist"] is True
        assert opts["outtmpl"]["default"].endswith("video.%(ext)s")

    def test_download_requires_outdir(self):
        with pytest.raises(ValueError):
            fetch._ydl_options(True, None)

    def test_extract_info_uses_yt_dlp(self, monkeypatch, tmp_path):
        import yt_dlp

        seen: dict[str, Any] = {}

        class FakeYDL:
            def __init__(self, opts):
                seen["opts"] = opts

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download):
                seen["args"] = (url, download)
                return {"id": "x", "webpage_url": url}

            def sanitize_info(self, info):
                return dict(info, sanitized=True)

        monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
        out = fetch.extract_info("https://www.youtube.com/watch?v=x", download=False)
        assert out["sanitized"] and seen["args"] == ("https://www.youtube.com/watch?v=x", False)
        assert "outtmpl" not in seen["opts"]


def test_import_has_no_heavy_imports():
    import subprocess
    import sys

    code = "import sys, fetch; assert 'yt_dlp' not in sys.modules, 'yt_dlp imported at module import'"
    subprocess.run([sys.executable, "-c", code], check=True, cwd=Path(fetch.__file__).parent)
