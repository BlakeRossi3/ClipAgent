"""assistant/initiative.py: the backlog, idle + usage-share gating, picking, the background run and its
bookkeeping, harvest, the away report, undo, tools and the Initiative page. Offline, fake runner/model."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from assistant_fixtures import assistant_env  # noqa: F401
from fastapi.testclient import TestClient

import db
import main
import server
from assistant import capabilities, initiative, runtime, selfedit, store, tasks, tools, usage
from assistant import settings as asettings

pytestmark = pytest.mark.usefixtures("assistant_env")

REPORT = ("All done.\nSTATUS: done\nBUILT: A wake word you can say\nWHY: He asked twice\n"
          "TESTS: pytest -q, 1200 passed\nLEFT: none")


@pytest.fixture
def on(monkeypatch, clock):
    monkeypatch.setenv("ASSISTANT_ENABLED", "true")
    asettings.reset_assistant_settings()
    store.init_store()
    state = {"reply": (REPORT, "ok"), "briefs": []}

    def runner(brief, context):
        state["briefs"].append(brief)
        return state["reply"]

    tasks.set_runner(runner)
    yield state
    tasks.set_runner(None)


def _wait():
    for t in tasks.list_tasks():
        tasks.wait(t["id"], 10)


def _ctx():
    return tools.ToolContext(channel="dashboard", agent="main", source="test")


def _window(util: float, *, kind: str = "five_hour", hours: float = 3, at: datetime | None = None) -> None:
    resets = int((at or db.utcnow() + timedelta(hours=hours)).timestamp())
    usage.record_window({"status": "allowed", "rateLimitType": kind, "utilization": util, "resetsAt": resets})


def _quiet(clock, minutes: int = 45) -> None:
    store.add_message("in", "voice", "thanks, that's all for now")
    clock.advance(minutes=minutes)


# -- storage -----------------------------------------------------------------------

def test_store_backlog_helpers(on, clock):
    a = store.add_backlog("First", detail="d", source="owner")
    b = store.add_backlog("Second")
    c = store.add_backlog("Urgent one", priority=1)            # high priority goes on top
    assert [i.id for i in store.list_backlog("open")] == [c, a, b]
    assert store.move_backlog(b, -1) and [i.id for i in store.list_backlog("open")] == [c, b, a]
    assert store.move_backlog(a, -10_000) and [i.id for i in store.list_backlog("open")] == [a, c, b]
    assert not store.move_backlog(a, -1)
    assert store.claim_backlog(a) and not store.claim_backlog(a)
    it = store.get_backlog(a)
    assert it.status == "in_progress" and it.attempts == 1 and it.last_attempt_at
    store.update_backlog(a, status="done", commits=["abc"], meta={"built": "x"}, finished_at=db.now_iso())
    assert store.get_backlog(a).commits == ["abc"] and store.get_backlog(a).meta == {"built": "x"}
    assert [i.id for i in store.list_backlog_finished()] == [a]
    with pytest.raises(ValueError):
        store.add_backlog("bad", source="nobody")
    with pytest.raises(ValueError):
        store.update_backlog(b, status="whatever")


def test_seed_once_in_order_without_duplicates(on):
    store.add_backlog("A 'Hey Cecil' wake word", source="owner")      # he already asked for one of them
    n = initiative.seed()
    titles = [i.title for i in store.list_backlog(None)]
    assert n == len(initiative.SEED_LIST) - 1 and titles.count("A 'Hey Cecil' wake word") == 1
    assert titles[1] == initiative.SEED_LIST[0][0] and titles[-1] == initiative.SEED_LIST[-1][0]
    assert initiative.seed() == 0
    db.kv_delete(initiative.KV_SEEDED)
    assert initiative.seed() == 0                                        # dedupe, not just the flag
    assert initiative.similar("Connect to Higgsfield for images", "Connect Cecil to Higgsfield to make images")
    assert not initiative.similar("A Hey Cecil wake word", "Google Calendar and Gmail")


# -- gating ------------------------------------------------------------------------

def test_idle_gate_and_busy_gate(on, clock, monkeypatch):
    store.add_message("in", "dashboard", "add a weather widget")
    ok, why, when = initiative.gate()
    assert not ok and "active 0 min ago" in why and when == db.utcnow() + timedelta(minutes=30)
    clock.advance(minutes=31)
    assert initiative.gate()[0]
    monkeypatch.setattr(selfedit, "in_flight", lambda: True)
    ok, why, _ = initiative.gate()
    assert not ok and "being tested" in why
    monkeypatch.setattr(selfedit, "in_flight", lambda: False)
    runtime.set_stopped(True, via="test")
    assert initiative.gate()[1] == "Cecil is stopped"
    runtime.set_stopped(False, via="test")
    monkeypatch.setenv("ASSISTANT_INITIATIVE", "false")
    asettings.reset_assistant_settings()
    assert "initiative is off" in initiative.gate()[1] and initiative.tick()["status"] == "off"


def test_usage_share_gate(on, clock):
    _quiet(clock)
    assert initiative.usage_gate()[0]                                   # nothing known yet, no runs
    _window(0.85)
    ok, why, when = initiative.usage_gate()
    assert not ok and "constrained" not in why and when is not None     # usage.constrained (>= 80 %) blocks
    _window(0.30)
    assert initiative.usage_gate()[0]
    started = db.now_iso()
    w = initiative.windows()
    # three runs in flight (no fresh usage event since): 3 x 0.10 estimate < 0.35 share
    db.kv_set(initiative.KV_RUNS, [{"id": f"r{i}", "started": started, "windows": w} for i in range(3)])
    assert initiative.share_used("five_hour", w["five_hour"]) == pytest.approx(0.30)
    assert initiative.usage_gate()[0]
    db.kv_set(initiative.KV_RUNS, initiative.runs() + [{"id": "r4", "started": started, "windows": w}])
    ok, why, _ = initiative.usage_gate()
    assert not ok and "35% share of the 5-hour window" in why
    # observed: a fresh event after the run shows it only used 2 %
    clock.advance(minutes=5)
    _window(0.32, at=db.parse_iso(w["five_hour"]["resets_at"]))
    end = initiative.windows()
    db.kv_set(initiative.KV_RUNS, [{"id": "r", "started": started, "windows": w, "end": end}])
    assert initiative.share_used("five_hour", initiative.windows()["five_hour"]) == pytest.approx(0.02)
    assert initiative.usage_gate()[0]
    st = initiative.status()
    assert st["usage_known"] and st["share_used"][0]["window"] == "5-hour" and st["share_pct"] == 35


def test_fallback_cap_without_usage_data(on, clock):
    _quiet(clock)
    db.kv_set(initiative.KV_RUNS, [{"id": "a", "started": db.now_iso()},
                                   {"id": "b", "started": db.to_iso(db.utcnow() - timedelta(hours=1))}])
    ok, why, when = initiative.usage_gate()
    assert not ok and "at most 2 runs per 5 hours" in why
    assert when == db.utcnow() - timedelta(hours=1) + timedelta(hours=5)
    clock.advance(hours=4, minutes=1)
    assert initiative.usage_gate()[0]


# -- picking and running ------------------------------------------------------------------

def test_pick_order_attempts_and_backoff(on, clock):
    a = store.add_backlog("Alpha")
    b = store.add_backlog("Bravo")
    c = store.add_backlog("Charlie")
    store.update_backlog(a, attempts=1, last_attempt_at=db.to_iso(db.utcnow() - timedelta(hours=1)))
    store.update_backlog(b, attempts=3)
    assert initiative.pick().id == c
    clock.advance(hours=6)
    assert initiative.pick().id == a


def test_tick_runs_top_item_and_records_the_result(on, clock):
    db.kv_set(initiative.KV_SEEDED, "x")                  # no seed list in this test
    first = store.add_backlog("A 'Hey Cecil' wake word", source="owner")
    store.add_backlog("Something else")
    _quiet(clock)
    res = initiative.tick()
    assert res["status"] == "started" and res["item_id"] == first
    _wait()
    brief = on["briefs"][-1]
    assert "Backlog item #%d" % first in brief and "capability_check" in brief and "STATUS:" in brief
    assert "Do not git commit" in brief and "Agent/Heartbeat.md" in brief
    it = store.get_backlog(first)
    assert it.status == "done" and it.attempts == 1 and it.meta["built"] == "A wake word you can say"
    assert it.meta["tests"].startswith("pytest") and it.meta["edit"] == "unchanged" and it.finished_at
    run = initiative.runs()[-1]
    assert run["item_id"] == first and run["status"] == "done" and run["finished"]
    t = tasks.list_tasks()[-1]
    assert t["meta"]["initiative_id"] == first and t.get("self_edit") and not t["announce"]
    # the next tick: fallback cap allows a second run, then waits
    assert initiative.tick()["status"] == "started"
    _wait()
    assert initiative.tick() == {"status": "waiting", "reason": "plan usage not reported yet: at most 2 runs per 5 hours"}
    assert db.kv_get(initiative.KV_LAST_TICK)["status"] == "waiting"


def test_failures_count_attempts_then_block(on, clock):
    db.kv_set(initiative.KV_SEEDED, "x")
    iid = store.add_backlog("Fix the texting")
    on["reply"] = ("It broke.", "error")
    for _ in range(3):
        _quiet(clock, minutes=6 * 60 + 1)
        assert initiative.tick()["status"] == "started"
        _wait()
    it = store.get_backlog(iid)
    assert it.status == "blocked" and it.attempts == 3 and "did not finish" in it.result
    # plan limit: back to open, the attempt is not counted
    j = store.add_backlog("Budget stopped")
    on["reply"] = ("I hit the Claude plan's limit before finishing.", "budget")
    _quiet(clock, minutes=6 * 60 + 1)
    initiative.tick()
    _wait()
    assert store.get_backlog(j).status == "open" and store.get_backlog(j).attempts == 0
    # a partial result stays open with what is left
    k = store.add_backlog("Partial work")
    store.update_backlog(j, status="dropped")
    on["reply"] = ("STATUS: partial\nBUILT: half of it\nLEFT: the other half", "ok")
    _quiet(clock, minutes=6 * 60 + 1)
    initiative.tick()
    _wait()
    assert store.get_backlog(k).status == "open" and store.get_backlog(k).meta["left"] == "the other half"
    assert "the other half" in initiative.brief(store.get_backlog(k))


def test_commits_from_the_self_edit_flow_are_recorded(on, clock, monkeypatch):
    db.kv_set(initiative.KV_SEEDED, "x")
    iid = store.add_backlog("Build a widget")
    monkeypatch.setattr(tasks, "_edit_begin", lambda: object())

    def fake_finish(snap, row):
        row["edit"] = {"status": "committed", "summary": "saved", "files": ["home.py"], "commit": "c2",
                       "commits": ["c1", "c2"]}
        return False

    monkeypatch.setattr(tasks, "_edit_finish", fake_finish)
    _quiet(clock)
    initiative.tick()
    _wait()
    it = store.get_backlog(iid)
    assert it.commits == ["c1", "c2"] and it.meta["files"] == ["home.py"] and it.meta["edit"] == "committed"
    assert initiative.work_log()[0]["commits"] == ["c1", "c2"]


def test_reconcile_reopens_items_lost_to_a_restart(on, clock):
    iid = store.add_backlog("Lost one")
    store.claim_backlog(iid)
    store.update_backlog(iid, meta={"task_id": "gone"})
    assert initiative._reconcile() == 1 and store.get_backlog(iid).status == "open"


# -- a real self-edit around a background task ------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "app"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "app.py").write_text("VERSION = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "initial")
    selfedit.set_app_dir(r)
    selfedit.set_runners(tests=lambda root, timeout: (True, "ok"), restart=lambda: True)
    restarts = []
    monkeypatch.setattr(selfedit, "schedule_restart", lambda delay=0: restarts.append(1))
    yield r, restarts
    selfedit.set_runners()
    selfedit.set_app_dir(r.parent / "no-repo-after-test")   # never None (= the real checkout)


def test_background_self_edit_is_committed_and_undoable_per_item(on, clock, repo, monkeypatch):
    r, restarts = repo
    db.kv_set(initiative.KV_SEEDED, "x")
    iid = store.add_backlog("Bump the version")

    def runner(brief, context):
        (r / "app.py").write_text("VERSION = 2\n")
        return REPORT, "ok"

    tasks.set_runner(runner)
    _quiet(clock)
    initiative.tick()
    _wait()
    it = store.get_backlog(iid)
    assert it.status == "done" and len(it.commits) == 1 and it.meta["edit"] == "committed"
    assert selfedit.TRAILER in _git(r, "log", "-1", "--format=%B") and restarts == [1]
    db.kv_delete(selfedit.KV_PENDING)                                   # the restart check passed
    monkeypatch.setattr(selfedit, "in_flight", lambda: True)
    res = initiative.undo_item(iid)
    assert not res["ok"] and "Not now" in res["message"]                # refused while busy
    monkeypatch.setattr(selfedit, "in_flight", lambda: False)
    res = initiative.undo_item(iid)
    assert res["ok"] and (r / "app.py").read_text() == "VERSION = 1\n" and restarts == [1, 1]
    it = store.get_backlog(iid)
    assert it.status == "blocked" and it.meta["undone_at"] and initiative.work_log()[0]["undone"]
    assert not initiative.undo_item(iid)["ok"]                          # nothing left to undo


# -- harvest -----------------------------------------------------------------------------

def test_harvest_from_messages_capabilities_failures_with_dedupe(on, clock):
    fb = runtime.FakeBackend()
    runtime.set_backend(fb)
    initiative.seed()
    store.add_message("in", "voice", "I really wish you could read my Google calendar out loud every morning")
    store.add_message("in", "dashboard", "and get that hey cecil wake word working")
    store.add_message("in", "sms", "9", kind="checkin_answer")
    fb.queue(json.dumps({"items": [
        {"title": "Read his Google Calendar aloud each morning", "detail": "He wishes you could read it out."},
        {"title": "A Hey Cecil wake word", "detail": "Asked again."}]}))
    capabilities.record("transcribe voice notes from WhatsApp", "gave_up", "no API access")
    for _ in range(2):
        store.record_agent_run("researcher", "look up x", model="m", duration_ms=1, cost_usd=0.0, status="error",
                               summary="timeout")
    res = initiative.harvest()
    assert res["status"] == "ok"
    added = {a["title"]: a["source"] for a in res["added"]}
    assert added == {"Read his Google Calendar aloud each morning": "owner",
                     "Capability: transcribe voice notes from WhatsApp": "capability",
                     "Fix repeated failures of the researcher agent": "failure"}
    assert [d["title"] for d in res["duplicates"]] == ["A Hey Cecil wake word"]
    req = fb.requests[-1]
    assert req.agent == "initiative" and "Google calendar" in req.messages[0]["content"]
    assert "A 'Hey Cecil' wake word" in req.messages[0]["content"] and "9" not in req.messages[0]["content"].split("\n")[-2:]
    n = len(fb.requests)
    res = initiative.harvest()                                           # nothing new: no model call, no repeats
    assert len(fb.requests) == n and res["added"] == []
    # a dropped wish is never harvested back
    gid = next(i.id for i in store.list_backlog(None) if i.title.startswith("Read his Google"))
    store.update_backlog(gid, status="dropped")
    store.add_message("in", "voice", "calendar read out in the morning please")
    fb.queue(json.dumps({"items": [{"title": "Read his Google Calendar aloud every morning", "detail": ""}]}))
    assert initiative.harvest()["added"] == []


def test_harvest_triggers_after_many_messages_and_keeps_cursor_on_error(on, clock, monkeypatch):
    fb = runtime.FakeBackend()
    runtime.set_backend(fb)
    for i in range(initiative.HARVEST_AFTER_MESSAGES - 1):
        store.add_message("in", "voice", f"message {i}")
    assert initiative.maybe_harvest() is None
    store.add_message("in", "voice", "one more")

    def boom(*a, **k):
        raise RuntimeError("model down")

    from assistant import auto_memory

    monkeypatch.setattr(auto_memory, "_ask_model", boom)
    assert initiative.maybe_harvest()["status"] == "error"
    assert len(initiative.new_owner_messages()) == initiative.HARVEST_AFTER_MESSAGES   # read again next time


# -- telling him ---------------------------------------------------------------------------

def _built(title: str, built: str) -> int:
    iid = store.add_backlog(title)
    store.update_backlog(iid, status="done", meta={"built": built}, finished_at=db.now_iso())
    return iid


def test_away_report_spoken_once_and_digest_texted_once(on, clock):
    store.add_message("in", "voice", "see you tomorrow")
    clock.advance(hours=2)
    _built("A 'Hey Cecil' wake word", "A Hey Cecil wake word")
    clock.advance(minutes=10)
    _built("More charts", "Two new charts on the home page")
    line = initiative.announcement()
    assert line.startswith("While you were out I built a Hey Cecil wake word and two new charts on the home page.")
    assert initiative.announcement() == ""
    assert initiative.send_digest() is None                             # only 2 h 10 quiet
    clock.advance(hours=2)
    res = initiative.send_digest()
    assert res["sent"] and initiative.send_digest() is None
    msg = store.list_messages(1, channel="sms", direction="out")[0]
    assert msg.kind == "initiative" and "wake word" in msg.body
    clock.advance(minutes=5)
    _built("Third thing", "A third thing")
    assert "a third thing" in initiative.announcement()


def test_briefing_includes_the_away_report(on, clock):
    from voice import web

    _built("Widgets", "Home widgets")
    snap = web.activity()
    assert "While you were out I built home widgets" in snap["briefing"]
    assert "While you were out" not in web.activity()["briefing"]


# -- tools, jobs, pages -----------------------------------------------------------------------

def test_tools(on, clock):
    tools.load_extensions()
    assert {"backlog_list", "backlog_add", "backlog_update", "initiative_status"} <= set(tools.REGISTRY)
    r = initiative._tool_add(_ctx(), {"title": "Read my calendar out", "detail": "he said so", "priority": "high"})
    assert r["added"] and store.list_backlog("open")[0].id == r["id"]
    again = initiative._tool_add(_ctx(), {"title": "read my calendar out please"})
    assert not again["added"] and again["duplicate_of"]["id"] == r["id"]
    lst = initiative._tool_list(_ctx(), {})
    assert lst["items"][0]["title"] == "Read my calendar out" and len(lst["items"]) == 1 + len(initiative.SEED_LIST)
    u = initiative._tool_update(_ctx(), {"id": r["id"], "move": "bottom", "status": "dropped"})
    assert u["item"]["status"] == "dropped"
    with pytest.raises(tools.ToolError):
        initiative._tool_update(_ctx(), {"id": 9999})
    st = initiative._tool_status(_ctx(), {})
    assert st["enabled"] and "fallback" in st


def test_jobs_follow_the_setting(on, monkeypatch):
    ids = [j.id for j in main.job_specs()]
    assert "assistant_initiative" in ids and "assistant_initiative_harvest" in ids
    monkeypatch.setenv("ASSISTANT_INITIATIVE", "false")
    asettings.reset_assistant_settings()
    assert "assistant_initiative" not in [j.id for j in main.job_specs()]
    assert initiative.harvest()["status"] == "off"


def _csrf(c):
    html = c.get("/dashboard/initiative").text
    return re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1)


def test_page_json_and_actions(on, clock, monkeypatch):
    c = TestClient(server.create_app())
    page = c.get("/dashboard/initiative")
    assert page.status_code == 200 and initiative.SEED_LIST[0][0] in page.text
    assert 'href="/dashboard/initiative"' in page.text and "Work log" in page.text
    j = c.get("/dashboard/api/initiative").json()
    assert len(j["backlog"]) == len(initiative.SEED_LIST) and "status" in j
    csrf = _csrf(c)
    assert c.post("/dashboard/initiative/add", data={"title": "x"}).status_code == 403            # CSRF guard
    r = c.post("/dashboard/initiative/add", data={"csrf": csrf, "title": "Make a podcast feed", "priority": "high"},
               follow_redirects=False)
    assert r.status_code == 303 and "added" in r.headers["location"]
    top = store.list_backlog("open")[0]
    assert top.title == "Make a podcast feed" and top.source == "owner"
    r = c.post("/dashboard/initiative/add", data={"csrf": csrf, "title": "make a podcast feed"}, follow_redirects=False)
    assert "duplicate" in r.headers["location"]
    c.post(f"/dashboard/initiative/{top.id}/down", data={"csrf": csrf})
    assert store.list_backlog("open")[1].id == top.id
    c.post(f"/dashboard/initiative/{top.id}/top", data={"csrf": csrf})
    assert store.list_backlog("open")[0].id == top.id
    c.post(f"/dashboard/initiative/{top.id}/edit", data={"csrf": csrf, "title": "Podcast feed", "detail": "RSS"})
    assert store.get_backlog(top.id).title == "Podcast feed" and store.get_backlog(top.id).detail == "RSS"
    c.post(f"/dashboard/initiative/{top.id}/drop", data={"csrf": csrf})
    assert store.get_backlog(top.id).status == "dropped" and "Podcast feed" in c.get("/dashboard/initiative").text
    c.post(f"/dashboard/initiative/{top.id}/reopen", data={"csrf": csrf})
    assert store.get_backlog(top.id).status == "open"
    assert c.post(f"/dashboard/initiative/{top.id}/explode", data={"csrf": csrf}).status_code == 404
    # undo is refused while busy
    done = _built("Widgets", "Home widgets")
    store.update_backlog(done, commits=["abc"])
    assert "Home widgets" in c.get("/dashboard/initiative").text
    monkeypatch.setattr(selfedit, "in_flight", lambda: True)
    r = c.post(f"/dashboard/initiative/{done}/undo", data={"csrf": csrf})
    assert r.status_code == 409 and "Not now" in r.text
    # the Scheduled page shows a line about it
    sched = c.get("/dashboard/scheduled").text
    assert "Backlog and work log" in sched
