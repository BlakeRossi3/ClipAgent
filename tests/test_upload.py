"""Tests for upload.py (and the shared youtube_auth helpers it relies on). Fully offline."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httplib2
import pytest
from googleapiclient.errors import HttpError

import config
import db
import strategy as strategy_mod
import upload
import youtube_auth


# ---------------------------------------------------------------------------
# helpers / fakes
# ---------------------------------------------------------------------------

def http_error(status: int, reason: str, message: str = "boom") -> HttpError:
    content = json.dumps({"error": {"code": status, "message": message,
                                    "errors": [{"reason": reason, "message": message}]}}).encode()
    return HttpError(httplib2.Response({"status": status}), content)


class _Exec:
    def __init__(self, value: Any):
        self.value = value

    def execute(self):
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


class FakeInsertRequest:
    def __init__(self, outcomes: list[Any]):
        self.outcomes = outcomes

    def next_chunk(self):
        o = self.outcomes.pop(0)
        if isinstance(o, BaseException):
            raise o
        return o


class FakeYouTube:
    """Minimal youtube v3 double: videos().insert/list, channels().list, playlistItems().list."""

    def __init__(self, outcomes: list[Any] | None = None):
        self.outcomes = outcomes if outcomes is not None else [(None, {"id": "vid123", "status": {}})]
        self.insert_calls: list[dict] = []
        self.channels_resp: Any = {"items": [{"id": "UC1", "contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]}
        self.playlist_resp: Any = {"items": []}
        self.videos_list_resp: Any = {"items": []}
        self.list_calls: list[tuple[str, dict]] = []

    def videos(self):
        outer = self

        class V:
            def insert(self, **kw):
                outer.insert_calls.append(kw)
                return FakeInsertRequest(outer.outcomes)

            def list(self, **kw):
                outer.list_calls.append(("videos", kw))
                return _Exec(outer.videos_list_resp)
        return V()

    def channels(self):
        outer = self

        class C:
            def list(self, **kw):
                outer.list_calls.append(("channels", kw))
                return _Exec(outer.channels_resp)
        return C()

    def playlistItems(self):
        outer = self

        class P:
            def list(self, **kw):
                outer.list_calls.append(("playlistItems", kw))
                return _Exec(outer.playlist_resp)
        return P()


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(upload, "_sleep", sleeps.append)
    monkeypatch.setattr(upload, "probe_video", lambda path, settings=None: (1080, 1920, 45.0))
    upload.set_service_factory(None)
    yield sleeps
    upload.set_service_factory(None)


@pytest.fixture
def sleeps(_reset):
    return _reset


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    config.reset_settings()
    return config.get_settings()


@pytest.fixture
def fake_yt():
    yt = FakeYouTube()
    upload.set_service_factory(lambda: yt)
    return yt


@pytest.fixture
def rendered_clip(make_clip, settings, tmp_path):
    def _make(**kw) -> int:
        cid = make_clip(status="rendered", **kw)
        p = settings.renders_dir / f"{cid}.mp4"
        p.write_bytes(b"\x00" * 2048)
        db.update_clip(cid, render_path=str(p))
        return cid
    return _make


def write_strategy(**changes) -> None:
    s = strategy_mod.load_strategy()
    data = dict(s)
    data.update(changes)
    Path(config.get_settings().strategy_path).write_text(json.dumps(data), encoding="utf-8")


def Z(text: str) -> datetime:
    return db.parse_iso(text)


# ---------------------------------------------------------------------------
# slots
# ---------------------------------------------------------------------------

def test_next_slots_basic_crosses_utc_midnight(clock):
    # 10:00 New York; slots 08:45, 12:15, 17:10, 20:40 (EST = UTC-5)
    slots = upload.next_publish_slots(4)
    assert [db.to_iso(s) for s in slots] == [
        "2026-11-13T17:15:00Z", "2026-11-13T22:10:00Z", "2026-11-14T01:40:00Z", "2026-11-14T13:45:00Z"]
    assert all(s.tzinfo is not None and s.utcoffset() == timedelta(0) for s in slots)


def test_next_slots_strictly_after(clock):
    slots = upload.next_publish_slots(1, after=Z("2026-11-13T17:15:00Z"))
    assert db.to_iso(slots[0]) == "2026-11-13T22:10:00Z"


def test_next_slots_skip_taken_and_respect_daily_cap(clock, monkeypatch, make_upload):
    monkeypatch.setenv("MAX_UPLOADS_PER_DAY", "2")
    config.reset_settings()
    make_upload(publish_at="2026-11-13T17:15:00Z")
    slots = upload.next_publish_slots(3)
    # today: one taken + 17:10 = cap 2; tomorrow: 08:45, 12:15 (cap 2)
    assert [db.to_iso(s) for s in slots] == [
        "2026-11-13T22:10:00Z", "2026-11-14T13:45:00Z", "2026-11-14T17:15:00Z"]


def test_next_slots_failed_uploads_free_their_slot(clock, make_upload):
    make_upload(publish_at="2026-11-13T17:15:00Z", succeeded=False)
    uid = db.list_uploads()[0].id
    db.mark_upload_failed(uid, "x")
    assert db.to_iso(upload.next_publish_slots(1)[0]) == "2026-11-13T17:15:00Z"


def test_next_slots_cap_zero(clock):
    write_strategy(uploads_per_day=0)
    assert upload.next_publish_slots(3) == []


def test_next_slots_dst_spring_forward(clock):
    # US DST starts 2026-03-08: 08:45 EST = 13:45Z on Mar 7, 08:45 EDT = 12:45Z on Mar 8
    write_strategy(posting_times=["08:45"], uploads_per_day=1)
    slots = upload.next_publish_slots(2, after=Z("2026-03-07T12:00:00Z"))
    assert [db.to_iso(s) for s in slots] == ["2026-03-07T13:45:00Z", "2026-03-08T12:45:00Z"]


def test_next_slots_dst_gap_and_overlap(clock):
    # 02:30 does not exist on 2026-03-08 (shifted forward by the gap: 07:30Z = 03:30 EDT)
    write_strategy(posting_times=["02:30"], uploads_per_day=1)
    assert db.to_iso(upload.next_publish_slots(1, after=Z("2026-03-08T05:00:00Z"))[0]) == "2026-03-08T07:30:00Z"
    # 01:30 happens twice on 2026-11-01; first occurrence (EDT, 05:30Z)
    write_strategy(posting_times=["01:30"], uploads_per_day=1)
    assert db.to_iso(upload.next_publish_slots(1, after=Z("2026-11-01T04:00:00Z"))[0]) == "2026-11-01T05:30:00Z"


def test_next_slots_other_timezone_and_day_boundary(clock):
    # strategy in Tokyo (UTC+9, no DST): 23:59 local on the 13th -> next slot is the 14th 08:45 JST
    write_strategy(timezone="Asia/Tokyo", posting_times=["08:45", "23:30"], uploads_per_day=2)
    after = datetime(2026, 11, 13, 23, 59, tzinfo=strategy_mod.ZoneInfo("Asia/Tokyo"))
    slots = upload.next_publish_slots(2, after=after)
    assert [db.to_iso(s) for s in slots] == ["2026-11-13T23:45:00Z", "2026-11-14T14:30:00Z"]


# ---------------------------------------------------------------------------
# metadata
# ---------------------------------------------------------------------------

def _clip(make_clip, **kw) -> db.Clip:
    return db.require_clip(make_clip(status="rendered", **kw))


def test_title_truncated_on_word_boundary_keeps_shorts(make_clip):
    long = "Astronaut explains <why> the International Space Station needs constant reboosts " * 3
    t = upload.build_title(long)
    assert len(t) <= 100 and t.endswith("#Shorts")
    assert "<" not in t and ">" not in t
    base = t[: -len(" #Shorts")]
    assert long.replace("<", "").replace(">", "").startswith(base)
    assert not base.endswith(" ")
    nxt = long.replace("<", "").replace(">", "")[len(base)]
    assert nxt == " "  # cut on a word boundary


def test_title_existing_shorts_tag_and_fallback():
    assert upload.build_title("Moon landing #shorts") == "Moon landing #shorts"
    assert upload.build_title("   ", "Source title") == "Source title #Shorts"
    long = ("word " * 30) + "#Shorts"
    t = upload.build_title(long)
    assert len(t) <= 100 and t.count("#Shorts") == 1


def test_description_always_has_credit_and_fits(make_clip, make_source):
    sid = make_source(status="picked")
    big = ("Ünïcödé <tag> " * 1000)  # multi-byte, far over 5000 bytes
    clip = _clip(make_clip, source_id=sid, description=big, credit_text="Video: NASA",
                 hashtags=["#Space", "#NASA", "Shorts"])
    src = db.get_source(sid)
    d = upload.build_description(clip, src, ["#Shorts", "#Science"])
    assert len(d.encode("utf-8")) <= 5000
    assert "<" not in d and ">" not in d
    assert "Video: NASA" in d and f"Source: {clip.source_url}" in d and "Licence: Public domain" in d
    assert "#Shorts" in d and d.count("#Shorts") == 1 and "#Space" in d


def test_description_cc_by_attribution(make_clip, make_source):
    sid = make_source(status="picked", licence="cc_by", channel="Some Creator", title="Great Physics Talk",
                      source_type="cc_by_youtube")
    clip = _clip(make_clip, source_id=sid, credit_text="")
    d = upload.build_description(clip, db.get_source(sid))
    assert "Video: Some Creator" in d
    assert "creativecommons.org/licenses/by" in d
    assert '"Great Physics Talk" by Some Creator' in d
    assert "Changes:" in d


def test_tags_limit_and_marker(make_clip):
    clip = _clip(make_clip, hashtags=[f"#tag{i:03d}longishkeyword" for i in range(60)])
    tags = upload.build_tags(clip)
    assert tags[0] == f"clipagent-clip-{clip.id}"
    total = sum(len(t) + (2 if " " in t else 0) for t in tags) + len(tags) - 1
    assert total <= 500


def test_build_metadata_scheduled_and_immediate(make_clip, settings):
    clip = _clip(make_clip)
    when = Z("2026-11-13T17:15:00Z")
    body = upload.build_metadata(clip, settings=settings, publish_at=when)
    st = body["status"]
    assert st["privacyStatus"] == "private" and st["publishAt"] == "2026-11-13T17:15:00.000Z"
    assert st["selfDeclaredMadeForKids"] is False and st["license"] == "youtube"
    assert body["snippet"]["categoryId"] == "28"
    body2 = upload.build_metadata(clip, settings=settings.replace(youtube_default_privacy="unlisted"))
    assert body2["status"]["privacyStatus"] == "unlisted" and "publishAt" not in body2["status"]


# ---------------------------------------------------------------------------
# dry run
# ---------------------------------------------------------------------------

def test_dry_run_upload(clock, rendered_clip):
    cid = rendered_clip()
    upload.set_service_factory(lambda: pytest.fail("no API in dry run"))
    uid = upload.upload_clip(cid)
    u = db.get_upload(uid)
    assert u.dry_run and u.status == "uploaded" and u.youtube_video_id == f"dryrun-{cid}"
    assert u.quota_units == 0 and u.privacy == "private"
    assert u.publish_at == "2026-11-13T17:15:00Z"  # first slot >= now + 20 min
    assert db.require_clip(cid).status == "uploaded"
    assert upload.uploads_today() == 1
    assert youtube_auth.quota_used() == 0
    assert any("dry run" in e.message for e in db.list_events(module="upload"))
    # idempotent
    assert upload.upload_clip(cid) == uid
    assert len(db.list_uploads(clip_id=cid)) == 1


def test_dry_run_slots_progress(clock, rendered_clip):
    a, b = rendered_clip(), rendered_clip()
    ua, ub = upload.upload_clip(a), upload.upload_clip(b)
    assert db.get_upload(ua).publish_at == "2026-11-13T17:15:00Z"
    assert db.get_upload(ub).publish_at == "2026-11-13T22:10:00Z"


def test_cap_reached(clock, rendered_clip, make_upload, monkeypatch):
    monkeypatch.setenv("MAX_UPLOADS_PER_DAY", "2")
    config.reset_settings()
    make_upload(dry_run=True)
    make_upload(dry_run=True)
    cid = rendered_clip()
    with pytest.raises(upload.UploadCapReached):
        upload.upload_clip(cid)
    assert db.list_uploads(clip_id=cid) == []
    assert db.require_clip(cid).status == "rendered"


def test_requires_rendered(clock, make_clip):
    cid = make_clip(status="picked")
    with pytest.raises(upload.UploadError):
        upload.upload_clip(cid)


def test_too_long_clip_fails(clock, rendered_clip):
    cid = rendered_clip(start_sec=0, end_sec=200)
    with pytest.raises(upload.UploadError):
        upload.upload_clip(cid)
    assert db.require_clip(cid).status == "failed"


# ---------------------------------------------------------------------------
# live (fake service)
# ---------------------------------------------------------------------------

def test_live_upload_scheduled(clock, live, fake_yt, rendered_clip):
    cid = rendered_clip()
    uid = upload.upload_clip(cid)
    u = db.get_upload(uid)
    assert u.status == "uploaded" and u.youtube_video_id == "vid123" and not u.dry_run
    assert u.quota_units == 1600
    call = fake_yt.insert_calls[0]
    assert call["part"] == "snippet,status"
    assert call["body"]["status"]["publishAt"] == "2026-11-13T17:15:00.000Z"
    assert call["body"]["status"]["privacyStatus"] == "private"
    assert f"clipagent-clip-{cid}" in call["body"]["snippet"]["tags"]
    assert call["media_body"].resumable()
    assert youtube_auth.quota_used() == 1600
    assert db.require_clip(cid).status == "uploaded"
    # never twice
    assert upload.upload_clip(cid) == uid
    assert len(fake_yt.insert_calls) == 1


def test_live_upload_immediate_uses_default_privacy(clock, live, fake_yt, rendered_clip):
    cid = rendered_clip()
    uid = upload.upload_clip(cid, immediate=True)
    body = fake_yt.insert_calls[0]["body"]
    assert body["status"]["privacyStatus"] == "private" and "publishAt" not in body["status"]
    assert db.get_upload(uid).publish_at is None


def test_explicit_publish_at_and_too_soon(clock, live, fake_yt, rendered_clip):
    a, b = rendered_clip(), rendered_clip()
    upload.upload_clip(a, publish_at=Z("2026-11-20T18:00:00Z"))
    assert fake_yt.insert_calls[0]["body"]["status"]["publishAt"] == "2026-11-20T18:00:00.000Z"
    fake_yt.outcomes.append((None, {"id": "vid456"}))
    upload.upload_clip(b, publish_at=Z("2026-11-13T15:01:00Z"))  # in 1 minute -> next free slot
    assert fake_yt.insert_calls[1]["body"]["status"]["publishAt"] == "2026-11-13T17:15:00.000Z"


def test_retry_on_503(clock, live, fake_yt, rendered_clip, sleeps):
    fake_yt.outcomes[:] = [http_error(503, "backendError"), ConnectionResetError("reset"),
                           (None, {"id": "vidR"})]
    uid = upload.upload_clip(rendered_clip())
    assert db.get_upload(uid).youtube_video_id == "vidR"
    assert len(sleeps) == 2 and all(s > 0 for s in sleeps)


def test_retries_exhausted_leaves_clip_for_later(clock, live, fake_yt, rendered_clip, sleeps):
    fake_yt.outcomes[:] = [http_error(500, "internalError")] * (upload.MAX_RETRIES + 1)
    cid = rendered_clip()
    with pytest.raises(upload.UploadError):
        upload.upload_clip(cid)
    assert db.require_clip(cid).status == "rendered"
    assert db.list_uploads(clip_id=cid)[0].status == "failed"
    assert len(sleeps) == upload.MAX_RETRIES


@pytest.mark.parametrize("status,reason", [(403, "quotaExceeded"), (400, "uploadLimitExceeded")])
def test_quota_exceeded_stops_for_the_day(clock, live, fake_yt, rendered_clip, status, reason):
    fake_yt.outcomes[:] = [http_error(status, reason)]
    a, b = rendered_clip(), rendered_clip()
    with pytest.raises(upload.QuotaExhausted):
        upload.upload_clip(a)
    assert isinstance(upload.QuotaExhausted("x"), upload.UploadCapReached)
    assert db.require_clip(a).status == "rendered"
    notes = [n for n in db.list_notifications() if n.kind == "youtube_quota"]
    assert len(notes) == 1 and notes[0].status == "queued"
    assert upload.quota_exhausted_today()
    with pytest.raises(upload.QuotaExhausted):
        upload.upload_clip(b)
    assert len(fake_yt.insert_calls) == 1
    assert len([n for n in db.list_notifications() if n.kind == "youtube_quota"]) == 1
    # resets after midnight Pacific (08:00Z in November)
    clock.set(datetime(2026, 11, 14, 8, 1, tzinfo=UTC))
    assert not upload.quota_exhausted_today()


def test_local_quota_tally_stops_before_limit(clock, live, fake_yt, rendered_clip):
    youtube_auth.record_quota("videos.insert", units=8300)
    with pytest.raises(upload.QuotaExhausted):
        upload.upload_clip(rendered_clip())
    assert fake_yt.insert_calls == []


def test_permanent_error_fails_clip(clock, live, fake_yt, rendered_clip):
    fake_yt.outcomes[:] = [http_error(400, "invalidTitle")]
    cid = rendered_clip()
    with pytest.raises(upload.UploadError) as ei:
        upload.upload_clip(cid)
    assert not isinstance(ei.value, upload.UploadCapReached)
    assert db.require_clip(cid).status == "failed"
    assert any(e.level == "error" for e in db.list_events(module="upload"))


def test_auth_error_from_api(clock, live, fake_yt, rendered_clip):
    fake_yt.outcomes[:] = [http_error(401, "authError")]
    cid = rendered_clip()
    with pytest.raises(upload.UploadAuthError):
        upload.upload_clip(cid)
    assert db.require_clip(cid).status == "rendered"
    assert [n.kind for n in db.list_notifications()] == ["youtube_auth"]


def test_missing_token_raises_auth_and_notifies_once(clock, live, rendered_clip):
    cid = rendered_clip()
    for _ in range(2):
        with pytest.raises(upload.UploadAuthError):
            upload.upload_clip(cid)
    notes = [n for n in db.list_notifications() if n.kind == "youtube_auth"]
    assert len(notes) == 1 and notes[0].channel == "call"
    assert any(e.level == "error" for e in db.list_events(module="youtube_auth"))
    assert db.require_clip(cid).status == "rendered"


def test_landscape_render_rejected(clock, live, fake_yt, rendered_clip, monkeypatch):
    monkeypatch.setattr(upload, "probe_video", lambda p, settings=None: (1920, 1080, 30.0))
    cid = rendered_clip()
    with pytest.raises(upload.UploadError):
        upload.upload_clip(cid)
    assert db.require_clip(cid).status == "failed" and fake_yt.insert_calls == []


# ---------------------------------------------------------------------------
# crash recovery
# ---------------------------------------------------------------------------

def test_orphan_found_on_channel_is_not_reuploaded(clock, live, fake_yt, rendered_clip):
    cid = rendered_clip()
    orphan = db.add_upload(cid, privacy="private", publish_at="2026-11-13T17:15:00Z")
    clock.advance(hours=3)
    fake_yt.playlist_resp = {"items": [{"contentDetails": {"videoId": "vOLD"}}]}
    fake_yt.videos_list_resp = {"items": [{"id": "vOLD", "snippet": {"tags": [f"clipagent-clip-{cid}"]}}]}
    assert upload.upload_clip(cid) == orphan
    assert db.get_upload(orphan).youtube_video_id == "vOLD"
    assert fake_yt.insert_calls == []
    assert db.require_clip(cid).status == "uploaded"


def test_orphan_not_found_is_failed_then_uploaded(clock, live, fake_yt, rendered_clip):
    cid = rendered_clip()
    orphan = db.add_upload(cid, privacy="private")
    clock.advance(hours=3)
    uid = upload.upload_clip(cid)
    assert uid != orphan and db.get_upload(orphan).status == "failed"
    assert len(fake_yt.insert_calls) == 1


def test_recent_queued_row_blocks(clock, live, fake_yt, rendered_clip):
    cid = rendered_clip()
    db.add_upload(cid, privacy="private")
    with pytest.raises(upload.UploadError):
        upload.upload_clip(cid)
    assert fake_yt.insert_calls == []


# ---------------------------------------------------------------------------
# real googleapiclient request building (static discovery + HttpMockSequence)
# ---------------------------------------------------------------------------

def test_real_client_resumable_upload(clock, live, rendered_clip):
    from googleapiclient.discovery import build
    from googleapiclient.http import HttpMockSequence

    http = HttpMockSequence([
        ({"status": "200", "location": "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=xyz"}, b""),
        ({"status": "200"}, json.dumps({"id": "realVid", "status": {"privacyStatus": "private"}}).encode()),
    ])
    yt = build("youtube", "v3", http=http, static_discovery=True)
    upload.set_service_factory(lambda: yt)
    uid = upload.upload_clip(rendered_clip())
    assert db.get_upload(uid).youtube_video_id == "realVid"


# ---------------------------------------------------------------------------
# youtube_auth helpers
# ---------------------------------------------------------------------------

def test_quota_day_is_pacific(clock):
    clock.set(datetime(2026, 11, 14, 7, 30, tzinfo=UTC))  # 23:30 PT on the 13th, 02:30 NY on the 14th
    assert youtube_auth.quota_day().isoformat() == "2026-11-13"
    assert db.local_today().isoformat() == "2026-11-14"
    youtube_auth.record_quota("videos.list", calls=3)
    assert youtube_auth.quota_used() == 3
    clock.set(datetime(2026, 11, 14, 8, 30, tzinfo=UTC))
    assert youtube_auth.quota_used() == 0


def test_quota_limit_override(clock):
    assert youtube_auth.daily_quota_limit() == 10_000
    db.kv_set("youtube:quota_limit", 50_000)
    assert youtube_auth.daily_quota_limit() == 50_000


def test_save_credentials_0600(tmp_path):
    class C:
        def to_json(self):
            return json.dumps({"refresh_token": "r", "client_id": "c", "client_secret": "s",
                               "token": "t", "scopes": list(youtube_auth.SCOPES)})
    path = youtube_auth.save_credentials(C(), tmp_path / "sub" / "tok.json")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert youtube_auth.has_scope(youtube_auth.SCOPE_UPLOAD, config.get_settings().replace(youtube_token_path=path))
    assert not youtube_auth.has_scope(youtube_auth.SCOPE_MONETARY,
                                      config.get_settings().replace(youtube_token_path=path))


def test_load_credentials_revoked_token_reports(clock, settings, monkeypatch):
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials

    youtube_auth.save_credentials(type("C", (), {"to_json": lambda self: json.dumps({
        "refresh_token": "r", "client_id": "c", "client_secret": "s", "token": "t",
        "expiry": "2020-01-01T00:00:00Z", "scopes": list(youtube_auth.SCOPES)})})(), settings.youtube_token_path)

    def boom(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")
    monkeypatch.setattr(Credentials, "refresh", boom)
    with pytest.raises(youtube_auth.YouTubeAuthError, match="revoked"):
        youtube_auth.load_credentials()
    assert [n.kind for n in db.list_notifications()] == ["youtube_auth"]


def test_load_credentials_missing_scope(clock, settings):
    youtube_auth.save_credentials(type("C", (), {"to_json": lambda self: json.dumps({
        "refresh_token": "r", "client_id": "c", "client_secret": "s", "token": "t",
        "scopes": [youtube_auth.SCOPE_UPLOAD]})})(), settings.youtube_token_path)
    with pytest.raises(youtube_auth.YouTubeAuthError, match="lacks scopes"):
        youtube_auth.load_credentials(refresh=False)
