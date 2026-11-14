"""The fast voice front (assistant/voice_front.py): offline, fake streaming model, no Claude Code."""

from __future__ import annotations

import threading
import time

import pytest

import config
import db
from assistant import claude_code, runtime, store, voice_front
from assistant import settings as asettings
from assistant_fixtures import assistant_env, fake_model  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")


@pytest.fixture(autouse=True)
def _front(monkeypatch):
    voice_front.reset()

    def no_claude_code(*a, **k):
        raise AssertionError("the fast path must not start Claude Code")

    monkeypatch.setattr(claude_code, "run_prompt", no_claude_code)
    yield
    voice_front.reset()


def _reply(text: str, *tool_calls: tuple[str, dict]) -> runtime.ModelReply:
    content: list[dict] = [{"type": "text", "text": text}] if text else []
    uses = []
    for i, (name, inp) in enumerate(tool_calls):
        content.append({"type": "tool_use", "id": f"tu{i}", "name": name, "input": inp})
        uses.append(runtime.ToolUse(f"tu{i}", name, inp))
    return runtime.ModelReply(content, text, uses, "tool_use" if uses else "end_turn",
                              runtime.Usage(400, 40), "claude-haiku-5-5", "fake")


def test_whats_going_on_is_answered_from_the_status_snapshot_in_one_fast_call(fake_model, monkeypatch):
    """Eval: "what's going on" -> one front call, answered from <status>; no tools, no full agent."""
    db.add_decision("strategy_change", "Switch the Shorts niche to Mars rovers")
    store.add_income(250.0, source="job", note="paycheck")
    monkeypatch.setattr(runtime, "run_turn", lambda *a, **k: pytest.fail("full agent used for a status question"))
    seen = {}

    def model(req):
        seen["user"] = req.messages[0]["content"]
        seen["tools"] = {t["name"] for t in req.tools}
        assert req.model == "claude-haiku-5-5" and req.max_tokens <= 300 and req.effort == "low"
        assert "Approvals waiting: 1" in seen["user"] and "Mars rovers" in seen["user"]
        return ("Quiet morning, sir. One approval is waiting: the Shorts niche change to Mars rovers. "
                "You have logged two hundred and fifty dollars this week.")

    fake_model.queue(model)
    heard = []
    res = voice_front.respond("what's going on", on_sentence=heard.append)
    assert res.status == "ok" and res.task_id is None
    assert len(fake_model.requests) == 1                      # one fast call
    assert heard == res.sentences and len(heard) == 3 and heard[0] == "Quiet morning, sir."
    assert "<status>" in seen["user"] and "income logged $250.00" in seen["user"]
    assert {"delegate_task", "status_snapshot", "memory_search"} <= seen["tools"]
    rows = [r for r in db.list_llm_usage(10) if r.purpose == "assistant.voice_front"]
    assert rows and rows[0].tier == "assistant"
    msgs = store.list_messages(5, channel="voice")
    assert [m.direction for m in msgs[:2]] == ["out", "in"] and msgs[0].body == res.text


def test_quick_tool_runs_in_process(fake_model):
    fake_model.queue(_reply("", ("memory_search", {"query": "dentist"})), "Nothing in your notes about a dentist, sir.")
    res = voice_front.respond("when is my dentist appointment")
    assert res.text == "Nothing in your notes about a dentist, sir."
    tool_results = fake_model.requests[1].messages[-1]["content"]
    assert tool_results[0]["type"] == "tool_result" and "<untrusted" in tool_results[0]["content"]


def test_delegation_acknowledges_at_once_and_speaks_the_summary_when_done(fake_model):
    release = threading.Event()
    briefs = []

    def runner(brief, context):
        briefs.append((brief, context))
        release.wait(5)
        return "I ran the tests: three failures in pick.py, all fixed and committed on sterling/self.", "ok"

    voice_front.set_task_runner(runner)
    announce = voice_front.Announcer("voice")
    assert announce() == []                                    # primes: older events are never replayed
    fake_model.queue(_reply("Right away, sir. I'll look into that.",
                            ("delegate_task", {"task": "Run the test suite and fix failures"})))
    fake_model.queue_for("voice_front_summary", "Sir, about the tests: three were failing in pick and all are fixed.")
    t0 = time.monotonic()
    res = voice_front.respond("can you fix the failing tests", origin="voice")
    assert time.monotonic() - t0 < 1.0
    assert res.text == "Right away, sir. I'll look into that." and res.task_id
    assert len(fake_model.requests) == 1                      # no second round after a pure delegation
    task = voice_front.get_task(res.task_id)
    assert task["status"] == "running"
    assert voice_front.running_tasks()[0]["task"] == "Run the test suite and fix failures"
    assert "Working" not in res.text and "Background task running" in voice_front.status_snapshot()
    release.set()
    assert voice_front.wait(res.task_id, 5)
    assert "can you fix the failing tests" in briefs[0][0] and "status" in briefs[0][1]
    assert voice_front.get_task(res.task_id)["status"] == "done"
    said = announce()
    assert said == ["Sir, about the tests: three were failing in pick and all are fixed."]
    assert announce() == []                                    # once only
    assert voice_front.Announcer("browser")() == []            # other surface: primes, never speaks it
    detail = [m for m in store.list_messages(5, channel="dashboard") if m.kind == "voice_detail"]
    assert detail and "sterling/self" in detail[0].body       # the full result is kept in history
    ev = voice_front.task_events(0)[-1]
    assert ev["origin"] == "voice" and ev["task_id"] == res.task_id


def test_delegated_task_uses_a_fresh_voice_task_session(fake_model, monkeypatch):
    calls = []

    def run_turn(message, *, channel, context=None, **kw):
        calls.append((message, channel, context))
        return runtime.TurnResult("Done: the report is written.", 0.0, [], [], "ok")

    monkeypatch.setattr(runtime, "run_turn", run_turn)
    fake_model.queue(_reply("", ("delegate_task", {"task": "Write the weekly report"})))
    res = voice_front.respond("write the weekly report")
    assert res.text == "Right away, sir. I'll look into that."   # canned ack when the model said nothing
    voice_front.wait(res.task_id, 5)
    assert calls[0][1] == "voice-task" and "<voice_task>" in calls[0][0]
    assert "voice-task" not in claude_code.SESSION_CHANNELS and claude_code.session_for("voice-task") is None


def test_voice_replies_never_point_at_the_dashboard(fake_model):
    fake_model.queue("You have two uploads today, sir. The details are on your dashboard. "
                     "I've pasted the full list on the desktop. See https://example.com for more.")
    res = voice_front.respond("how are the shorts doing")
    assert "dashboard" not in res.text.lower() and "desktop" not in res.text.lower()
    assert "http" not in res.text and res.text.startswith("You have two uploads today")


def test_without_an_api_key_falls_back_to_claude_code_haiku_one_turn(monkeypatch):
    asettings.set_assistant_settings(asettings.get_assistant_settings().replace(fake=False, enabled=True))
    assert not config.get_settings().anthropic_api_key
    assert isinstance(voice_front.get_backend(), voice_front.ClaudeCodeFront)
    seen = []

    def run_prompt(prompt, **kw):
        seen.append(kw)
        text = "DELEGATE: Check the pipeline logs for errors\nRight away, sir." if "logs" in prompt else \
            "All quiet, sir. Nothing needs you."
        return claude_code.CCResult(text=text, model="claude-haiku-5-5", alias="haiku", input_tokens=500,
                                    output_tokens=20, cache_read_tokens=0, cache_write_tokens=0, notional_usd=0.001,
                                    session_id="s1", num_turns=1, status="ok")

    monkeypatch.setattr(claude_code, "run_prompt", run_prompt)
    voice_front.set_task_runner(lambda brief, ctx: ("No errors in the logs.", "ok"))
    res = voice_front.respond("anything new")
    assert res.text == "All quiet, sir. Nothing needs you."
    kw = seen[0]
    assert kw["model"] == "haiku" and kw["max_turns"] == 1 and kw.get("mcp") is None and not kw.get("full")
    res2 = voice_front.respond("look at the logs for me")
    assert res2.task_id and res2.text == "Right away, sir."
    voice_front.wait(res2.task_id, 5)
    # with a key saved the direct API is used instead (one cached client)
    config.set_settings(config.get_settings().replace(anthropic_api_key="sk-test"))
    b = voice_front.get_backend()
    assert isinstance(b, runtime.AnthropicBackend) and voice_front.get_backend() is b


def test_budget_is_respected(fake_model):
    s = asettings.get_assistant_settings()
    asettings.set_assistant_settings(s.replace(daily_budget_usd=0.0))
    res = voice_front.respond("what's going on")
    assert res.status == "budget" and not fake_model.requests
    assert "spending limit" in res.text
    db.kv_set("budget:paused", {"by": "test"})
    asettings.set_assistant_settings(s)
    assert voice_front.respond("hello").status == "budget" and not fake_model.requests


def test_stopped_assistant_says_so_without_a_model_call(fake_model):
    runtime.set_stopped(True, via="test")
    res = voice_front.respond("hello")
    assert res.status == "stopped" and not fake_model.requests


def test_sentence_stream_splits_as_text_arrives():
    st = voice_front.SentenceStream()
    assert st.feed("Good morning, ") == []
    assert st.feed("sir.") == []                                # could still be "sir.”" or an abbreviation
    assert st.feed(" You have ") == ["Good morning, sir."]      # out as soon as sentence two begins
    assert st.feed("two approvals. Mr. Smith called.") == ["You have two approvals."]
    assert st.flush() == ["Mr. Smith called."]


def test_fast_path_overhead_in_fake_mode(fake_model):
    """Measured plain-code overhead (snapshot + prompt + fake stream + recording); real latency adds the
    model's time to first token (~0.5-1 s for Haiku) and TTS of sentence one."""
    fake_model.queue(*["Two uploads today, sir. Nothing needs you."] * 5)
    voice_front.respond("warm up")
    times = []
    for _ in range(4):
        r = voice_front.respond("what's going on")
        times.append((r.first_sentence_ms, r.total_ms))
    first = sorted(t[0] for t in times)[len(times) // 2]
    print(f"\nvoice front fake-mode overhead: first sentence {first:.1f} ms, "
          f"whole turn {sorted(t[1] for t in times)[len(times) // 2]:.1f} ms")
    assert first < 500
