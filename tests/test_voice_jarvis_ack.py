"""static/jarvis.js: the narrator's safety net (the ack block), run in node with a fake clock.

What Cecil says while he works is the model's own judgement; this block only covers silence, with a
patience that scales with the step that is running (learned durations) and with how long the request has run.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "static" / "jarvis.js").read_text(encoding="utf-8")
BLOCK = JS[JS.index("/* ack:begin"):JS.index("/* ack:end */")]

HARNESS = r"""
%s
var now = 0, timers = [], seq = 0, out = {};
function later(f, ms) { var id = ++seq; timers.push({ id: id, at: now + ms, f: f }); return id; }
function cancel(id) { timers = timers.filter(function (t) { return t.id !== id; }); }
function advance(ms) {
  var end = now + ms;
  for (;;) {
    timers.sort(function (a, b) { return a.at - b.at; });
    if (!timers.length || timers[0].at > end) break;
    var t = timers.shift(); now = t.at; t.f();
  }
  now = end;
}
function lineFor(name, label) {
  if (name === 'open_url') return label + ', sir.';
  if (name === 'WebSearch') return 'Searching.';
  if (name === 'memory_search') return 'Checking my notes.';
  return null;
}
function run(name, script, table) {
  var said = [], t0 = now;
  var n = ACK.make({ h: 'sir', later: later, cancel: cancel, now: function () { return now; }, lineFor: lineFor,
                     table: table, say: function (t, kind) { said.push([now - t0, t, kind]); } });
  script(n);
  n.stop(); advance(600000);                    // after the turn ends: never another word
  out[name] = said;
  return n;
}
// 1. answerable now: the answer comes within a second -> nothing extra
run('quick', function (n) { n.start(); advance(900); n.sentence(); advance(60000); });
// 2. quick visible action, Cecil said his own line before the tool -> nothing from the net
run('action_said', function (n) { n.start(); advance(600); n.sentence(); advance(100); n.tool('open_url', 'Opening YouTube'); advance(900); });
// 3. a visible action nobody announced -> its own line at once, once
run('action_silent', function (n) { n.start(); advance(400); n.tool('open_url', 'Opening a page'); advance(100); n.relabel('open_url', 'Opening YouTube'); advance(1000); });
// 4. a fast internal step, then the answer -> nothing
run('fast_tool', function (n) { n.start(); advance(300); n.tool('memory_search', 'Checking my notes'); advance(900); n.sentence(); advance(30000); });
// 5. a slow step first, nothing said -> that step's line at 2 s, then quiet
run('slow_tool', function (n) { n.start(); advance(500); n.tool('WebSearch', 'Searching the web: score'); advance(1500); n.sentence(); advance(60000); });
// 6. nothing at all for 2 s -> one neutral line, and no more without a running step
run('silence', function (n) { n.start(); advance(30000); });
// 7. a live lookup where Cecil keeps up a running commentary -> the net never speaks
run('commentary', function (n) {
  n.start(); advance(700); n.sentence();
  for (var i = 0; i < 6; i++) { n.tool('WebSearch', 'Searching the web'); advance(4000); n.sentence(); }
  advance(500);
});
// 8. a long job after Cecil's own start line -> "still" lines only after long silences, further apart, capped
run('long', function (n) { n.start(); advance(800); n.sentence(); advance(100); n.tool('Bash', 'Running a command'); advance(400000); });
// 9. interrupted: everything stops at once
run('interrupted', function (n) { n.start(); advance(1500); n.stop(); advance(60000); });
// 10. the learned table changes the decision: web searches that take 40 s -> much more patience
var slow = ACK.table(); for (var k = 0; k < 3; k++) slow.learn('WebSearch', 40000);
run('learned', function (n) { n.start(); advance(800); n.sentence(); advance(100); n.tool('WebSearch', 'Searching'); advance(200000); }, slow);
run('seeded', function (n) { n.start(); advance(800); n.sentence(); advance(100); n.tool('WebSearch', 'Searching'); advance(200000); });
// 11. a step whose learned duration is short postpones the first line: the answer is about to come
var quick = ACK.table(); quick.learn('Grep', 1000);
run('soon', function (n) { n.start(); advance(1500); n.tool('Grep', 'Searching the code'); advance(1900); n.sentence(); advance(9000); }, quick);
// 12. never the same neutral line twice running, across requests
var lines = [];
for (var r = 0; r < 12; r++) {
  var n = ACK.make({ h: 'sir', later: later, cancel: cancel, now: function () { return now; },
                     say: function (t) { lines.push(t); } });
  n.start(); advance(2500); n.stop();
}
out.repeats = lines;
var tb = ACK.table(); tb.learn('X', 5); tb.learn('X', 500); tb.learn('X', 50);
out.median = tb.expected('X'); out.seed = tb.expected('WebSearch'); out.unknown = tb.expected('nope');
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def r():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    p = subprocess.run([node, "-e", HARNESS % BLOCK], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(p.stdout)


def texts(rows):
    return [t for _, t, _ in rows]


def test_quick_answers_and_announced_actions_get_nothing_extra(r):
    assert r["quick"] == [] and r["action_said"] == [] and r["fast_tool"] == [] and r["interrupted"] == []


def test_a_silent_visible_action_is_named_at_once_and_only_once(r):
    assert r["action_silent"] == [[700, "Opening YouTube, sir.", "step"]]   # named once its input arrived


def test_a_slow_first_step_gets_its_own_line_after_two_seconds(r):
    assert r["slow_tool"] == [[2000, "Searching.", "step"]]


def test_two_seconds_of_nothing_gets_one_neutral_line(r):
    assert len(r["silence"]) == 1 and r["silence"][0][0] == 2000 and r["silence"][0][2] == "quiet"
    assert r["silence"][0][1] in ("One moment, sir.", "Bear with me, sir.", "Just a moment, sir.")


def test_a_running_commentary_from_cecil_keeps_the_net_quiet(r):
    assert r["commentary"] == []


def test_long_work_gets_sparse_still_lines_further_apart_and_capped(r):
    rows = r["long"]
    assert 1 <= len(rows) <= 3 and all(k == "still" for _, _, k in rows)
    assert rows[0][0] >= 800 + 15000                      # nothing before a real silence
    gaps = [b[0] - a[0] for a, b in zip(rows, rows[1:])]
    assert all(g2 > g1 for g1, g2 in zip(gaps, gaps[1:])) and all(g >= 30000 for g in gaps)
    assert all(a != b for a, b in zip(texts(rows), texts(rows)[1:]))


def test_learned_step_durations_scale_the_patience(r):
    seeded, learned = r["seeded"][0][0], r["learned"][0][0]
    assert learned >= 800 + 80000 and learned >= 4 * (seeded - 900)
    assert r["median"] == 50 and r["seed"] == 6000 and r["unknown"] == 2500


def test_a_step_expected_to_finish_soon_postpones_the_first_line(r):
    assert r["soon"] == []                                # Grep usually takes 1 s: the answer came at 3.4 s


def test_never_the_same_line_twice_running(r):
    lines = r["repeats"]
    assert len(lines) == 12 and all(a != b for a, b in zip(lines, lines[1:]))


def test_page_wires_the_narrator_to_the_stream_and_shows_the_trail():
    assert "acker.start();" in JS and "acker.sentence()" in JS and "acker.tool(e.name, e.label)" in JS
    assert "learnTimings(e.timing.brain)" in JS and "localStorage.setItem(LAT_KEY" in JS
    assert "function newSpeech()" in JS and "acker.stop()" in JS and "j.duplicate" in JS and "brainStages(" in JS
    assert "function renderTrail()" in JS and "Did not finish" in JS
    html = (ROOT / "templates" / "home.html").read_text(encoding="utf-8")
    assert "data-jv-trail" in html and "data-jv-trail-list" in html
