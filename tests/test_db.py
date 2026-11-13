import sqlite3
from datetime import UTC, date, datetime

import pytest

import db


def test_init_is_idempotent_and_versioned(settings):
    assert db.init_db() == db.SCHEMA_VERSION
    assert db.init_db() == db.SCHEMA_VERSION
    with db.connect() as c:
        versions = [r["version"] for r in c.execute("SELECT version FROM schema_version")]
        assert versions == list(range(1, db.SCHEMA_VERSION + 1))
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sources", "clips", "uploads", "stats", "channel_stats", "decisions", "llm_usage",
            "strategy_versions", "notifications", "events", "kv", "schema_version"} <= tables


def test_time_helpers(clock):
    assert db.now_iso() == "2026-11-13T15:00:00Z"
    assert db.local_today() == date(2026, 11, 13)
    start, end = db.local_day_bounds(date(2026, 11, 13))
    assert (start, end) == ("2026-11-13T05:00:00Z", "2026-11-14T05:00:00Z")
    clock.set(datetime(2026, 11, 14, 3, 30, tzinfo=UTC))  # 22:30 local on the 13th
    assert db.local_today() == date(2026, 11, 13)
    assert db.parse_iso("2026-11-13T15:00:00Z") == datetime(2026, 11, 13, 15, tzinfo=UTC)
    assert db.to_iso(datetime(2026, 1, 1, 12)) == "2026-01-01T12:00:00Z"  # naive = UTC
    assert db.local_date_of("2026-11-14T03:30:00Z") == date(2026, 11, 13)


def test_source_crud_and_dedupe():
    sid = db.add_source("https://youtu.be/abc", "nasa", title="T", licence_evidence={"x": 1}, metadata={"k": [1]})
    assert sid is not None
    assert db.add_source("https://youtu.be/abc", "nasa") is None
    s = db.require_source(sid)
    assert s.status == "candidate" and s.licence_evidence == {"x": 1} and s.metadata == {"k": [1]}
    db.update_source(sid, licence="us_gov_public_domain", video_path="/tmp/v.mp4")
    assert db.get_source_by_url("https://youtu.be/abc").licence == "us_gov_public_domain"
    with pytest.raises(ValueError):
        db.update_source(sid, status="done")
    with pytest.raises(db.NotFound):
        db.update_source(999, title="x")


def test_source_state_machine(make_source):
    sid = make_source()
    db.set_source_status(sid, "fetched")
    db.set_source_status(sid, "transcribed")
    with pytest.raises(db.InvalidTransition):
        db.set_source_status(sid, "candidate")
    db.set_source_status(sid, "failed", "whisper crashed")
    assert db.require_source(sid).status_reason == "whisper crashed"
    db.set_source_status(sid, "transcribed")  # retry from stage
    db.set_source_status(sid, "picked")
    db.set_source_status(sid, "done")
    with pytest.raises(db.InvalidTransition):
        db.set_source_status(sid, "failed")
    db.set_source_status(sid, "candidate", force=True)
    with pytest.raises(ValueError):
        db.set_source_status(sid, "bogus")


def test_list_and_count_sources(make_source):
    a = make_source()
    b = make_source(status="fetched", source_type="congress")
    make_source(status="rejected")
    assert [s.id for s in db.list_sources("candidate")] == [a]
    assert [s.id for s in db.list_sources(["fetched"], source_type="congress")] == [b]
    assert len(db.list_sources(limit=2)) == 2
    assert db.count_sources_by_status() == {"candidate": 1, "fetched": 1, "rejected": 1}


def test_clip_copies_licence_and_requires_it(make_source):
    sid = make_source(status="picked")
    cid = db.add_clip(sid, 5, 50, title="Hi", hashtags=["#a", "#b"], strategy_version=1,
                      experiment_id="e1", experiment_variant="control")
    clip = db.require_clip(cid)
    src = db.require_source(sid)
    assert clip.licence == src.licence and clip.source_url == src.url
    assert clip.hashtags == ["#a", "#b"] and clip.duration_sec == 45
    unlicensed = make_source(licence=None)
    with pytest.raises(ValueError, match="licence"):
        db.add_clip(unlicensed, 0, 10)
    with pytest.raises(ValueError):
        db.add_clip(sid, 10, 5)


def test_clip_state_machine_and_update(make_clip):
    cid = make_clip()
    db.update_clip(cid, render_path="/r.mp4", hashtags=("#x",))
    db.set_clip_status(cid, "rendered")
    db.set_clip_status(cid, "uploaded")
    with pytest.raises(db.InvalidTransition):
        db.set_clip_status(cid, "picked")
    c = db.require_clip(cid)
    assert c.render_path == "/r.mp4" and c.hashtags == ["#x"]
    assert [x.id for x in db.list_clips("uploaded")] == [cid]


def test_foreign_keys_cascade(make_clip):
    cid = make_clip()
    sid = db.require_clip(cid).source_id
    with db.transaction() as c:
        c.execute("DELETE FROM sources WHERE id = ?", (sid,))
    assert db.get_clip(cid) is None
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as c:
            c.execute("INSERT INTO clips (source_id, start_sec, end_sec, licence, source_url, created_at, updated_at)"
                      " VALUES (424242, 0, 1, 'x', 'y', 'a', 'b')")


def test_transaction_groups_writes_and_rolls_back(make_source):
    with pytest.raises(RuntimeError):
        with db.transaction() as c:
            db.add_source("https://x/1", "nasa", conn=c)
            db.log_event("test", "inside", conn=c)
            raise RuntimeError("boom")
    assert db.get_source_by_url("https://x/1") is None
    assert db.list_events(module="test") == []


def test_uploads_and_daily_count(clock, make_clip, make_upload):
    u1 = make_upload()
    make_upload(dry_run=True)
    failed = db.add_upload(make_clip(status="rendered"), privacy="private")
    db.mark_upload_failed(failed, "quotaExceeded")
    assert db.count_uploads_on() == 2
    assert db.quota_used_on() == db.UPLOAD_QUOTA_UNITS  # dry run excluded
    clock.advance(days=1)
    assert db.count_uploads_on() == 0
    got = db.get_upload(u1)
    assert got.status == "uploaded" and got.youtube_video_id and got.dry_run is False
    assert db.get_upload_by_video(got.youtube_video_id).id == u1
    assert db.live_video_ids() == [got.youtube_video_id]
    db.set_copyright_claim(got.youtube_video_id)
    assert db.get_upload(u1).copyright_claim is True
    assert {u.status for u in db.list_uploads()} == {"uploaded", "failed"}
    assert len(db.list_uploads(include_dry_run=False, status="uploaded")) == 1
    with pytest.raises(ValueError):
        db.add_upload(make_clip(), privacy="secret")


def test_publish_times_between(make_upload):
    make_upload(publish_at="2026-11-13T17:10:00Z")
    make_upload(publish_at="2026-11-13T22:15:00Z")
    make_upload(publish_at="2026-11-15T22:15:00Z")
    assert db.publish_times_between("2026-11-13T00:00:00Z", "2026-11-14T00:00:00Z") == [
        "2026-11-13T17:10:00Z", "2026-11-13T22:15:00Z"]


def test_stats_upsert_and_performance(make_upload):
    uid = make_upload(video_id="vidA")
    make_upload(video_id="dry", dry_run=True)
    db.upsert_stat("vidA", date(2026, 11, 13), views=100, likes=5, avg_view_pct=60.0, retention=[[0, 1.0], [1, 0.4]])
    db.upsert_stat("vidA", "2026-11-13", views=150, likes=6, avg_view_pct=62.0)
    db.upsert_stat("vidA", "2026-11-14", views=50, avg_view_pct=58.0)
    stats = db.get_stats("vidA")
    assert [s.views for s in stats] == [150, 50] and stats[0].retention is None
    assert len(db.stats_between("2026-11-14", "2026-11-14")) == 1
    perf = db.clip_performance()
    assert len(perf) == 1
    row = perf[0]
    assert row["youtube_video_id"] == "vidA" and row["views"] == 200 and row["days_tracked"] == 2
    # view-weighted: (150 * 62 + 50 * 58) / 200, not the mean of the daily percentages (60)
    assert row["avg_view_pct"] == pytest.approx(61.0)
    assert row["clip_id"] == db.get_upload(uid).clip_id
    db.upsert_channel_stat("2026-11-13", subscribers=10, total_views=1000)
    db.upsert_channel_stat("2026-11-13", subscribers=12, total_views=1100)
    assert [(c.day, c.subscribers) for c in db.list_channel_stats()] == [("2026-11-13", 12)]


def test_decisions_lifecycle(clock):
    did = db.add_decision("budget_increase", "Raise the budget to 2 dollars", {"new": 2.0},
                          expires_at="2026-11-14T15:00:00Z")
    d = db.get_decision(did)
    assert d.status == "pending" and d.payload == {"new": 2.0}
    db.set_decision_call_sid(did, "CA123")
    assert db.get_decision_by_call_sid("CA123").id == did
    assert db.resolve_decision(did, "approved", via="call") is True
    assert db.resolve_decision(did, "rejected", via="sms") is False  # first answer wins
    assert db.get_decision(did).status == "approved"
    with pytest.raises(ValueError):
        db.add_decision("world_domination", "no")
    with pytest.raises(db.NotFound):
        db.resolve_decision(999, "approved", via="call")

    d2 = db.add_decision("new_niche", "Add cooking", expires_at="2026-11-14T15:00:00Z")
    assert db.expire_decisions() == 0
    clock.advance(days=2)
    assert db.expire_decisions() == 1
    assert db.get_decision(d2).status == "expired"
    assert [x.id for x in db.list_decisions("expired")] == [d2]


def test_notifications_events_kv():
    nid = db.add_notification("daily_summary", "hello", "sms")
    db.add_notification("error", "older?", "log", status="sent")
    assert [n.id for n in db.list_notifications("queued")] == [nid]
    db.update_notification(nid, status="sent", sid="SM1")
    assert db.list_notifications("queued") == []
    with pytest.raises(ValueError):
        db.add_notification("x", "y", "pigeon")

    db.log_event("scout", "found 3", data={"n": 3})
    db.log_event("fetch", "boom", level="error")
    assert db.list_events(level="error")[0].module == "fetch"
    assert db.list_events(module="scout")[0].data == {"n": 3}
    with pytest.raises(ValueError):
        db.log_event("x", "y", level="fatal")

    assert db.kv_get("k", 5) == 5
    db.kv_set("k", {"a": 1})
    assert db.kv_get("k") == {"a": 1}
    assert db.kv_set_if_absent("k", 2) is False
    assert db.kv_set_if_absent("k2", 2) is True
    db.kv_delete("k")
    assert db.kv_get("k") is None


def test_strategy_versions_table():
    db.add_strategy_version(1, {"a": 1}, report="r")
    db.add_strategy_version(2, {"a": 2})
    assert db.latest_strategy_version().version == 2
    assert db.get_strategy_version(1).json == {"a": 1}
    assert [v.version for v in db.list_strategy_versions()] == [2, 1]


def test_llm_usage_helpers(clock):
    db.insert_llm_usage(model="m", purpose="p", input_tokens=10, output_tokens=5, cost_usd=0.25)
    db.insert_llm_usage(model="m", purpose="p", input_tokens=10, output_tokens=5, cost_usd=0.5, fake=True)
    start, end = db.local_day_bounds()
    assert db.llm_spend_between(start, end) == pytest.approx(0.75)
    summary = db.llm_usage_summary()
    assert summary[0]["calls"] == 2 and summary[0]["cost_usd"] == pytest.approx(0.75)
    assert db.list_llm_usage()[0].fake is True


# ---------------------------------------------------------------------------
# schema v2
# ---------------------------------------------------------------------------

def test_v1_database_upgrades_in_place(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:1])
    assert db.init_db(path) == 1
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO sources (id, url, source_type, licence, status, discovered_at, updated_at) "
                 "VALUES (1, 'https://nasa.gov/a.mp4', 'nasa', 'us_gov_public_domain', 'picked', 't', 't')")
    conn.execute("INSERT INTO clips (id, source_id, start_sec, end_sec, licence, source_url, status, created_at, "
                 "updated_at) VALUES (1, 1, 0, 30, 'us_gov_public_domain', 'https://nasa.gov/a.mp4', 'uploaded', 't', 't')")
    conn.execute("INSERT INTO uploads (id, clip_id, youtube_video_id, status, quota_units, dry_run, uploaded_at, "
                 "created_at, updated_at) VALUES (7, 1, 'vidOld', 'uploaded', 1600, 0, '2026-11-01T12:00:00Z', 't', 't')")
    conn.execute("INSERT INTO stats (youtube_video_id, day, views, fetched_at) VALUES ('vidOld', '2026-11-02', 42, 't')")
    conn.commit()
    conn.close()
    assert db.schema_version(path) == 1

    monkeypatch.undo()
    assert db.init_db(path) == db.SCHEMA_VERSION == 3
    assert db.schema_version(path) == 3
    with db.transaction(db.connect(path)) as c:
        up = db.get_upload(7, conn=c)
        assert up.youtube_video_id == "vidOld" and up.quota_units == 1600 and up.lifetime_views is None
        db.update_upload(7, status="superseded", conn=c)       # new status accepted by the rebuilt CHECK
        assert db.get_upload(7, conn=c).status == "superseded"
        assert db.get_stats("vidOld", conn=c)[0].estimated_revenue is None
        idx = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"idx_uploads_clip", "idx_uploads_uploaded_at", "idx_notifications_sid", "idx_llm_usage_clip",
                "idx_llm_usage_source", "idx_events_module_ts"} <= idx
        assert db.get_clip(1, conn=c).haiku_reason is None
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        c.commit()


def test_schema_version_of_missing_db(tmp_path):
    assert db.schema_version(tmp_path / "nope.db") == 0


def test_kv_incr_is_atomic_counter():
    assert db.kv_incr("n") == 1
    assert db.kv_incr("n", 5) == 6
    db.kv_set("m", 10)
    assert db.kv_incr("m", 3) == 13 and db.kv_get("m") == 13


def test_notification_getters():
    nid = db.add_notification("budget_cap", "hi", "call")
    assert db.get_notification(nid).kind == "budget_cap"
    assert db.get_notification(nid + 99) is None
    db.update_notification(nid, status="sent", sid="CA1")
    newer = db.add_notification("approval", "x", "call", sid="CA1")
    assert db.get_notification_by_sid("CA1").id == newer
    assert db.get_notification_by_sid("nope") is None


def test_count_clips_and_strategy_version_for_decision(make_clip):
    make_clip(experiment_id="exp1", experiment_variant="control")
    make_clip(experiment_id="exp1", experiment_variant="variant")
    make_clip(experiment_id="exp1", experiment_variant="variant", status="rendered")
    make_clip()
    assert db.count_clips() == 4
    assert db.count_clips(experiment_id="exp1") == 3
    assert db.count_clips(experiment_id="exp1", variant="variant") == 2
    assert db.count_clips(status=["rendered"]) == 1
    did = db.add_decision("strategy_change", "x")
    assert db.strategy_version_for_decision(did) is None
    db.add_strategy_version(5, {"v": 5}, decision_id=did)
    assert db.strategy_version_for_decision(did).version == 5


def test_lifetime_status_and_retention(make_upload):
    make_upload(video_id="vidL")
    db.set_upload_lifetime("vidL", views=10, likes=2, comments=1)
    db.set_youtube_status("vidL", "processed/public")
    up = db.get_upload_by_video("vidL")
    assert (up.lifetime_views, up.lifetime_likes, up.lifetime_comments) == (10, 2, 1)
    assert up.lifetime_fetched_at and up.youtube_status == "processed/public"
    with pytest.raises(db.NotFound):
        db.set_upload_lifetime("ghost", views=1, likes=0, comments=0)
    assert db.get_retention("vidL") is None
    db.upsert_retention("vidL", [(0.0, 1.0), (1.0, 0.4)], start_day=date(2026, 11, 1), end_day="2026-11-10")
    db.upsert_retention("vidL", [[0.0, 1.0], [1.0, 0.5]], start_day="2026-11-01", end_day="2026-11-11")
    r = db.get_retention("vidL")
    assert r.curve == [[0.0, 1.0], [1.0, 0.5]] and r.end_day == "2026-11-11"


def test_clip_performance_period_filters_stats_days(clock, make_upload, make_source, make_clip):
    sid = make_source(status="picked", topic="Mars rover")
    old = make_upload(make_clip(sid, status="rendered"), video_id="vOld")      # uploaded 2026-11-13
    clock.advance(days=10)
    make_upload(make_clip(sid, status="rendered", title="new"), video_id="vNew")  # uploaded 2026-11-23
    make_upload(make_clip(sid, status="rendered"), video_id="dry-1", dry_run=True)
    db.upsert_stat("vOld", "2026-11-14", views=100, avg_view_duration_sec=20.0, estimated_revenue=0.5)
    db.upsert_stat("vOld", "2026-11-20", views=30, avg_view_duration_sec=30.0)
    db.upsert_stat("vOld", "2026-11-22", views=7, avg_view_duration_sec=40.0)

    rows = {r["youtube_video_id"]: r for r in db.clip_performance("2026-11-20")}
    assert set(rows) == {"vOld", "vNew"}                     # old clip earned views in the period
    o = rows["vOld"]
    assert o["views"] == 37 and o["days_tracked"] == 2 and o["total_views"] == 137
    assert o["avg_view_duration_sec"] == pytest.approx((30 * 30 + 7 * 40) / 37) and o["topic"] == "Mars rover"
    assert o["estimated_revenue"] is None and o["upload_status"] == "uploaded"
    assert rows["vNew"]["views"] == 0 and rows["vNew"]["days_tracked"] == 0   # new, no data yet

    prev = db.clip_performance("2026-11-13", "2026-11-19")
    assert [(r["youtube_video_id"], r["views"], r["estimated_revenue"]) for r in prev] == [("vOld", 100, 0.5)]
    assert [r["youtube_video_id"] for r in db.clip_performance()] == ["vOld", "vNew"]
    db.update_upload(old, status="removed")
    assert db.clip_performance("2026-11-20")[0]["upload_status"] == "removed"
    assert db.clip_performance("2026-12-01") == []


def test_v2_database_upgrades_to_v3_and_failed_migration_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "v2.db"
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:2])
    assert db.init_db(path) == 2
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO sources (id, url, source_type, licence, status, discovered_at, updated_at) "
                 "VALUES (1, 'https://nasa.gov/a.mp4', 'nasa', 'us_gov_public_domain', 'picked', 't', 't')")
    conn.execute("INSERT INTO clips (id, source_id, start_sec, end_sec, licence, source_url, status, created_at, "
                 "updated_at, title) VALUES (1, 1, 0, 30, 'us_gov_public_domain', 'u', 'picked', 't', 't', 'Kept')")
    conn.commit()
    conn.close()
    # a migration that dies half way leaves the database exactly at v2 (one transaction per migration)
    monkeypatch.setattr(db, "MIGRATIONS", [*db.MIGRATIONS[:2], "ALTER TABLE clips ADD COLUMN x TEXT; BROKEN SQL"])
    with pytest.raises(sqlite3.Error):
        db.init_db(path)
    assert db.schema_version(path) == 2
    conn = sqlite3.connect(path)
    assert "x" not in {r[1] for r in conn.execute("PRAGMA table_info(clips)")}
    conn.close()
    monkeypatch.undo()
    assert db.init_db(path) == 3
    c = db.connect(path)
    try:
        clip = db.get_clip(1, conn=c)
        assert clip.title == "Kept" and clip.haiku_reason is None
        db.update_clip(1, haiku_reason="strong hook", conn=c)
        assert db.get_clip(1, conn=c).haiku_reason == "strong hook"
    finally:
        c.close()


def test_add_clip_stores_haiku_reason(make_source):
    sid = make_source(status="transcribed")
    cid = db.add_clip(sid, 1.0, 30.0, haiku_score=8, haiku_reason="clear question, punchy answer")
    assert db.get_clip(cid).haiku_reason == "clear question, punchy answer"


def test_status_change_is_compare_and_set(make_clip, monkeypatch):
    """Two processes that both read 'rendered' must not both move the clip (lost update)."""
    cid = make_clip(status="rendered")
    real = db.require_clip

    def stale_read(clip_id, *, conn=None):
        clip = real(clip_id, conn=conn)
        # another process wins the race between our read and our write
        other = db.connect()
        try:
            other.execute("UPDATE clips SET status = 'skipped' WHERE id = ?", (clip_id,))
            other.commit()
        finally:
            other.close()
        return clip

    monkeypatch.setattr(db, "require_clip", stale_read)
    with pytest.raises(db.InvalidTransition):
        db.set_clip_status(cid, "uploaded")
    monkeypatch.undo()
    assert db.get_clip(cid).status == "skipped"


def test_claim_upload_is_exclusive(make_clip):
    cid = make_clip(status="rendered")
    first = db.claim_upload(cid, privacy="private")
    assert first is not None
    assert db.claim_upload(cid, privacy="private") is None          # live upload in flight
    db.mark_upload_failed(first, "boom")
    second = db.claim_upload(cid, privacy="private")
    assert second is not None and second != first
    db.mark_upload_succeeded(second, "vid1")
    assert db.claim_upload(cid, privacy="private") is None          # already live
    dry_clip = make_clip(status="rendered")
    d = db.claim_upload(dry_clip, privacy="private", dry_run=True)
    db.mark_upload_succeeded(d, "dryrun-x", quota_units=0)
    assert db.claim_upload(dry_clip, privacy="private", dry_run=True) is None   # dry run already done
    assert db.claim_upload(dry_clip, privacy="private", dry_run=False) is not None  # going live is allowed


def test_count_notifications_between(clock):
    a = db.add_notification("x", "a", "sms", status="sent")
    db.add_notification("x", "b", "call", status="sent")
    db.add_notification("x", "c", "sms", status="dry_run")
    s, e = db.local_day_bounds()
    assert db.count_notifications_between(s, e) == 3
    assert db.count_notifications_between(s, e, status="sent") == 2
    assert db.count_notifications_between(s, e, status="sent", channels=["sms"]) == 1
    clock.advance(days=1)
    db.update_notification(a, status="failed")
    assert db.count_notifications_between(s, e, status="sent") == 1
