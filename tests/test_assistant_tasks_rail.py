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
  empty: TASKROW.view({}, now),
  retrying: TASKROW.view({ id: 'd', task: 'Memory pass', status: 'retrying', started: '2026-11-15T11:55:40Z',
                           next_try: '2026-11-15T12:09:10Z', last_error: 'auth: Not logged in', attempts: 2 }, now),
  retry_due: TASKROW.view({ id: 'e', task: 'Backlog pass', status: 'retrying', started: '2026-11-15T11:50:00Z',
                            next_try: '2026-11-15T12:04:00Z' }, now),
  failed: TASKROW.view({ id: 'f', task: 'Brief', status: 'error', started: '2026-11-15T12:00:00Z',
                         finished: '2026-11-15T12:00:01Z', summary: 'Something went wrong.',
                         last_error: 'blocked: Claude Code could not start' }, now)
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


RECONCILE = JS[JS.index("/* reconcile:begin"):JS.index("/* reconcile:end */")]
FAKE_DOM = r"""
function N(attrs, kids) {
  var n = { attrs: attrs || {}, children: kids || [], scrollTop: 0, parentElement: null, textContent: '', title: '' };
  n.children.forEach(function (c) { c.parentElement = n; });
  n.getAttribute = function (k) { return k in n.attrs ? n.attrs[k] : null; };
  n.querySelectorAll = function (sel) {            // only '[attr]' selectors are used here
    var a = sel.slice(1, -1), out = [];
    (function walk(x) { x.children.forEach(function (c) { if (a in c.attrs) out.push(c); walk(c); }); })(n);
    return out;
  };
  n.replaceChild = function (nw, old) { n.children[n.children.indexOf(old)] = nw; nw.parentElement = n; };
  n.appendChild = function (c) { n.children.push(c); c.parentElement = n; };
  n.removeChild = function (c) { n.children.splice(n.children.indexOf(c), 1); };
  Object.defineProperty(n, 'lastChild', { get: function () { return n.children[n.children.length - 1]; } });
  return n;
}
%s
function row(k) { return N({ 'data-k': k }, [N({ 'data-keep-scroll': 'task-a' })]); }
var list = N({}, [row('a|1 step')]);
var rail = N({}, [list]); rail.scrollTop = 140;
list.children[0].children[0].scrollTop = 230;      // he scrolled halfway down the open task
reconcile(list, [row('a|2 steps')]);                // a new step arrives: the row is rebuilt
var kept = list.children[0].children[0].scrollTop;
var same = list.children[0].getAttribute('data-k');
console.log(JSON.stringify({ kept: kept, same: same, rail: rail.scrollTop }));
"""


def test_an_open_task_keeps_its_scroll_when_the_row_is_rebuilt():
    """Blake: "I scroll through a task and it jumps back to the top as soon as I stop scrolling"."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    p = subprocess.run([node, "-e", FAKE_DOM % RECONCILE], capture_output=True, text=True, timeout=30, check=True)
    out = json.loads(p.stdout)
    assert out["same"] == "a|2 steps"           # the row really was replaced...
    assert out["kept"] == 230                   # ...and the detail box is still where he left it
    assert out["rail"] == 140                   # the rail around it did not move either
    assert 'data-keep-scroll' in JS and "box.setAttribute('data-keep-scroll'" in JS


def test_retrying_rows_count_down_to_the_next_try_and_say_why(rows):
    """Blake: "it says retrying 9 minutes and 20 seconds and it's counting up" - it counts down now."""
    assert rows["retrying"]["line"] == "Retrying in 4m 10s · auth: Not logged in"
    assert rows["retry_due"]["line"] == "Retrying now · waiting for a free slot"
    assert rows["failed"]["line"] == "Error · 1s · blocked: Claude Code could not start"
