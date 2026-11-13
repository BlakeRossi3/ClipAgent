"""Orchestrator policies with the stage functions replaced by fakes (no ffmpeg, no network)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import budget
import config
import db
import edit
import fetch
import main
import pick
import transcribe
import upload


# ---------------------------------------------------------------------------
# fake stages
# ---------------------------------------------------------------------------

@pytest.fixture
def stages(monkeypatch, settings):
    """Stage stand-ins that move rows like the real ones. ``fail[...]`` makes a stage raise."""
    calls: list[tuple[str, int]] = []
    fail: dict[str, Exception] = {}

    def fetch_source(sid, strategy=None):
        calls.append(("fetch", sid))
        if "fetch" in fail:
            exc = fail["fetch"]
            db.set_source_status(sid, "rejected" if isinstance(exc, fetch.LicenceRejected) else "failed", str(exc))
            raise exc
        video = settings.downloads_dir / str(sid) / "video.mp4"
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"v" * 100)
        db.update_source(sid, video_path=str(video), licence="us_gov_public_domain")
        db.set_source_status(sid, "fetched")
        return video

    def transcribe_source(sid):
        calls.append(("transcribe", sid))
        if "transcribe" in fail:
            db.set_source_status(sid, "failed", "boom")
            raise fail["transcribe"]
        path = settings.transcripts_dir / f"{sid}.json"
        path.write_text('{"segments": []}')
        db.update_source(sid, transcript_path=str(path))
        db.set_source_status(sid, "transcribed")
        return path

    def pick_clips(sid, strategy=None):
        calls.append(("pick", sid))
        if "pick" in fail:
            raise fail["pick"]
        cid = db.add_clip(sid, 1.0, 30.0, title="t")
        db.set_source_status(sid, "picked")
        return [cid]

    def render_clip(cid, strategy=None, **_):
        calls.append(("render", cid))
        if "render" in fail:
            db.set_clip_status(cid, "failed", "ffmpeg died")
            raise edit.RenderError("ffmpeg died")
        out = settings.renders_dir / f"{cid}.mp4"
        out.write_bytes(b"r" * 10)
        db.update_clip(cid, render_path=str(out))
        db.set_clip_status(cid, "rendered")
        return out

    monkeypatch.setattr(fetch, "fetch_source", fetch_source)
    monkeypatch.setattr(transcribe, "transcribe_source", transcribe_source)
    monkeypatch.setattr(pick, "pick_clips", pick_clips)
    monkeypatch.setattr(edit, "render_clip", render_clip)
    return {"calls": calls, "fail": fail}


# ---------------------------------------------------------------------------
# process_source / process_pending
# ---------------------------------------------------------------------------

def test_process_source_resumes_and_finishes(stages, make_source, clock):
    sid = make_source(licence=None)
    rendered = main.process_source(sid)
    assert [c[0] for c in stages["calls"]] == ["fetch", "transcribe", "pick", "render"]
    assert len(rendered) == 1 and db.require_source(sid).status == "done"
    stages["calls"].clear()
    assert main.process_source(sid) == [] and stages["calls"] == []       # done: nothing to do


def test_process_source_without_llm_stops_at_transcribed(stages, make_source, clock):
    sid = make_source()
    assert main.process_source(sid, allow_llm=False) == []
    assert db.require_source(sid).status == "transcribed"


def test_render_failure_is_per_clip_and_bounded(stages, make_source, make_clip, clock):
    sid = make_source(status="picked")
    a, b = make_clip(sid), make_clip(sid, start_sec=60.0, end_sec=90.0)
    stages["fail"]["render"] = edit.RenderError("x")
    assert main.process_source(sid) == []
    assert {db.require_clip(a).status, db.require_clip(b).status} == {"failed"}
    assert db.require_source(sid).status == "picked"                   # clips may still be re-rendered
    for _ in range(main.MAX_CLIP_RETRIES):
        main.render_clips(sid)
    stages["calls"].clear()
    assert main.render_clips(sid) == [] and stages["calls"] == []        # retries exhausted
    main.process_pending()
    assert db.require_source(sid).status == "done"


def test_process_pending_orders_work_and_isolates_errors(stages, make_source, make_clip, clock):
    picked = make_source(status="picked")
    make_clip(picked)
    bad = make_source(url="https://www.youtube.com/watch?v=bad")
    stages["fail"]["fetch"] = fetch.FetchError("network down")
    counters = main.process_pending(max_sources=5)
    assert counters["processed"] == 2 and counters["failed"] == 1 and counters["clips_rendered"] == 1
    assert stages["calls"][0] == ("render", db.list_clips(source_id=picked)[0].id)  # closest to done first
    assert db.require_source(bad).status == "failed"


def test_licence_rejection_is_permanent(stages, make_source, clock):
    sid = make_source()
    stages["fail"]["fetch"] = fetch.LicenceRejected("C-SPAN")
    assert main.process_pending()["rejected"] == 1
    clock.advance(hours=5)
    assert main.retry_failed_sources() == [] and db.require_source(sid).status == "rejected"


def test_failed_sources_retry_twice_then_stay_failed(stages, make_source, clock):
    sid = make_source()
    stages["fail"]["transcribe"] = transcribe.TranscribeError("model crashed")
    main.process_pending()
    assert db.require_source(sid).status == "failed"
    assert main.retry_failed_sources() == []                   # too soon
    for attempt in range(1, main.MAX_SOURCE_RETRIES + 1):
        clock.advance(hours=1)
        main.process_pending()
        assert db.kv_get(f"main:retries:{sid}") == attempt
        assert ("fetch", sid) not in stages["calls"][1:]           # resumed after the download
    clock.advance(hours=1)
    assert main.retry_failed_sources() == [] and db.require_source(sid).status == "failed"
    assert [c[0] for c in stages["calls"]].count("transcribe") == 1 + main.MAX_SOURCE_RETRIES


def test_budget_exceeded_stops_llm_but_keeps_rendering(stages, make_source, make_clip, clock):
    t1, t2 = make_source(status="transcribed"), make_source(status="transcribed")
    picked = make_source(status="picked")
    make_clip(picked)
    stages["fail"]["pick"] = budget.BudgetExceeded("cap", spent=1, cap=1)
    counters = main.process_pending(max_sources=5)
    assert counters["budget_stopped"] == 1 and counters["clips_rendered"] == 1
    assert [c for c in stages["calls"] if c[0] == "pick"] == [("pick", t1)]   # no second LLM attempt
    assert db.require_source(t1).status == db.require_source(t2).status == "transcribed"
    assert db.require_source(picked).status == "done"


def test_stage_lock_makes_a_concurrent_run_skip(stages, clock):
    with main.stage_lock("process"):
        with pytest.raises(main.Busy, match="pid"):
            main.process_pending()
        assert main.lock_holder("process") == str(os.getpid())
        assert main.run_job("process", main.process_pending) is None
    assert db.kv_get("main:job:process")["skipped"]
    assert main.lock_holder("process") is None


# ---------------------------------------------------------------------------
# upload_ready
# ---------------------------------------------------------------------------

def test_upload_ready_stops_on_cap_and_continues_past_errors(monkeypatch, make_clip, clock):
    clips = [make_clip(status="rendered") for _ in range(4)]
    seen: list[int] = []

    def fake_upload(cid, publish_at=None, *, immediate=False):
        seen.append(cid)
        if cid == clips[0]:
            raise upload.UploadError("bad metadata")
        if cid == clips[2]:
            raise upload.QuotaExhausted("quota")
        return 100 + cid

    monkeypatch.setattr(upload, "upload_clip", fake_upload)
    assert main.upload_ready() == [100 + clips[1]]
    assert seen == clips[:3]
    assert db.list_events(module="main")[0].level == "warning"
    seen.clear()
    monkeypatch.setattr(upload, "upload_clip", lambda cid, **_: (_ for _ in ()).throw(upload.UploadCapReached("4")))
    assert main.upload_ready() == []


def test_upload_ready_respects_limit(monkeypatch, make_clip, clock):
    for _ in range(3):
        make_clip(status="rendered")
    monkeypatch.setattr(upload, "upload_clip", lambda cid, **_: cid)
    assert len(main.upload_ready(limit=2)) == 2


# ---------------------------------------------------------------------------
# jobs / scheduler
# ---------------------------------------------------------------------------

def test_run_job_logs_errors_and_alerts_once_per_day(clock):
    def boom():
        raise RuntimeError("disk full")

    assert main.run_job("analytics", boom) is None
    assert main.run_job("analytics", boom) is None
    err = db.list_events(level="error", module="main")
    assert len(err) == 2 and "disk full" in err[0].message and "Traceback" in err[0].data["traceback"]
    assert [n.kind for n in db.list_notifications()] == ["job_error"]
    state = db.kv_get("main:job:analytics")
    assert state["ok"] is False and state["started_at"] == "2026-11-13T15:00:00Z"
    assert main.run_job("analytics", lambda: 7) == 7 and db.kv_get("main:job:analytics")["ok"] is True


def test_analytics_job_isolates_steps(monkeypatch):
    import analytics

    monkeypatch.setattr(analytics, "collect_stats", lambda: (_ for _ in ()).throw(RuntimeError("api down")))
    monkeypatch.setattr(analytics, "refresh_channel_stats", lambda: None)
    monkeypatch.setattr(analytics, "check_claims", lambda: 2)
    out = main.analytics_job()
    assert out["collect_stats"].startswith("failed") and out["check_claims"] == 2


def test_scheduler_jobs_and_options(settings):
    sched = main.build_scheduler()
    sched.start(paused=True)            # job defaults are applied when the scheduler starts
    jobs = {j.id: j for j in sched.get_jobs()}
    sched.shutdown(wait=False)
    assert set(jobs) == {"scout", "process", "upload", "send_queued", "expire_decisions", "analytics", "cleanup",
                         "daily_summary", "weekly_review"}
    assert str(sched.timezone) == settings.timezone
    for j in jobs.values():
        assert j.max_instances == 1 and j.coalesce and j.misfire_grace_time == 1800
        assert j.func is main.run_job and j.args[0] == j.id
    assert "hour='6', minute='10'" in str(jobs["scout"].trigger)
    assert "day_of_week='sun', hour='18', minute='5'" in str(jobs["weekly_review"].trigger)


def test_scheduler_lock_is_exclusive():
    fh = main.acquire_lock("scheduler")
    try:
        with pytest.raises(main.Busy):
            main.acquire_lock("scheduler")
    finally:
        fh.close()
    main.acquire_lock("scheduler").close()


# ---------------------------------------------------------------------------
# manual
# ---------------------------------------------------------------------------

def test_infer_source_type_prefers_government_then_guesses(monkeypatch, strategy):
    info = {"webpage_url": "https://www.youtube.com/watch?v=x", "extractor_key": "Youtube", "channel": "Educator",
            "channel_id": "UCx", "license": "Creative Commons Attribution license (reuse allowed)", "duration": 600}
    monkeypatch.setattr(fetch, "extract_info", lambda url, download, outdir=None: dict(info))
    assert main.infer_source_type("https://www.youtube.com/watch?v=x", strategy) == "cc_by_youtube"
    info.update(channel="NASA", channel_id="UCLA_DiR1FfKNvjuUpBHmylQ", license=None)
    assert main.infer_source_type("https://www.youtube.com/watch?v=x", strategy) == "nasa"

    def offline(url, download, outdir=None):
        raise RuntimeError("no network")
    monkeypatch.setattr(fetch, "extract_info", offline)
    assert main.infer_source_type("https://science.house.gov/hearing.mp4", strategy) == "congress"


def test_manual_rejects_disabled_source_type(clock):
    with pytest.raises(ValueError, match="not enabled"):
        main.manual("https://www.dvidshub.net/video/1", "dvids")
    assert db.list_sources() == []


# ---------------------------------------------------------------------------
# DRY_RUN -> live
# ---------------------------------------------------------------------------

def test_requeue_dry_run(make_clip, make_upload, settings, clock, monkeypatch):
    with_render, without_render, live_one = (make_clip(status="rendered") for _ in range(3))
    render = settings.renders_dir / f"{with_render}.mp4"
    render.write_bytes(b"x")
    db.update_clip(with_render, render_path=str(render))
    db.update_clip(without_render, render_path=str(settings.renders_dir / "gone.mp4"))
    dry_a = make_upload(with_render, video_id=f"dryrun-{with_render}", dry_run=True, publish_at="2026-11-13T22:15:00Z")
    make_upload(without_render, video_id=f"dryrun-{without_render}", dry_run=True)
    make_upload(live_one, video_id="realvid")
    for cid in (with_render, without_render, live_one):
        db.set_clip_status(cid, "uploaded")

    out = main.requeue_dry_run()
    assert out == {"rendered": [with_render], "picked": [without_render]}
    assert db.require_clip(with_render).status == "rendered"
    assert db.require_clip(without_render).status == "picked" and db.require_clip(without_render).render_path is None
    assert db.require_clip(live_one).status == "uploaded"
    sup = db.get_upload(dry_a)
    assert sup.status == "superseded" and sup.youtube_video_id is None and f"dryrun-{with_render}" in sup.error
    assert db.publish_times_between("2026-11-13T00:00:00Z", "2026-11-14T00:00:00Z") == []   # slot freed
    assert main.requeue_dry_run() == {"rendered": [], "picked": []}
    # a forced requeue while still in DRY_RUN can be dry-run uploaded again (fake id is free)
    monkeypatch.setattr(upload, "probe_video", lambda path, settings=None: (1080, 1920, 30.0))
    assert len(main.upload_ready()) == 1 and db.require_clip(with_render).status == "uploaded"


def test_cli_requeue_refuses_while_dry_run(capsys):
    assert main.main(["requeue-dry-run"]) == 2
    assert "DRY_RUN is still true" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------

def _media(settings, sid: int) -> Path:
    folder = settings.downloads_dir / str(sid)
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("video.mp4", "audio.wav", "info.json"):
        (folder / name).write_bytes(b"0" * 1000)
    db.update_source(sid, video_path=str(folder / "video.mp4"), audio_path=str(folder / "audio.wav"))
    return folder


def test_cleanup_deletes_only_what_is_safe(make_source, make_clip, make_upload, settings, clock):
    done = make_source(status="done")
    rejected = make_source(status="rejected")
    still_needed = make_source(status="done")
    dry_only = make_source(status="done")
    recent = make_source(status="done")
    folders = {sid: _media(settings, sid) for sid in (done, rejected, still_needed, dry_only, recent)}
    make_clip(still_needed, status="rendered")
    make_upload(make_clip(dry_only, status="uploaded"), dry_run=True)
    live_clip = make_clip(done, status="rendered")
    render = settings.renders_dir / f"{live_clip}.mp4"
    render.write_bytes(b"1" * 500)
    db.update_clip(live_clip, render_path=str(render))
    make_upload(live_clip, video_id="live1")
    db.set_clip_status(live_clip, "uploaded")

    clock.advance(days=settings.source_media_retention_days + 1)
    db.update_source(recent, title="touched")                      # recently updated: kept
    out = main.cleanup()
    assert out["sources"] == 2 and out["renders"] == 0
    for sid in (done, rejected):
        assert [p.name for p in folders[sid].iterdir()] == ["info.json"]       # licence evidence kept
        src = db.require_source(sid)
        assert src.video_path is None and src.metadata["media_deleted_at"]
    for sid in (still_needed, dry_only, recent):
        assert (folders[sid] / "video.mp4").exists()
    assert render.exists()

    clock.advance(days=settings.render_retention_days)
    assert main.cleanup()["renders"] == 1
    assert not render.exists() and db.require_clip(live_clip).render_path is None


# ---------------------------------------------------------------------------
# status / doctor / CLI
# ---------------------------------------------------------------------------

def test_status_text(make_upload, make_source, clock):
    make_source(status="failed")
    make_upload(publish_at="2026-11-14T13:45:00Z")
    db.add_decision("budget_increase", "Raise the budget to 2 dollars")
    main.run_job("scout", lambda: [])
    text = main.status_text()
    assert "DRY_RUN=on" in text and "failed 1" in text and "Uploads today: 1/4" in text
    assert "#1 budget_increase" in text and "Sat 08:45" in text
    assert "scout             11-13 10:00  ok" in text and "Scheduler: not running" in text


def test_doctor_reports_failures_with_fixes(monkeypatch, settings):
    monkeypatch.setattr(main, "_whisper_check", lambda s: main.Check("whisper model", "pass", "cached"))
    config.set_settings(settings.replace(dry_run=False, fake_llm=False, ffmpeg_bin="/nonexistent/ffmpeg"))
    checks = {c.name: c for c in main.doctor_checks()}
    assert checks["ffmpeg"].status == "fail" and "apt install ffmpeg" in checks["ffmpeg"].fix
    assert checks["Anthropic"].status == "fail" and checks["Twilio"].status == "fail"
    assert checks["YouTube token"].status == "fail" and checks["strategy.json"].status == "pass"
    assert checks["database schema"].status == "warn"            # not initialised yet
    text = main.format_checks(list(checks.values()))
    assert "fix: set ANTHROPIC_API_KEY" in text and text.rstrip().endswith("failed")


def test_doctor_network_checks_use_httpx(monkeypatch):
    import httpx

    seen = []

    def fake_get(url, **kw):
        seen.append(url)
        if "twilio" in url:
            raise httpx.ConnectError("refused")
        return httpx.Response(404)

    monkeypatch.setattr(httpx, "get", fake_get)
    checks = main._network_checks()
    assert len(seen) == len(main.NETWORK_HOSTS)
    assert {c.status for c in checks if "twilio" in c.name} == {"fail"}
    assert all(c.status == "pass" for c in checks if "twilio" not in c.name)


def test_cli_init_status_doctor(monkeypatch, capsys, settings):
    monkeypatch.setattr(main, "_whisper_check", lambda s: main.Check("whisper model", "warn", "not cached"))
    assert main.main(["init"]) == 0
    assert db.latest_strategy_version().version == 1
    assert main.main(["status"]) == 0
    assert main.main(["doctor"]) == (1 if any(c.status == "fail" for c in main.doctor_checks()) else 0)
    out = capsys.readouterr().out
    assert f"schema v{db.SCHEMA_VERSION}" in out and "Pending approvals: 0" in out and "[PASS] strategy.json" in out


def test_cli_approve(capsys, clock):
    did = db.add_decision("other", "Do the thing")
    assert main.main(["approve", str(did)]) == 0
    assert db.get_decision(did).status == "approved" and db.get_decision(did).resolved_via == "cli"
    assert main.main(["reject", str(did)]) == 1
    assert "approved (unchanged)" in capsys.readouterr().out


def test_retry_spacing_uses_updated_at(stages, make_source, clock):
    sid = make_source(status="failed")
    db.set_source_status(sid, "failed", "x")
    clock.advance(minutes=10)
    assert main.retry_failed_sources() == []
    clock.advance(minutes=45)
    assert main.retry_failed_sources() == [sid] and db.require_source(sid).status == "candidate"


def test_unexpected_error_is_not_retried_forever(stages, make_source, clock):
    """A bug-type exception in a stage must count against the retry budget like any failure,
    not leave the source in place to be re-run (and re-billed) every hour."""
    sid = make_source(status="transcribed")
    stages["fail"]["pick"] = KeyError("bug in postprocess")
    for _ in range(10):
        main.process_pending()
        clock.advance(hours=1)
    picks = [c for c in stages["calls"] if c == ("pick", sid)]
    assert len(picks) == 1 + main.MAX_SOURCE_RETRIES
    assert db.require_source(sid).status == "failed"


def test_doctor_warns_about_allowlist_entries_without_ids(monkeypatch):
    monkeypatch.setattr(main, "_whisper_check", lambda s: main.Check("whisper model", "warn", "not cached"))
    chk = {c.name: c for c in main.doctor_checks()}["allowlist channel ids"]
    assert chk.status == "warn" and "House Committee on Science" in chk.detail


def test_llm_outage_stops_judgment_without_failing_sources(stages, make_source, clock):
    t1, t2 = make_source(status="transcribed"), make_source(status="transcribed")
    stages["fail"]["pick"] = pick.LLMUnavailable("API down")
    counters = main.process_pending(max_sources=5)
    assert counters["llm_unavailable"] == 1
    assert [c for c in stages["calls"] if c[0] == "pick"] == [("pick", t1)]
    assert db.require_source(t1).status == db.require_source(t2).status == "transcribed"


def test_doctor_dashboard_token_rules(settings):
    def tok(**kw):
        config.set_settings(settings.replace(**kw))
        return {c.name: c.status for c in main.doctor_checks()}
    assert tok(public_base_url="https://agent.example.com", dashboard_token="")["DASHBOARD_TOKEN"] == "fail"
    assert tok(public_base_url="http://localhost:8000", dashboard_token="")["DASHBOARD_TOKEN"] == "warn"
    assert tok(public_base_url="https://agent.example.com", dashboard_token="short")["DASHBOARD_TOKEN"] == "warn"
    assert tok(public_base_url="https://agent.example.com", dashboard_token="x" * 24)["DASHBOARD_TOKEN"] == "pass"
    st = tok(public_base_url="https://agent.example.com", dashboard_token="x" * 24, dashboard_allow_unauthenticated=True)
    assert st["DASHBOARD_ALLOW_UNAUTHENTICATED"] == "fail"


def test_assistant_cli(capsys, clock, monkeypatch, tmp_path):
    monkeypatch.setenv("ASSISTANT_ENABLED", "true")
    config.reset_settings()
    from assistant import settings as aset
    aset.reset_assistant_settings()
    assert main.main(["assistant", "job", "--rate", "25"]) == 0
    assert main.main(["assistant", "hours", "38.5"]) == 0
    assert "Logged, sir: 38.5 hours, roughly $963 before tax" in capsys.readouterr().out
    assert main.main(["assistant", "plan"]) == 0
    assert "weekly_hours" in capsys.readouterr().out
    assert main.main(["assistant", "validate"]) == 0
    assert {j.id for j in main.job_specs()} >= {"assistant_plan", "assistant_checkins", "assistant_tidy"}
    checks = {c.name: c for c in main.doctor_checks()}
    assert checks["pay rate"].status == "pass" and checks["assistant"].detail == "enabled"
    aset.reset_assistant_settings()
