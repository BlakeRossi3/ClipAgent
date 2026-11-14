"""assistant/selfcheck.py: the acceptance suite runs green on the seeded demo, undoes every change and sends nothing."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil

import pytest
from fastapi.testclient import TestClient

import config
import config_store
import db
import seed_demo
from assistant import admin, events, registry, selfcheck, store, tools
from assistant import settings as asettings
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")


@pytest.fixture
def demo(tmp_path, monkeypatch, clock):
    staff = tmp_path / "agents"
    shutil.copytree(registry.AGENTS_DIR, staff, ignore=shutil.ignore_patterns(".archive"))
    monkeypatch.setattr(registry, "AGENTS_DIR", staff)
    data_dir = config.get_settings().data_dir
    seed_demo.seed(data_dir, days=10, seed_value=5, now=clock.now)
    monkeypatch.setenv("CLIPAGENT_STRATEGY_PATH", str(data_dir / "strategy.json"))
    monkeypatch.setenv("VAULT_DIR", str(data_dir / "vault"))
    for k in ("ASSISTANT_ENABLED", "FAKE_ASSISTANT", "OWNER_NAME"):
        monkeypatch.setenv(k, os.environ.get(k, ""))
    cfg = tmp_path / "config.env"
    cfg.write_text("# his settings\nVOICE_TTS_VOICE=bm_george\n", encoding="utf-8")
    monkeypatch.setenv("CLIPAGENT_CONFIG_FILE", str(cfg))
    config_store.reload()
    config.reset_settings()
    asettings.reset_assistant_settings()
    return {"staff": staff, "cfg": cfg}


def _digest(d):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.glob("*.md"))}


def _work_log():
    with store.transaction() as c:
        return [tuple(r) for r in c.execute("SELECT job_id, week_start, hours FROM as_work_log ORDER BY 1, 2")]


def test_selfcheck_runs_green_on_the_demo_and_undoes_everything(demo):
    staff_before, cfg_before, log_before = _digest(demo["staff"]), demo["cfg"].read_text(), _work_log()
    notes_before = len(db.list_notifications(limit=1000))
    rep = selfcheck.run()
    by = {x.check: x for x in rep.lines}
    assert rep.ok, rep.text()
    assert [x.check for x in rep.lines] == [n for n, _ in selfcheck.CHECKS] + ["safety"]
    for name in ("hours", "pay", "staff", "voice", "history", "memory", "open_url", "map", "studio", "upload",
                 "usage", "safety"):
        assert by[name].status == "pass", (name, by[name].detail)
    assert by["render"].status == "skip" and "source video" in by["render"].detail      # demo clips have no media
    assert by["twilio"].status in ("pass", "warn")
    assert "then undid it" in by["hours"].detail and "and back" in by["staff"].detail
    assert "Nothing was sent" in by["upload"].detail
    # nothing left behind: staff files, his config.env, the work log, notifications, admin changes
    assert _digest(demo["staff"]) == staff_before and demo["cfg"].read_text() == cfg_before
    assert _work_log() == log_before and len(db.list_notifications(limit=1000)) == notes_before
    assert all(c["undone"] for c in admin.list_changes(50))
    assert any(e["type"] == "panel" and e["panel"]["title"] == "SELF-CHECK MAP" for e in events.BUS.since(0, 1000))
    assert "All checks passed" in rep.summary() or "warnings" in rep.summary()


def test_selfcheck_on_an_empty_app_skips_politely(monkeypatch, tmp_path):
    staff = tmp_path / "agents"
    shutil.copytree(registry.AGENTS_DIR, staff, ignore=shutil.ignore_patterns(".archive"))
    monkeypatch.setattr(registry, "AGENTS_DIR", staff)
    rep = selfcheck.run()
    by = {x.check: x for x in rep.lines}
    assert rep.ok, rep.text()
    assert by["hours"].status == "skip" and by["render"].detail == "No clips yet."
    assert by["upload"].detail == "No clips yet." and by["safety"].status == "pass"


def test_a_broken_capability_is_a_fail_line_and_the_tool_and_settings_button_report_it(demo, monkeypatch):
    def boom():
        raise RuntimeError("ledger offline")

    monkeypatch.setattr(selfcheck, "CHECKS", [("pay", boom), ("map", selfcheck.check_map)])
    rep = selfcheck.run()
    assert not rep.ok and rep.lines[0].status == "fail" and "ledger offline" in rep.lines[0].detail
    tools.load_extensions()
    res = tools.dispatch("selfcheck_run", {"only": ["map"]}, tools.ToolContext(channel="voice", agent="main"),
                         allowed={"selfcheck_run"})
    out = json.loads(res.content)
    assert res.ok and out["ok"] and [x["check"] for x in out["lines"]] == ["map", "safety"]

    import server

    c = TestClient(server.create_app(), base_url="http://127.0.0.1:8000")
    page = c.get("/setup/").text
    assert 'action="/setup/action/selfcheck"' in page
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)
    r = c.post("/setup/action/selfcheck", data={"csrf": csrf})
    assert r.status_code == 200 and "Self-check: problems found" in r.text and "ledger offline" in r.text


def test_cli(demo, capsys, monkeypatch):
    import main

    monkeypatch.setattr(selfcheck, "CHECKS", [("map", selfcheck.check_map)])
    assert main.main(["assistant", "selfcheck"]) == 0
    out = capsys.readouterr().out
    assert "PASS  map" in out and "All checks passed" in out


def test_render_check_renders_a_slice_to_a_temp_file_and_leaves_the_clip_alone(make_clip, make_source, tmp_path,
                                                                               monkeypatch):
    import edit

    video = tmp_path / "src.mp4"
    video.write_bytes(b"\0" * 100)
    sid = make_source(status="picked")
    db.update_source(sid, video_path=str(video))
    cid = make_clip(source_id=sid, status="rendered")
    before = db.get_clip(cid)
    outs = []

    def fake_ffmpeg(cmd, timeout=None):
        out = cmd[-1]
        outs.append(out)
        open(out, "wb").write(b"m" * 4096)

    monkeypatch.setattr(edit, "run_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(edit, "probe_media", lambda p, s=None: edit.MediaInfo(1920, 1080, 600.0, True))
    monkeypatch.setattr(selfcheck.shutil, "which", lambda b: "/usr/bin/ffmpeg")
    detail = selfcheck.check_render()
    assert f"clip {cid}" in detail and "deleted it" in detail
    assert outs and not os.path.exists(outs[0])                      # the temporary render is gone
    assert db.get_clip(cid) == before                                 # status, render_path untouched
