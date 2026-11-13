"""Tests for analytics.py with injected fake YouTube Data / Analytics services. Fully offline."""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import httplib2
import pytest
from googleapiclient.errors import HttpError

import analytics
import config
import db
import youtube_auth

CORE = list(analytics.CORE_METRICS)
ENG = list(analytics.ENGAGEMENT_METRICS)


def http_error(status: int, reason: str = "badRequest") -> HttpError:
    content = json.dumps({"error": {"code": status, "message": reason, "errors": [{"reason": reason}]}}).encode()
    return HttpError(httplib2.Response({"status": status}), content)


class _Exec:
    def __init__(self, fn: Callable[[], Any]):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeAnalytics:
    """``reports().query(**kw).execute()`` -> ``handler(kw)`` (dict, or raise)."""

    def __init__(self, handler: Callable[[dict], Any]):
        self.handler = handler
        self.calls: list[dict] = []

    def reports(self):
        outer = self

        class R:
            def query(self, **kw):
                outer.calls.append(kw)
                return _Exec(lambda: outer.handler(kw))
        return R()


class FakeData:
    def __init__(self):
        self.items: dict[str, dict] = {}       # id -> item (status/contentDetails/statistics)
        self.channel: dict = {"items": [{"id": "UC1", "statistics": {
            "subscriberCount": "120", "viewCount": "4567", "videoCount": "9", "hiddenSubscriberCount": False}}]}
        self.calls: list[tuple[str, dict]] = []
        self.videos_error: Exception | None = None

    def videos(self):
        outer = self

        class V:
            def list(self, **kw):
                outer.calls.append(("videos.list", kw))

                def run():
                    if outer.videos_error:
                        raise outer.videos_error
                    ids = kw["id"].split(",")
                    return {"items": [dict(outer.items[i], id=i) for i in ids if i in outer.items]}
                return _Exec(run)
        return V()

    def channels(self):
        outer = self

        class C:
            def list(self, **kw):
                outer.calls.append(("channels.list", kw))
                return _Exec(lambda: outer.channel)
        return C()


def video_rows(metrics: list[str], values: dict[str, list[float]]) -> dict:
    return {"columnHeaders": [{"name": "video"}] + [{"name": m} for m in metrics],
            "rows": [[vid, *vals] for vid, vals in values.items()]}


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    config.reset_settings()


@pytest.fixture
def services():
    data = FakeData()
    holder: dict[str, Any] = {"data": data, "an": FakeAnalytics(lambda kw: {"columnHeaders": [], "rows": []})}
    analytics.set_service_factory(lambda: (holder["data"], holder["an"]))
    yield holder
    analytics.set_service_factory(None)


@pytest.fixture
def videos(make_upload):
    """Two live videos published 2026-11-01 and one dry-run upload (ignored)."""
    make_upload(video_id="vA", publish_at="2026-11-01T17:15:00Z")
    make_upload(video_id="vB", publish_at="2026-11-01T22:10:00Z")
    make_upload(video_id="dryrun-99", dry_run=True)
    return ["vA", "vB"]


def standard_handler(scale: int = 1):
    def h(kw):
        d = int(kw["startDate"][-2:])
        if kw.get("dimensions") == "video":
            ids = kw["filters"].removeprefix("video==").split(",")
            if kw["metrics"] == ",".join(CORE):
                return video_rows(CORE, {v: [d * scale, d * 1.5, 30.0, 66.6] for v in ids if v == "vA"})
            return video_rows(ENG, {v: [d, 1, 2, 3, 0] for v in ids if v == "vA"})
        if kw.get("dimensions") == "elapsedVideoTimeRatio":
            return {"columnHeaders": [{"name": "elapsedVideoTimeRatio"}, {"name": "audienceWatchRatio"}],
                    "rows": [[0.02, 1.1], [0.01, 1.2], [1.0, 0.3]]}
        raise AssertionError(kw)
    return h


# ---------------------------------------------------------------------------

def test_dry_run_returns_zero(clock, videos, services):
    assert analytics.collect_stats() == 0
    assert analytics.check_claims() == 0
    analytics.refresh_channel_stats()
    assert services["an"].calls == [] and services["data"].calls == []
    assert db.list_channel_stats() == []


def test_no_token_returns_zero(clock, live, videos):
    analytics.set_service_factory(None)
    assert analytics.collect_stats() == 0
    assert [n.kind for n in db.list_notifications()] == ["youtube_auth"]


def test_collect_backfills_window(clock, live, videos, services):
    services["an"].handler = standard_handler()
    n = analytics.collect_stats()   # default day = today - 2 = 2026-11-11; window 9..11
    assert n == 6
    days = sorted({s.day for s in db.stats_between("2026-11-01", "2026-11-30")})
    assert days == ["2026-11-09", "2026-11-10", "2026-11-11"]
    a11 = [s for s in db.get_stats("vA") if s.day == "2026-11-11"][0]
    assert (a11.views, a11.watch_minutes, a11.avg_view_duration_sec, a11.avg_view_pct) == (11, 16.5, 30.0, 66.6)
    assert (a11.likes, a11.comments, a11.shares, a11.subscribers_gained) == (11, 1, 2, 3)
    b11 = [s for s in db.get_stats("vB") if s.day == "2026-11-11"][0]
    assert b11.views == 0 and b11.avg_view_pct is None   # absent from the report = no views that day
    # each core/engagement query is a one-day dimensions=video query filtered to our ids
    q = services["an"].calls[0]
    assert q["ids"] == "channel==MINE" and q["startDate"] == q["endDate"] == "2026-11-09"
    assert q["filters"] == "video==vA,vB" and q["sort"] == "-views"
    # revised numbers on the next run overwrite (upsert), no duplicates
    services["an"].handler = standard_handler(scale=10)
    analytics.collect_stats()
    assert [s.views for s in db.get_stats("vA") if s.day == "2026-11-11"] == [110]
    assert len(db.get_stats("vA")) == 3


def test_videos_not_live_yet_are_skipped(clock, live, make_upload, services):
    make_upload(video_id="vNew", publish_at="2026-11-10T17:15:00Z")
    services["an"].handler = standard_handler()
    analytics.collect_stats()
    assert [s.day for s in db.get_stats("vNew")] == ["2026-11-10", "2026-11-11"]


def test_partial_report_failure_keeps_previous_values(clock, live, videos, services):
    db.upsert_stat("vA", "2026-11-11", views=5, likes=42, comments=7, shares=3, subscribers_gained=2)
    base = standard_handler()

    def h(kw):
        if kw.get("dimensions") == "video" and kw["metrics"] == ",".join(ENG):
            raise http_error(500, "internalError")
        return base(kw)
    services["an"].handler = h
    n = analytics.collect_stats()
    assert n == 6
    a = [s for s in db.get_stats("vA") if s.day == "2026-11-11"][0]
    assert a.views == 11            # core report landed
    assert (a.likes, a.comments, a.shares, a.subscribers_gained) == (42, 7, 3, 2)   # engagement kept
    warn = [e for e in db.list_events(module="analytics") if e.level == "warning"]
    assert any("engagement" in e.message for e in warn)


def test_both_reports_failing_writes_nothing(clock, live, videos, services):
    services["an"].handler = lambda kw: (_ for _ in ()).throw(http_error(503, "backendError"))
    assert analytics.collect_stats() == 0
    assert db.get_stats("vA") == []


def test_400_falls_back_to_per_video_daily(clock, live, videos, services):
    def h(kw):
        if kw.get("dimensions") == "video":
            raise http_error(400, "badRequest")
        if kw.get("dimensions") == "day":
            vid = kw["filters"].removeprefix("video==")
            metrics = kw["metrics"].split(",")
            return {"columnHeaders": [{"name": "day"}] + [{"name": m} for m in metrics],
                    "rows": [[kw["startDate"]] + [7 if vid == "vB" else 1] * len(metrics)]}
        return {"rows": []}
    services["an"].handler = h
    assert analytics.collect_stats(date(2026, 11, 11)) == 6
    b = [s for s in db.get_stats("vB") if s.day == "2026-11-11"][0]
    assert b.views == 7 and b.likes == 7


def test_retention_fetched_sparingly(clock, live, videos, services):
    services["an"].handler = standard_handler()
    analytics.collect_stats()
    ret_calls = [c for c in services["an"].calls if c.get("dimensions") == "elapsedVideoTimeRatio"]
    assert len(ret_calls) == 2 and ret_calls[0]["filters"] == "video==vA"
    assert ret_calls[0]["startDate"] == "2026-11-01" and ret_calls[0]["metrics"] == "audienceWatchRatio"
    a = [s for s in db.get_stats("vA") if s.day == "2026-11-11"][0]
    assert a.retention == [[0.01, 1.2], [0.02, 1.1], [1.0, 0.3]]
    # next day: not refreshed (weekly), but the curve is carried onto the new day's row
    clock.advance(days=1)
    services["an"].calls.clear()
    analytics.collect_stats()
    assert not [c for c in services["an"].calls if c.get("dimensions") == "elapsedVideoTimeRatio"]
    a12 = [s for s in db.get_stats("vA") if s.day == "2026-11-12"][0]
    assert a12.retention == a.retention and a12.views == 12
    clock.advance(days=7)
    services["an"].calls.clear()
    analytics.collect_stats()
    assert [c for c in services["an"].calls if c.get("dimensions") == "elapsedVideoTimeRatio"]


def test_retention_failure_is_isolated(clock, live, videos, services):
    base = standard_handler()

    def h(kw):
        if kw.get("dimensions") == "elapsedVideoTimeRatio":
            raise http_error(400, "badRequest")
        return base(kw)
    services["an"].handler = h
    assert analytics.collect_stats() == 6
    assert all(s.retention is None for s in db.get_stats("vA"))


def test_lifetime_counters_cached(clock, live, videos, services):
    services["data"].items = {"vA": {"statistics": {"viewCount": "1000", "likeCount": "50", "commentCount": "4"}},
                               "vB": {"statistics": {"viewCount": "7"}}}
    analytics.collect_stats()
    assert analytics.lifetime_stats("vA")["views"] == 1000
    assert analytics.lifetime_stats("vB") == {"views": 7, "likes": 0, "comments": 0,
                                              "fetched_at": "2026-11-13T15:00:00Z"}
    assert youtube_auth.quota_used() == 1   # one videos.list for 2 ids


def test_refresh_channel_stats(clock, live, services):
    analytics.refresh_channel_stats()
    [row] = db.list_channel_stats()
    assert (row.day, row.subscribers, row.total_views, row.video_count) == ("2026-11-13", 120, 4567, 9)
    assert services["data"].calls[0][1] == {"part": "statistics", "mine": True}


def test_check_claims_flags_and_notifies(clock, live, make_upload, services):
    for vid in ("ok", "rej", "blk", "gone", "fail"):
        make_upload(video_id=vid, publish_at="2026-11-12T17:15:00Z")
    services["data"].items = {
        "ok": {"status": {"uploadStatus": "processed", "privacyStatus": "public"}, "contentDetails": {}},
        "rej": {"status": {"uploadStatus": "rejected", "rejectionReason": "claim"}, "contentDetails": {}},
        "blk": {"status": {"uploadStatus": "processed", "privacyStatus": "public"},
                "contentDetails": {"regionRestriction": {"blocked": ["DE", "FR"]}}},
        "fail": {"status": {"uploadStatus": "failed", "failureReason": "codec", "privacyStatus": "public"},
                 "contentDetails": {}},
    }
    assert analytics.check_claims() == 4
    assert db.get_upload_by_video("rej").copyright_claim and db.get_upload_by_video("rej").status == "removed"
    assert db.get_upload_by_video("blk").copyright_claim and db.get_upload_by_video("blk").status == "uploaded"
    assert db.get_upload_by_video("gone").status == "removed"
    assert not db.get_upload_by_video("fail").copyright_claim
    assert not db.get_upload_by_video("ok").copyright_claim
    notes = [n for n in db.list_notifications() if n.kind == "youtube_claim"]
    assert len(notes) == 4 and all(n.channel == "call" and n.status == "queued" for n in notes)
    call = [c for c in services["data"].calls if c[0] == "videos.list"][0][1]
    assert call["part"] == "status,contentDetails"
    # second run: nothing new
    assert analytics.check_claims() == 0
    assert len([n for n in db.list_notifications() if n.kind == "youtube_claim"]) == 4


def test_check_claims_detects_private_lock(clock, live, make_upload, services):
    make_upload(video_id="vP", publish_at="2026-11-12T17:15:00Z")
    services["data"].items = {"vP": {"status": {"uploadStatus": "processed", "privacyStatus": "private"},
                                      "contentDetails": {}}}
    assert analytics.check_claims() == 0
    notes = [n for n in db.list_notifications() if n.kind == "youtube_private_lock"]
    assert len(notes) == 1 and notes[0].channel == "sms"
    analytics.check_claims()
    assert len([n for n in db.list_notifications() if n.kind == "youtube_private_lock"]) == 1


def test_check_claims_list_failure_flags_nothing(clock, live, videos, services):
    services["data"].videos_error = http_error(500, "backendError")
    assert analytics.check_claims() == 0
    assert db.get_upload_by_video("vA").status == "uploaded"


# ---------------------------------------------------------------------------
# real googleapiclient request building against the bundled discovery docs
# ---------------------------------------------------------------------------

class RecordingHttp:
    """httplib2-compatible double that records URIs and answers with ``responder(uri)``."""

    def __init__(self, responder: Callable[[str], dict]):
        self.responder = responder
        self.uris: list[str] = []

    def request(self, uri, method="GET", body=None, headers=None, redirections=5, connection_type=None):
        self.uris.append(uri)
        return httplib2.Response({"status": "200"}), json.dumps(self.responder(uri)).encode()


def test_real_clients_accept_our_parameters(clock, live, videos):
    from googleapiclient.discovery import build

    def an_resp(uri):
        return {"columnHeaders": [], "rows": []}

    an_http = RecordingHttp(an_resp)
    data_http = RecordingHttp(lambda uri: {"items": []})
    an = build("youtubeAnalytics", "v2", http=an_http, static_discovery=True)
    yt = build("youtube", "v3", http=data_http, static_discovery=True)
    analytics.set_service_factory(lambda: (yt, an))
    try:
        analytics.collect_stats()
        analytics.refresh_channel_stats()
        analytics.check_claims()
    finally:
        analytics.set_service_factory(None)
    q = parse_qs(urlparse(an_http.uris[0]).query)
    assert q["ids"] == ["channel==MINE"] and q["dimensions"] == ["video"] and q["filters"] == ["video==vA,vB"]
    assert q["metrics"] == [",".join(CORE)] and q["maxResults"] == ["2"]
    assert any("elapsedVideoTimeRatio" in u for u in an_http.uris)
    assert any("/youtube/v3/videos?" in u and "part=statistics" in u for u in data_http.uris)
    assert any("/youtube/v3/channels?" in u and "mine=true" in u for u in data_http.uris)
