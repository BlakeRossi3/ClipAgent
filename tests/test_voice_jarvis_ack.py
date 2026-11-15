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
a.start('hello'); advance(1000); a.answered();     // chat answered within 1.5 s: nothing
a.start('search the web for the Ohio State score'); // task: ack at once
a.start('and the Michigan one');                   // further breath: no second ack
advance(18000);                                    // no answer: progress at 5 s, 11 s, 17 s
a.answered(); advance(60000);                      // answered: silence
out.mark = out.timeline.length;
var c = ACK.make({ h: 'sir', later: later, cancel: cancel, say: function (t) { out.timeline.push([now, 'C:' + t]); } });
var c0 = now; c.start('how are you'); advance(1600); out.lateAt = now - c0; c.answered(); advance(30000);  // slow chat: ack at 1.5 s
var d = ACK.make({ h: 'sir', later: later, cancel: cancel, say: function (t) { out.timeline.push([now, 'D:' + t]); },
                   progress: function () { return 'Searching the web, sir.'; } });
d.start('open netflix'); advance(5100); d.answered();   // the running step is named
var e = ACK.make({ h: 'sir', later: later, cancel: cancel, say: function (t) { out.timeline.push([now, 'E:' + t]); } });
e.start('research everything about mars'); advance(300000); e.answered();   // capped
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


def test_ack_at_once_then_progress_lines_and_never_silent(result):
    tl = result["timeline"]
    a = tl[:result["mark"]]
    assert a[0][0] == 1000 and a[0][1] in ("One moment, sir.", "On it, sir.", "Right away, sir.", "Very good, sir.")
    assert [at - 1000 for at, _ in a[1:]] == [5000, 11000, 17000]         # progress while it works, then silence
    assert a[1][1] == "Still working on it, sir." and all(x[1] != y[1] for x, y in zip(a, a[1:]))
    c = [t for _, t in tl if t.startswith("C:")]
    assert len(c) == 1 and result["lateAt"] == 1600                       # slow chat: one ack (at 1.5 s), no more once answered
    d = [t for _, t in tl if t.startswith("D:")]
    assert len(d) == 2 and d[1] == "D:Searching the web, sir."            # the running step is named
    e = [t for _, t in tl if t.startswith("E:")]
    assert len(e) == 1 + 6 and all(x != y for x, y in zip(e, e[1:]))      # capped, never the same line twice running
    acks = [t for _, t in tl if not t[:2] in ("B:", "C:", "D:", "E:")][result["mark"]:]
    assert len(acks) == 12 and all(x != y for x, y in zip(acks, acks[1:]))
    assert [t for _, t in tl if t.startswith("B:")] and not any("Still" in t for _, t in tl if t.startswith("B:"))


def test_page_wires_the_ack_and_single_flight():
    assert "acker.start(text)" in JS and "speaker.say(t, null, true, null, true)" in JS   # browser voice
    assert "function newSpeech()" in JS and "!acker.busy()" in JS
    assert "j.duplicate" in JS and "brainStages(" in JS
