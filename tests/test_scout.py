"""Scout tests. The NASA API and yt-dlp payloads below are *recorded-shape* fixtures written from
the documented formats; they were not captured from the live services (the sandbox blocks them)."""

from __future__ import annotations

import pytest

import db
import scout
import strategy as strategy_mod

# --- NASA Image and Video Library: GET /search?q=...&media_type=video -----------------------
NASA_SEARCH = {
    "collection": {
        "version": "1.0",
        "href": "http://images-api.nasa.gov/search?q=Artemis&media_type=video",
        "items": [
            {
                "href": "https://images-assets.nasa.gov/video/KSC-20221116-MH-ART01-0001/collection.json",
                "data": [{
                    "center": "KSC", "title": "Artemis I Launch", "nasa_id": "KSC-20221116-MH-ART01-0001",
                    "media_type": "video", "date_created": "2022-11-16T00:00:00Z",
                    "keywords": ["Artemis I", "SLS", "Orion"],
                    "description": "NASA's Space Launch System rocket lifts off from Launch Pad 39B.",
                }],
                "links": [{"href": "https://images-assets.nasa.gov/video/KSC-20221116-MH-ART01-0001/"
                                   "KSC-20221116-MH-ART01-0001~thumb.jpg", "rel": "preview", "render": "image"}],
            },
            {
                "href": "https://images-assets.nasa.gov/video/JPL-20230101-MUSIC/collection.json",
                "data": [{"center": "JPL", "title": "Mission montage", "nasa_id": "JPL-20230101-MUSIC",
                          "media_type": "video", "date_created": "2023-01-01T00:00:00Z",
                          "description": "Music: Copyright 2022 Some Label. All rights reserved."}],
            },
            {
                "href": "https://images-assets.nasa.gov/video/jsc2023m000123/collection.json",
                "data": [{"center": "JSC", "title": "Artemis II crew answers questions",
                          "nasa_id": "jsc2023m000123", "media_type": "video",
                          "date_created": "2023-04-03T00:00:00Z", "description": "Crew Q&A."}],
            },
        ],
        "metadata": {"total_hits": 3},
        "links": [{"rel": "next", "prompt": "Next", "href": "http://images-api.nasa.gov/search?q=Artemis&page=2"}],
    }
}

# --- GET /asset/{nasa_id} ---------------------------------------------------------------------
def nasa_asset(nasa_id):
    base = f"http://images-assets.nasa.gov/video/{nasa_id}/{nasa_id}"
    return {"collection": {"version": "1.0", "href": f"http://images-api.nasa.gov/asset/{nasa_id}", "items": [
        {"href": base + "~orig.mp4"}, {"href": base + "~large.mp4"}, {"href": base + "~mobile.mp4"},
        {"href": base + "~preview.mp4"}, {"href": base + "~small.mp4"}, {"href": base + "~thumb.jpg"},
        {"href": base + ".srt"},
        {"href": f"http://images-assets.nasa.gov/video/{nasa_id}/metadata.json"},
    ]}}

# --- metadata.json (exiftool-style keys) ---------------------------------------------------------
NASA_META = {
    "KSC-20221116-MH-ART01-0001": {"QuickTime:Duration": "0:05:12", "File:FileType": "MP4"},
    "jsc2023m000123": {"Video:Duration": "1:02:03.5"},
}

# --- yt-dlp flat extraction of "ytsearchN:<query>" -------------------------------------------
def yt_entry(vid, title, channel, channel_id=None, duration=600.0, uploader_id=None, **extra):
    e = {"_type": "url", "ie_key": "Youtube", "id": vid, "url": f"https://www.youtube.com/watch?v={vid}",
         "title": title, "description": None, "duration": duration, "channel_id": channel_id,
         "channel": channel, "channel_url": f"https://www.youtube.com/channel/{channel_id}" if channel_id else None,
         "uploader": channel, "uploader_id": uploader_id, "uploader_url": None, "thumbnails": [],
         "timestamp": None, "release_timestamp": None, "view_count": 1234, "live_status": None}
    e.update(extra)
    return e


YT_SEARCH_CONGRESS = {
    "_type": "playlist", "id": "UAP hearing", "title": "UAP hearing", "extractor": "youtube:search",
    "entries": [
        yt_entry("AAAAAAAAAA1", "UAP hearing full", "House Committee on Oversight and Accountability",
                 "UCoversight0000000000000"),
        yt_entry("AAAAAAAAAA2", "UAP hearing (C-SPAN)", "C-SPAN", "UCb--64Gl51jIEVE-GLDAVTg"),
        yt_entry("AAAAAAAAAA3", "UAP hearing highlights", "Forbes Breaking News", "UCg40OxZ1GYh3u3jBntB6DLg"),
        yt_entry("AAAAAAAAAA4", "UAP hearing live", "House Committee on Oversight and Accountability",
                 "UCoversight0000000000000", live_status="is_live"),
        {"_type": "url", "ie_key": "YoutubeTab", "id": "UCsomechannel", "url": "https://www.youtube.com/channel/UCx",
         "title": "A channel"},
    ],
}

# --- yt-dlp flat extraction of a channel's /videos tab (channel fields at playlist level) ---------
YT_CHANNEL_NASA = {
    "_type": "playlist", "id": "UCLA_DiR1FfKNvjuUpBHmylQ", "title": "NASA - Videos", "channel": "NASA",
    "channel_id": "UCLA_DiR1FfKNvjuUpBHmylQ", "uploader_id": "@NASA", "uploader": "NASA",
    "entries": [
        {"_type": "url", "ie_key": "Youtube", "id": "NASAvid0001", "url": "https://www.youtube.com/watch?v=NASAvid0001",
         "title": "NASA Artemis II Crew News Conference", "duration": 2400, "view_count": 50000},
        {"_type": "url", "ie_key": "Youtube", "id": "NASAvid0002", "url": "https://www.youtube.com/watch?v=NASAvid0002",
         "title": "Hubble images of a galaxy", "duration": 300},
    ],
}

YT_CC = {
    "_type": "playlist", "id": "physics explained", "extractor": "youtube:search_url",
    "entries": [
        yt_entry("CCCCCCCCCC1", "Physics explained: entropy", "Some Educator", "UCeducator000000000000",
                 duration=900.0),
        yt_entry("CCCCCCCCCC2", "Physics short", "Some Educator", "UCeducator000000000000", duration=40.0),
    ],
}


@pytest.fixture
def transports(monkeypatch):
    calls = {"http": [], "ytdlp": []}

    def http(url, params=None):
        calls["http"].append((url, params))
        if url.endswith("/search"):
            return NASA_SEARCH
        if "/asset/" in url:
            return nasa_asset(url.rsplit("/", 1)[1])
        if url.endswith("/metadata.json"):
            nid = url.split("/video/")[1].split("/")[0]
            return NASA_META.get(nid, {})
        raise AssertionError(url)

    def flat(url, limit):
        calls["ytdlp"].append((url, limit))
        if url.startswith("ytsearch"):
            return YT_SEARCH_CONGRESS if "hearing" in url else {"entries": []}
        if "/@NASA/" in url or "UCLA_DiR1FfKNvjuUpBHmylQ" in url:
            return YT_CHANNEL_NASA
        if "results?search_query" in url:
            return YT_CC
        return {"entries": []}

    monkeypatch.setattr(scout, "http_get_json", http)
    monkeypatch.setattr(scout, "ytdlp_flat", flat)
    return calls


# ---------------------------------------------------------------------------
# searchers
# ---------------------------------------------------------------------------

def test_nasa_images_searcher(transports, strategy):
    cands = scout.search_nasa_images("Artemis", 5, strategy)
    assert [c.external_id for c in cands] == ["KSC-20221116-MH-ART01-0001", "jsc2023m000123"]  # copyright one skipped
    c = cands[0]
    assert c.url == ("https://images-assets.nasa.gov/video/KSC-20221116-MH-ART01-0001/"
                     "KSC-20221116-MH-ART01-0001~large.mp4")
    assert c.platform == "nasa_images" and c.channel == "NASA" and c.source_type == "nasa"
    assert c.duration_sec == 312.0 and cands[1].duration_sec == 3723.5
    assert c.published_at == "2022-11-16T00:00:00Z"
    assert c.metadata["licence_verified"] is False
    assert strategy_mod.is_publisher_allowed(strategy, "nasa", channel=c.channel, url=c.url)[0]
    search_call = transports["http"][0]
    assert search_call[1]["media_type"] == "video" and search_call[1]["q"] == "Artemis"


def test_official_youtube_searcher_filters_to_allowlist(transports, strategy):
    cands = scout.SEARCHERS["congress"]("UAP hearing", 5, strategy)
    ids = {c.external_id for c in cands}
    assert "AAAAAAAAAA1" in ids
    assert not ids & {"AAAAAAAAAA2", "AAAAAAAAAA3"}  # C-SPAN, news outlet
    assert all(c.url.startswith("https://www.youtube.com/watch?v=") for c in cands)


def test_nasa_searcher_uses_channel_uploads(transports, strategy):
    cands = scout.search_nasa("Artemis crew", 5, strategy)
    yt = [c for c in cands if c.platform == "youtube"]
    assert [c.external_id for c in yt] == ["NASAvid0001"]  # title match only
    assert yt[0].channel == "NASA" and yt[0].channel_id == "UCLA_DiR1FfKNvjuUpBHmylQ" and yt[0].handle == "@NASA"


def test_nasa_searcher_survives_one_half_failing(transports, strategy, monkeypatch):
    def boom(*a):
        raise RuntimeError("nasa api down")
    monkeypatch.setattr(scout, "search_nasa_images", boom)
    cands = scout.search_nasa("Artemis crew", 5, strategy)
    assert [c.external_id for c in cands] == ["NASAvid0001"]


def test_cc_searcher_uses_cc_filter_and_never_verifies(transports, strategy):
    cands = scout.search_cc_youtube("physics explained", 5, strategy)
    url = transports["ytdlp"][0][0]
    assert "search_query=physics+explained" in url and "sp=EgIwAQ%253D%253D" in url
    assert all(c.metadata["licence_verified"] is False for c in cands)
    assert all(c.source_type == "cc_by_youtube" for c in cands)


def test_cc_data_api_backend_respects_daily_budget(monkeypatch, strategy, clock):
    monkeypatch.setenv("SCOUT_CC_BACKEND", "data_api")
    monkeypatch.setenv("YOUTUBE_API_KEY", "dummy")
    monkeypatch.setenv("SCOUT_CC_API_MAX_SEARCHES_PER_DAY", "1")
    calls = []

    def http(url, params=None):
        calls.append(url)
        if url.endswith("/search"):
            assert params["videoLicense"] == "creativeCommon" and params["type"] == "video"
            return {"items": [{"id": {"kind": "youtube#video", "videoId": "DDDDDDDDDD1"},
                               "snippet": {"publishedAt": "2026-10-01T12:00:00Z", "channelId": "UCx",
                                           "title": "Entropy", "description": "", "channelTitle": "Educator"}}]}
        return {"items": [{"id": "DDDDDDDDDD1", "contentDetails": {"duration": "PT12M5S"},
                           "status": {"license": "creativeCommon"}}]}

    monkeypatch.setattr(scout, "http_get_json", http)
    first = scout.search_cc_by_youtube("entropy", 5, strategy)
    assert [c.duration_sec for c in first] == [725.0]
    assert scout.search_cc_by_youtube("entropy", 5, strategy) == []  # budget used
    assert len(calls) == 2


def test_parse_duration():
    assert scout.parse_duration("0:05:12") == 312
    assert scout.parse_duration("204.0 s") == 204
    assert scout.parse_duration("PT1H2M3S") == 3723
    assert scout.parse_duration("garbage") is None
    assert scout.parse_duration(0) is None


# ---------------------------------------------------------------------------
# scout()
# ---------------------------------------------------------------------------

def C(vid, query, *, stype="cc_by_youtube", duration=600.0, channel="Some Educator", published="2026-10-01T00:00:00Z",
      url=None):
    return scout.Candidate(url=url or scout.canonical_youtube_url(vid), source_type=stype, platform="youtube",
                           external_id=vid, channel=channel, channel_id=None, title=f"title {vid}",
                           duration_sec=duration, published_at=published, topic=query, metadata={})


@pytest.fixture
def only(monkeypatch):
    """Replace all searchers; returns a setter for per-type functions."""
    for k in list(scout.SEARCHERS):
        monkeypatch.setitem(scout.SEARCHERS, k, lambda q, n, s: [])

    def set_(stype, fn):
        monkeypatch.setitem(scout.SEARCHERS, stype, fn)
    return set_


def test_scout_inserts_candidates_without_licence(only, clock):
    only("cc_by_youtube", lambda q, n, s: [C(f"v{abs(hash(q)) % 10**9:010d}", q)])
    ids = scout.scout()
    assert len(ids) == 4  # four topics use cc_by_youtube
    src = db.require_source(ids[0])
    assert src.status == "candidate" and src.licence is None
    assert src.metadata["licence_verified"] is False
    assert src.topic in {t["query"] for t in strategy_mod.load_strategy().topics}
    events = db.list_events(module="scout")
    assert len(events) == 1 and events[0].data["new"] == 4


def test_scout_duration_and_publisher_filters(only, clock):
    def cc(q, n, s):
        if q != "physics explained":
            return []
        return [C("short000001", q, duration=30.0), C("long0000001", q, duration=4 * 3600.0),
                C("cspan000001", q, channel="C-SPAN"), C("good0000001", q), C("nodur000001", q, duration=None)]
    only("cc_by_youtube", cc)
    only("congress", lambda q, n, s: [C("notallowed1", q, stype="congress", channel="Random Vlogger")])
    ids = scout.scout()
    got = {db.require_source(i).external_id for i in ids}
    assert got == {"good0000001", "nodur000001"}
    dropped = db.list_events(module="scout")[0].data["dropped"]
    assert dropped["duration"] == 2 and dropped["publisher"] >= 2


def test_scout_dedupes_against_db_and_url_variants(only, clock, make_source):
    make_source(url="https://youtu.be/known000001", source_type="cc_by_youtube", external_id=None)
    make_source(url="https://www.youtube.com/watch?v=reject00001", source_type="cc_by_youtube",
                external_id="reject00001", status="rejected")
    make_source(url="https://example.org/other", source_type="cc_by_youtube", platform="youtube",
                external_id="extid000001")

    def cc(q, n, s):
        if q != "physics explained":
            return []
        return [C("known000001", q), C("reject00001", q), C("extid000001", q), C("fresh000001", q),
                C("fresh000001", q, url="https://www.youtube.com/watch?v=fresh000001&t=5")]
    only("cc_by_youtube", cc)
    ids = scout.scout()
    assert [db.require_source(i).external_id for i in ids] == ["fresh000001"]
    assert scout.scout() == []  # second run finds nothing new


def test_scout_spreads_across_topics(only, clock):
    def nasa(q, n, s):
        if q == "NASA Artemis":
            return [C(f"art{i:08d}", q, stype="nasa", channel="NASA") for i in range(20)]
        return [C(f"o{abs(hash(q)) % 10**10:010d}", q, stype="nasa", channel="NASA")]
    only("nasa", nasa)
    ids = scout.scout(max_candidates=10)
    topics = [db.require_source(i).topic for i in ids]
    assert len(ids) == 10
    assert topics.count("NASA Artemis") <= 4  # ceil(10 * 0.4)
    assert len(set(topics)) >= 6


def test_scout_prefers_recent(only, clock):
    def cc(q, n, s):
        if q != "physics explained":
            return []
        return [C("old00000001", q, published="2020-01-01T00:00:00Z"), C("undated0001", q, published=None),
                C("new00000001", q, published="2026-11-01T00:00:00Z")]
    only("cc_by_youtube", cc)
    ids = scout.scout(max_candidates=5)  # per-topic share = ceil(5 * 0.4) = 2
    assert [db.require_source(i).external_id for i in ids] == ["new00000001", "old00000001"]


def test_scout_one_searcher_failing(only, clock):
    def broken(q, n, s):
        raise ConnectionError("blocked")
    only("nasa", broken)
    only("congress", lambda q, n, s: [C(f"c{abs(hash(q)) % 10**10:010d}", q, stype="congress",
                                         channel="House Committee on Oversight and Accountability")])
    ids = scout.scout()
    assert len(ids) == 4  # four congress topics
    ev = db.list_events(module="scout")[0]
    assert ev.data["error_count"] == scout.MAX_SEARCHER_FAILURES  # nasa disabled after repeated failures
    assert ev.data["dropped"]["searcher_disabled"] > 0


def test_scout_cap(only, clock):
    only("cc_by_youtube", lambda q, n, s: [C(f"{q[:3]}{i:08d}".replace(" ", "_"), q) for i in range(10)])
    assert len(scout.scout(max_candidates=3)) == 3
    assert scout.scout(max_candidates=0) == []


def test_scout_with_fake_transports_end_to_end(transports, clock):
    ids = scout.scout()
    srcs = [db.require_source(i) for i in ids]
    assert srcs and len(srcs) <= scout.DEFAULT_MAX_CANDIDATES
    assert not any(s.channel in ("C-SPAN", "Forbes Breaking News") for s in srcs)
    assert all(s.licence is None for s in srcs)
    # the NASA channel tab is fetched once per run, not once per topic
    tabs = [u for u, _ in transports["ytdlp"] if "/videos" in u]
    assert len(tabs) == len(set(tabs))
