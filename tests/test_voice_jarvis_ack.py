"""static/jarvis.js: the instant acknowledgement block (classifier + timing), run in node with a fake clock."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = (Path(__file__).resolve().parent.parent / "static" / "jarvis.js").read_text(encoding="utf-8")
BLOCK = JS[JS.index("/* ack:begin"):JS.index("/* ack:end */")]

HARNESS = r"""
%s
var out = { kinds: {}, timeline: [] };
var cases = %s;
cases.forEach(function (c) { out.kinds[c] = ACK.kind(c); });
var now = 0, timers = [], seq = 0;
function later(f, ms) { var id = ++seq; timers.push({ id: id, at: now + ms, f: f }); return id; }
function cancel(id) { timers = timers.filter(function (t) { return t.id !== id; }); }
function advance(ms) {
  var end = now + ms;
  timers.sort(function (a, b) { return a.at - b.at; });
  while (timers.length && timers[0].at <= end) { var t = timers.shift(); now = t.at; t.f(); }
  now = end;
}
var a = ACK.make({ h: 'sir', later: later, cancel: cancel, say: function (t) { out.timeline.push([now, t]); } });
a.start('hello');                                  // chat: nothing
a.start('search the web for the Ohio State score'); // task: ack at once
a.start('and the Michigan one');                   // further breath: no second ack
advance(9000);                                     // no answer: one "still working" at 8 s
advance(20000);                                    // never twice
a.answered();
for (var i = 0; i < 12; i++) { a.start('open youtube'); a.answered(); }
var b = ACK.make({ h: 'sir', later: later, cancel: cancel, say: function (t) { out.timeline.push([now, 'B:' + t]); } });
b.start('check my calendar'); advance(500); b.answered(); advance(10000);
console.log(JSON.stringify(out));
"""

TASKS = ["open YouTube", "search the web for the Ohio State score", "what's the weather tomorrow",
         "log eight hours for Monday", "change your voice to Lewis", "play some jazz", "show me Paris on a map",
         "check my email", "find the invoice from last week",
         "tell me everything you know about the history of the Roman empire"]
CHATS = ["hello", "Hello there.", "how are you", "How are you doing today?", "thanks", "good morning sir",
         "what time is it", "yes", "never mind", "you there?"]


@pytest.fixture(scope="module")
def result():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = HARNESS % (BLOCK, json.dumps(TASKS + CHATS))
    p = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(p.stdout)


def test_classifier_is_simple_and_local(result):
    for t in TASKS:
        assert result["kinds"][t] == "task", t
    for c in CHATS:
        assert result["kinds"][c] == "chat", c


def test_ack_at_once_still_working_at_8s_once_and_never_the_same_line_twice(result):
    tl = result["timeline"]
    first, still = tl[0], tl[1]
    assert first[0] == 0 and first[1] in ("One moment, sir.", "On it, sir.", "Right away, sir.", "Very good, sir.")
    assert still == [8000, "Still working on it, sir."]
    assert sum(1 for _, t in tl if t.startswith("Still")) == 1           # once, and not after an answer (B)
    acks = [t for _, t in tl[2:] if not t.startswith("B:")]
    assert len(acks) == 12 and all(x != y for x, y in zip(acks, acks[1:]))
    assert [t for _, t in tl if t.startswith("B:")] == [tl[-1][1]] and "Still" not in tl[-1][1]


def test_page_wires_the_ack_and_single_flight():
    assert "acker.start(text)" in JS and "speaker.say(t, null, true, null, true)" in JS   # browser voice
    assert "function newSpeech()" in JS and "!acker.busy()" in JS
    assert "j.duplicate" in JS and "brainStages(" in JS
