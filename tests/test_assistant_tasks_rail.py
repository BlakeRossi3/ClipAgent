"""Expandable background-task rows on the Jarvis rail.

assistant/tasks.py keeps the full result on the task row (``result``) and publishes it with ``heard``;
static/jarvis.js's TASKROW block (pure, run here in node) turns one row into a collapsed line and an
expanded detail; the page wires a click toggle on each row.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import config_store
from assistant import events, tasks
from assistant import settings as asettings
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "static" / "jarvis.js").read_text(encoding="utf-8")
BLOCK = JS[JS.index("/* taskrow:begin"):JS.index("/* taskrow:end */")]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPAGENT_CONFIG_FILE", str(tmp_path / "cfg" / "config.env"))
    monkeypatch.setenv("ASSISTANT_ENABLED", "true")
    monkeypatch.setenv("FAKE_ASSISTANT", "true")
    config_store.reload()
    asettings.reset_assistant_settings()
    yield tmp_path


LONG = "The logs are clean. " + " ".join(f"Line {i} checked and fine." for i in range(40))


def test_finished_task_keeps_the_full_result_for_the_expanded_row(env):
    tasks.set_runner(lambda brief, ctx, on_step: (on_step("Reading the logs"), (LONG, "ok"))[-1])
    tid = tasks.start("look at the logs", heard="have a look at the logs for me", origin="voice")
    assert tasks.wait(tid, 5)
    t = tasks.get_task(tid)
    assert t["summary"] == "The logs are clean."                 # the collapsed line: one sentence
    assert t["result"].startswith("The logs are clean. Line 0") and "Line 39 checked" in t["result"]
    pub = tasks.public(t)
    assert pub["result"] == t["result"] and pub["heard"] == "have a look at the logs for me"
    sent = [e for e in events.BUS.since(0, 1000) if e["type"] == "task" and e["task"]["id"] == tid]
    assert sent[-1]["task"]["result"] == t["result"]             # the page gets it on the stream


def test_a_huge_result_is_capped_on_the_row(env):
    big = "Word. " * 2000
    tasks.set_runner(lambda brief, ctx: (big, "ok"))
    tid = tasks.start("write a lot")
    assert tasks.wait(tid, 5)
    r = tasks.get_task(tid)["result"]
    assert 1000 < len(r) <= tasks.RESULT_CHARS and r.startswith("Word. Word.")


def test_a_running_task_has_no_result_yet(env):
    row = {"id": "x1", "task": "t", "status": "running", "steps": []}
    assert tasks.public(row)["result"] is None


HARNESS = r"""
%s
var now = Date.parse('2026-11-15T12:05:00Z');
var out = {
  running: TASKROW.view({ id: 'a', task: 'Fix the tests', heard: 'make the tests pass', status: 'running',
                          started: '2026-11-15T12:03:30Z', steps: [{ label: 'Reading code' }, { label: 'Running tests' }] }, now),
  done: TASKROW.view({ id: 'b', task: 'Check logs', heard: 'Check logs', status: 'done', started: '2026-11-15T12:00:00Z',
                       finished: '2026-11-15T12:00:42Z', summary: 'All clean.', result: 'All clean.\nNo errors overnight.',
                       steps: [{ label: 'Reading' }] }, now),
  old: TASKROW.view({ id: 'c', task: 'Old', status: 'error', started: '2026-11-15T12:00:00Z',
                      finished: '2026-11-15T12:01:00Z', summary: 'Broke.' }, now),
  empty: TASKROW.view({}, now)
};
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def rows():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    p = subprocess.run([node, "-e", HARNESS % BLOCK], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(p.stdout)


def test_collapsed_line_is_short_and_says_where_it_is(rows):
    assert rows["running"]["line"] == "Working · 1m 30s · Running tests"
    assert rows["done"]["line"] == "Done · 42s · All clean."
    assert rows["old"]["line"] == "Error · 1m 0s · Broke."
    assert rows["empty"]["title"] == "Task" and rows["empty"]["status"] == "running"


def test_expanded_detail_has_request_steps_and_full_result(rows):
    d = rows["running"]["detail"]
    assert d["request"] == "Fix the tests" and d["heard"] == "make the tests pass"
    assert d["steps"] == ["Reading code", "Running tests"] and d["result"] == ""
    assert d["meta"] == "Running for 1m 30s · 2 steps"
    d = rows["done"]["detail"]
    assert d["result"] == "All clean.\nNo errors overnight." and d["heard"] == ""   # same as the task: not repeated
    assert d["meta"] == "Done in 42s · 1 step"
    assert rows["old"]["detail"]["result"] == "Broke."                              # older rows fall back to the summary


def test_page_wires_rows_as_click_toggles():
    assert "var openTasks = {}" in JS and "TASKROW.view(t, Date.now())" in JS
    assert "'rail-task-head'" in JS and "aria-expanded" in JS and "closest('.rail-task-head')" in JS
    css = (ROOT / "static" / "home.css").read_text(encoding="utf-8")
    assert ".rail-task-head" in css and ".rail-task-detail" in css and ".rail-task.is-open" in css
