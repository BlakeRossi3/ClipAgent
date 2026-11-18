"""Self-healing: failures are grouped, given a grace period, repaired by a background task, and reported."""

from __future__ import annotations

import pytest

import db
from assistant import autofix, store, tasks
from assistant import settings as asettings
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")


@pytest.fixture
def on(monkeypatch, clock):
    monkeypatch.setenv("ASSISTANT_ENABLED", "true")
    asettings.reset_assistant_settings()
    briefs = []
    reply = {"text": "STATUS: fixed\nWHAT: Whisper ran out of memory on long videos.\nFIX: Switched to int8 and "
                     "re-ran source 12."}

    def runner(brief, context):
        briefs.append(brief)
        return reply["text"], "ok"

    tasks.set_runner(runner)
    yield {"briefs": briefs, "reply": reply}
    tasks.set_runner(None)


def _fail(msg="transcription failed for source 12", module="transcribe", **data):
    db.log_event(module, msg, level="error", data=data or {"error": "CUDA out of memory"})


def _run(clock):
    out = autofix.tick()
    if out.get("task_id"):
        assert tasks.wait(out["task_id"], 30)    # a slow finish must never outlive the test (see conftest)
    return out


def _texts():
    return [m.body for m in store.list_messages(20, direction="out") if m.channel == "sms"]


def test_signature_groups_the_same_failure():
    a = autofix.signature("transcribe", "transcription failed for source 12: 'C:\\clips\\a.mp4'")
    b = autofix.signature("transcribe", "transcription failed for source 97: 'D:/other/b.mp4'")
    assert a == b and a != autofix.signature("upload", "transcription failed for source 12")


def test_waits_for_the_grace_period_then_repairs_and_tells_him(on, clock):
    _fail()
    _fail("transcription failed for source 13")
    assert autofix.tick()["status"] == "nothing"            # normal retries go first
    clock.advance(minutes=4)
    out = _run(clock)
    assert out["status"] == "started"
    (brief,) = on["briefs"]
    assert "transcription failed for source 13" in brief and "2 time(s)" in brief and "STATUS:" in brief
    row = tasks.get_task(out["task_id"])
    assert row["meta"]["label"] == "Self-repair" and row["status"] == "done"
    texts = _texts()
    assert texts and texts[0].startswith("Fixed something: Whisper ran out of memory")
    assert autofix.history()[0]["status"] == "fixed"
    assert autofix.tick()["status"] == "nothing"             # the same problem is not taken on again soon


def test_needs_you_and_transient(on, clock):
    on["reply"]["text"] = ("STATUS: needs_you\nWHAT: The call to you failed: Twilio says the number is unverified.\n"
                           "FIX: Verify your number in the Twilio console.")
    _fail("call to owner failed (approval)", module="notify")
    clock.advance(minutes=4)
    _run(clock)
    assert "only you can fix" in _texts()[0] and "Verify your number" in _texts()[0]
    on["reply"]["text"] = "STATUS: transient\nWHAT: A network blip.\nFIX: Nothing; it recovered."
    _fail("upload failed: connection reset", module="upload")
    clock.advance(minutes=4)
    n = len(_texts())
    _run(clock)
    assert len(_texts()) == n and autofix.history()[0]["status"] == "transient"


def test_its_own_failures_never_start_a_repair(on, clock):
    db.log_event("autofix", "repair started: x", level="error", data={"autofix": True})
    tid = tasks.track("Fix: something", meta={"kind": "autofix"})
    db.log_event("assistant", "background task failed", level="error", data={"task_id": tid})
    clock.advance(minutes=10)
    assert autofix.problems() == [] and autofix.tick()["status"] == "nothing"


def test_daily_cap_off_switch_and_one_at_a_time(on, clock, monkeypatch):
    _fail()
    clock.advance(minutes=4)
    asettings.set_assistant_settings(asettings.get_assistant_settings().replace(autofix_max_per_day=0))
    assert autofix.tick()["status"] == "cap"
    asettings.set_assistant_settings(asettings.get_assistant_settings().replace(autofix=False))
    assert autofix.tick()["status"] == "off"
    asettings.set_assistant_settings(asettings.get_assistant_settings().replace(autofix=True, autofix_max_per_day=6))
    tasks.track("Fix: other", meta={"kind": "autofix"})
    assert autofix.tick()["status"] == "busy"


def test_a_repair_that_did_not_finish_is_tried_again_in_an_hour(on, clock):
    on["reply"]["text"] = "ran out of time"
    tasks.set_runner(lambda brief, context: ("plan limit", "budget"))
    _fail()
    clock.advance(minutes=4)
    _run(clock)
    assert autofix.history()[0]["status"] == "run_budget" and _texts() == []
    clock.advance(minutes=30)
    assert autofix.tick()["status"] == "nothing"
    clock.advance(minutes=40)
    assert autofix.tick()["status"] == "started"



def test_a_repair_that_cannot_run_is_texted_with_its_cause_once(on, clock):
    tasks.set_runner(lambda brief, context: ("could not start Claude Code: WinError 206", "error"))
    _fail()
    clock.advance(minutes=4)
    _run(clock)
    assert autofix.history()[0]["status"] == "run_error" and _texts() == []   # first time: just retry
    clock.advance(minutes=70)
    _fail()
    _run(clock)
    (text,) = _texts()
    assert "would not run" in text and "WinError 206" in text
    clock.advance(minutes=70)
    _fail()
    _run(clock)
    assert len(_texts()) == 1                                                   # told once, not every hour


def test_triage_one_row_per_cause_and_only_unfixable_ones_need_him(on, clock):
    """He asked: never "16 pipeline failures"; fix what you can, tell me the problem you cannot."""
    from assistant import needs

    for i in range(16):
        _fail(f"transcription failed for source {i}")
    _fail("upload rejected: token lacks scopes", module="upload")
    rows = autofix.triage()
    assert len(rows) == 2 and {r["state"] for r in rows} == {"fixing"}
    assert next(r for r in rows if r["module"] == "transcribe")["count"] == 16
    assert needs.data()["count"] == 0 and needs.briefing() == ""          # self-repair has them: quiet

    on["reply"]["text"] = "STATUS: needs_you\nWHAT: The YouTube token lacks scopes.\nFIX: Re-run youtube_auth.py."
    up = next(r for r in rows if r["module"] == "upload")
    autofix.start_repair({**up, "samples": []})
    tasks.join_all(30)
    its = needs.items()
    assert [i["kind"] for i in its] == ["failures"] and "Re-run youtube_auth.py" in its[0]["text"]
    assert "16" not in needs.briefing() and "lacks scopes" in needs.briefing()


def test_triage_fixed_stays_quiet_until_it_comes_back(on, clock):
    _fail()
    clock.advance(minutes=4)
    _run(clock)                                       # default reply: STATUS fixed
    assert [r["state"] for r in autofix.triage()] == ["fixed"]
    clock.advance(minutes=10)
    _fail()
    (row,) = autofix.triage()
    assert row["state"] == "needs_you" and "came back" in row["why"]

def test_join_all_waits_for_slow_task_endings(on):
    import threading

    gate = threading.Event()
    tasks.set_runner(lambda brief, context: (gate.wait(5) and "done", "ok"))
    tid = tasks.start("Slow one", announce=False)
    assert tasks.join_all(0.05) == [tid]          # still running: reported, not abandoned silently
    gate.set()
    assert tasks.join_all(10) == []


def test_parse_is_forgiving():
    assert autofix.parse("blah\n**STATUS:** Fixed.\nWHAT: x\nFIX: y")["status"] == "fixed"
    assert autofix.parse("no idea")["status"] == "not_fixed"
    assert autofix.message_for({"status": "fixed", "what": "a", "fix": "b"}, code_changed=True).endswith(
        "text UNDO to reverse it.")
