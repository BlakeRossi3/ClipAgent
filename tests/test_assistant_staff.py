"""The staff at work: Agent-tool hand-offs become staff runs; prompts carry the roster; the task rail
shows texts, dashboard requests and scheduled staff jobs while they run (fake runner only)."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import db
from assistant import claude_code, runtime, staff, store, tasks
from assistant import settings as asettings
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")


def _result(text="Done, sir.", session="sess-1"):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": text,
                       "session_id": session, "num_turns": 2, "total_cost_usd": 0.0,
                       "usage": {"input_tokens": 10, "output_tokens": 5}})


def _agent_call(tid="t1", agent="researcher", desc="Look up race-day nutrition", prompt="Find the facts."):
    return json.dumps({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
        {"type": "tool_use", "id": tid, "name": "Agent",
         "input": {"subagent_type": agent, "description": desc, "prompt": prompt}}]}})


def _agent_result(tid="t1", text="Carbs 60-90 g an hour.", error=False):
    return json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid, "is_error": error, "content": [{"type": "text", "text": text}]}]}})


def _tool_call(name, inp, tid="x1"):
    return json.dumps({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
        {"type": "tool_use", "id": tid, "name": name, "input": inp}]}})


class Runner:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []
        self.hook = None

    def __call__(self, argv, *, input, env, cwd, timeout):
        self.calls.append(argv)
        if self.hook is not None:
            self.hook()
        out = self.outputs.pop(0) if self.outputs else _result()
        return claude_code.Completed(0, out + "\n", "")


@pytest.fixture
def cc(monkeypatch, clock):
    monkeypatch.setenv("ASSISTANT_ENABLED", "true")
    monkeypatch.setenv("ASSISTANT_BACKEND", "claude_code")
    monkeypatch.setenv("FAKE_ASSISTANT", "false")
    asettings.reset_assistant_settings()
    monkeypatch.setattr(claude_code, "find_claude", lambda: Path("claude"))
    r = Runner()
    claude_code.set_runner(r)
    yield r
    claude_code.set_runner(None)


# -- roster ---------------------------------------------------------------------------

def test_roster_lists_every_specialist_and_resolves_names():
    names = {r["name"] for r in staff.roster()}
    assert {"researcher", "studio", "ledger", "quartermaster", "archivist", "briefer", "engineer"} <= names
    assert "main" not in names
    assert staff.resolve("researcher") == "researcher" and staff.resolve("allen") == "researcher"
    assert staff.resolve("nobody") is None and staff.resolve("") is None
    text = staff.roster_text()
    assert "- researcher (Allen):" in text and "Agent tool" in text


def test_spoken_and_background_prompts_carry_the_staff():
    from assistant import brain

    voice, worker = brain.system_prompt(worker=False), brain.system_prompt(worker=True)
    for p in (voice, worker):
        assert "# Your staff" in p and "- studio (Invincible):" in p
    assert "start_task with staff=" in voice and "hand each part that is a specialist's" in worker


# -- tracker --------------------------------------------------------------------------

def test_tracker_records_a_staff_run_from_start_to_result():
    t = staff.StaffTracker(channel="voice")
    t.feed_line(_agent_call())
    run = store.list_agent_runs(5, agent="researcher")[0]
    assert run.status == "running" and run.task.startswith("Look up race-day nutrition")
    t.feed_line(_agent_result())
    run = store.get_agent_run(run.id)
    assert run.status == "ok" and run.summary == "Carbs 60-90 g an hour."
    assert any(e.message == "researcher took on a job" for e in db.list_events(limit=10))


def test_tracker_ignores_non_staff_agents_and_closes_cut_off_runs():
    t = staff.StaffTracker()
    t.feed_line(_agent_call(tid="g", agent="general-purpose"))
    t.feed_line(_tool_call("Read", {"file_path": "x"}))
    assert store.list_agent_runs(5) == []
    t.feed_line(_agent_call(tid="s", agent="studio"))
    t.feed_line(_agent_result(tid="other"))       # someone else's result: still open
    t.close()
    run = store.list_agent_runs(5, agent="studio")[0]
    assert run.status == "error" and "before this job reported back" in run.summary


def test_full_run_records_staff_hand_offs(cc):
    cc.outputs = ["\n".join([_agent_call(), _agent_result(), _result("Here's what Allen found.")])]
    res = runtime.run_turn("what should I eat on race day", channel="dashboard")
    assert res.status == "ok"
    runs = store.list_agent_runs(10, agent="researcher")
    assert len(runs) == 1 and runs[0].status == "ok" and runs[0].parent_id is not None


def test_staff_page_shows_the_run(cc):
    import home_live

    cc.outputs = ["\n".join([_agent_call(agent="ledger", desc="Weekly pay"), _agent_result(), _result()])]
    runtime.run_turn("how much did I make", channel="dashboard")
    row = next(r for r in home_live.staff_at_work() if r["name"] == "ledger")
    assert row["runs_today"] == 1 and row["last"]["status"] == "ok"


# -- background tasks given to a specialist -----------------------------------------

def test_background_task_given_to_a_specialist():
    seen = {}

    def runner(brief, context):
        seen["brief"] = brief
        return "Found it.", "ok"

    tasks.set_runner(runner)
    try:
        tid = tasks.start("Research the best running shoes", staff="researcher", announce=False)
        assert tasks.wait(tid, 5)
    finally:
        tasks.set_runner(None)
    row = tasks.get_task(tid)
    assert row["meta"]["staff"] == "researcher" and row["meta"]["label"] == "Allen"
    assert 'subagent_type "researcher"' in seen["brief"] and row["status"] == "done"


# -- the rail ------------------------------------------------------------------------

def test_a_text_shows_on_the_rail_while_it_runs_with_its_steps(cc):
    during = {}

    def peek():
        during["rows"] = [dict(t) for t in tasks.running_tasks()]

    cc.hook = peek
    cc.outputs = ["\n".join([_tool_call("mcp__clipagent__reel_watch", {"url": "https://x"}), _result("Watched it.")])]
    with pytest.MonkeyPatch.context() as mp:
        from assistant import tools

        mp.setattr(tools, "send_to_owner", lambda text, **kw: {"message_id": store.add_message("out", "sms", text)})
        res = runtime.run_turn("take a look at this reel https://x", channel="sms", deliver=True)
    assert res.status == "ok"
    live = during["rows"]
    assert len(live) == 1 and live[0]["meta"]["via"] == "sms" and live[0]["tracked"]
    assert live[0]["task"].startswith("take a look at this reel")
    row = tasks.get_task(live[0]["id"])
    assert row["status"] == "done" and row["result"].startswith("Watched it") and row["steps"]
    assert tasks.running_tasks() == []


def test_scheduled_staff_job_shows_on_the_rail(cc):
    during = {}
    cc.hook = lambda: during.setdefault("rows", [dict(t) for t in tasks.running_tasks()])
    runtime.run_task("briefer", "Write the morning brief.", kind="morning_brief")
    (row,) = during["rows"]
    assert row["meta"]["label"] == "Donald" and row["task"].startswith("Morning brief:")
    assert tasks.get_task(row["id"])["status"] == "done"


def test_history_distil_stays_off_the_rail(cc):
    runtime.run_task("archivist", "Distil this.", kind="history_distil")
    assert tasks.list_tasks() == []


def test_cancelling_a_tracked_text_stops_it_and_sends_nothing(cc):
    sent = []
    holder = {}

    def cancel_mid_run():
        rid = tasks.running_tasks()[0]["id"]
        holder["id"] = rid
        tasks.cancel(rid, "removed from the list by him")

    cc.hook = cancel_mid_run
    with pytest.MonkeyPatch.context() as mp:
        from assistant import tools

        mp.setattr(tools, "send_to_owner", lambda text, **kw: sent.append(text) or {"message_id": 1})
        res = runtime.run_turn("do the thing", channel="sms", deliver=True)
    assert res.status == "cancelled" and sent == []
    assert tasks.get_task(holder["id"])["status"] == "cancelled"


def test_tracked_rows_cut_off_by_a_restart_are_closed(clock):
    tid = tasks.track("old text", origin="sms")
    tasks._tracked.discard(tid)          # as after a restart: no live run in this process
    clock.advance(hours=3)
    tasks._requeue_orphans()
    row = tasks.get_task(tid)
    assert row["status"] == "error" and "restarted" in row["summary"]


# -- the streaming runner (real subprocess, no network) -----------------------------

def test_stream_runner_feeds_lines_and_can_be_cancelled(tmp_path):
    script = tmp_path / "talk.py"
    script.write_text("import sys, time\nprint('{\"a\": 1}', flush=True)\nsys.stdin.read()\n"
                      "print('{\"b\": 2}', flush=True)\ntime.sleep(30)\n", encoding="utf-8")
    lines = []
    ctl = claude_code.RunControl(on_line=lines.append)
    threading.Timer(1.0, ctl.cancel).start()
    t0 = time.monotonic()
    done = claude_code._stream_runner([sys.executable, "-I", str(script)], input="hi", env={"PATH": ""},
                                      cwd=str(tmp_path), timeout=20, ctl=ctl)
    assert time.monotonic() - t0 < 10 and ctl.streamed
    assert [ln.strip() for ln in lines] == ['{"a": 1}', '{"b": 2}'] and done.returncode != 0


def test_stream_runner_times_out(tmp_path):
    script = tmp_path / "slow.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    with pytest.raises(subprocess.TimeoutExpired):
        claude_code._stream_runner([sys.executable, "-I", str(script)], input="", env={"PATH": ""},
                                   cwd=str(tmp_path), timeout=0.5, ctl=claude_code.RunControl())
