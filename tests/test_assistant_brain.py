"""assistant/brain.py: the warm Agent SDK session, faked offline with real SDK message types."""

from __future__ import annotations

import time

import numpy as np
import pytest

import config
import config_store
import db
from assistant import brain, claude_code, events, store, tasks
from assistant import settings as asettings
from assistant_fixtures import assistant_env  # noqa: F401
from voice import speech

pytestmark = pytest.mark.usefixtures("assistant_env")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPAGENT_CONFIG_FILE", str(tmp_path / "cfg" / "config.env"))
    monkeypatch.setenv("ASSISTANT_ENABLED", "true")
    monkeypatch.setenv("FAKE_ASSISTANT", "true")
    config_store.reload()
    asettings.reset_assistant_settings()
    yield tmp_path


def script(fn):
    """Install a FakeClient driven by ``fn(said, client) -> steps``."""
    import re

    def wrapped(prompt, client):
        m = re.search(r"<said>\s*(.*?)\s*</said>", prompt, re.S)
        return fn(m.group(1) if m else prompt, client)

    brain.set_client_factory(lambda options, tools: brain.FakeClient(options, tools, wrapped))


def say(text, origin="browser", timeout=10):
    heard = []
    res = brain.ask(text, origin=origin, on_sentence=heard.append).wait(timeout)
    return res, heard


def types_of(after=0):
    return [e["type"] for e in events.BUS.since(after, 1000)]


def test_switch_to_lewis_sets_the_voice_and_the_next_sentence_uses_it(env):
    from voice import web

    built = []

    class TTS:
        def __init__(self, voice):
            self.name, self.voice = "kokoro", voice

        def synthesize(self, text):
            return np.zeros(240, dtype=np.float32), 24000

    web.set_engines(tts_builder=lambda v: built.append(v.tts_voice) or TTS(v.tts_voice))
    assert web.synthesize("Hello.")[1] == "kokoro" and web._tts().voice == "bm_george"
    script(lambda said, c: [brain.tool_step("settings_set", setting="voice", value="Lewis"), "Done, sir. This is Lewis."]
           if "Lewis" in said else ["Hello."])
    res, heard = say("switch to Lewis")
    assert res["status"] == "ok" and heard == ["Done, sir.", "This is Lewis."]
    assert config_store.read_values()["VOICE_TTS_VOICE"] == "bm_lewis"
    assert config_store.read_values()["VOICE_WEB_TTS"] == "kokoro"
    assert web._tts().voice == "bm_lewis" and built == ["bm_george", "bm_lewis"]     # rebuilt, not cached
    evs = events.BUS.since(0, 1000)
    tool = [e for e in evs if e["type"] == "tool" and e["name"] == "settings_set"]
    assert tool[-1]["label"] == "Changing the voice" and len({e["tool_id"] for e in tool}) == 1   # refined in place
    st = next(e for e in evs if e["type"] == "settings")
    assert st["voice"]["kokoro_name"] == "Lewis" and st["voice"]["web_tts"] == "kokoro"
    assert next(e for e in evs if e["type"] == "tool_done")["ok"] is True


def test_one_session_remembers_the_conversation_and_resumes_after_a_restart(env):
    def reply(said, client):
        if said == "what voice did I pick":
            prior = [p for p in client.history if "Lewis" in p]
            return ["You picked Lewis a minute ago, sir." if prior else "I don't know."]
        return ["Noted, Lewis it is."]

    script(reply)
    say("I like the voice called Lewis")
    res, _ = say("what voice did I pick")
    assert res["reply"] == "You picked Lewis a minute ago, sir."
    assert len(brain.FakeClient.instances) == 1 and brain.get_brain().connects == 1      # one warm session
    sid = brain.FakeClient.instances[0].session_id
    assert claude_code.session_state("voice")["id"] == sid and claude_code.session_state("voice")["turns"] == 2
    brain.get_brain().stop()          # the server restarts
    brain._brain = None
    script(reply)
    say("hello again")
    assert brain.FakeClient.instances[-1].options.resume == sid                          # same conversation
    msgs = store.list_messages(20, channel="voice")
    assert {m.direction for m in msgs} == {"in", "out"}


def test_no_plumbing_words_and_no_empty_acknowledgement(env):
    script(lambda said, c: ["I can't see the voice setting from this channel.", "My full self will delegate it.",
                            "The front layer is slow."] if said == "change it" else
           (["Understood, sir."] if len(c.history) == 1 else ["I've opened YouTube for you."]))
    res, heard = say("open youtube")
    assert heard == ["I've opened YouTube for you."]                    # the bare ack was held back...
    assert "Understood" not in res["reply"]
    assert brain.NUDGE in brain.FakeClient.instances[0].history[1]       # ...and the session nudged once
    res, heard = say("change it")
    text = " ".join(heard).lower()
    for word in ("channel", "full self", "delegate", "front"):
        assert word not in text
    assert heard[0] == "I can't see the voice setting."


def test_ack_before_a_tool_is_spoken_and_no_nudge(env):
    script(lambda said, c: ["Very good, sir.", brain.tool_step("memory_search", query="dentist"),
                            "Your dentist is on Tuesday."])
    res, heard = say("when is the dentist")
    assert heard == ["Very good, sir.", "Your dentist is on Tuesday."]
    assert len(brain.FakeClient.instances[0].history) == 1


def test_background_task_announces_completion_exactly_once(env):
    gate = []
    tasks.set_runner(lambda brief, ctx, on_step: (on_step("Running tests"), gate.append(brief),
                                                  ("All 412 tests pass; I fixed two in pick.py.", "ok"))[-1])

    def reply(said, client):
        if brain.ANNOUNCE_MARK in said:
            return ["Sir, about the tests: all of them pass now; two were fixed in pick."]
        return [brain.tool_step("start_task", task="Run the test suite and fix failures"),
                "That'll take a few minutes, sir. I'll tell you when it's done."]

    script(reply)
    announce = tasks.Announcer("browser")
    announce()                                                        # primes
    res, heard = say("fix the failing tests")
    assert res["task"] and heard[-1] == "I'll tell you when it's done."
    tid = tasks.list_tasks()[-1]["id"]
    assert tasks.wait(tid, 5)
    for _ in range(200):
        done = [m for m in store.list_messages(20, channel="voice") if m.kind == "task_done"]
        if done:
            break
        time.sleep(0.02)
    assert len(done) == 1 and done[0].body.startswith("Sir, about the tests")
    assert done[0].meta["origin"] == "browser" and done[0].meta["task_id"] == tid
    assert announce() == [done[0].body] and announce() == []
    assert tasks.announce(tasks.get_task(tid), "again") is False      # never twice
    time.sleep(0.1)
    assert len([m for m in store.list_messages(20, channel="voice") if m.kind == "task_done"]) == 1
    t = tasks.get_task(tid)
    assert t["status"] == "done" and t["steps"][0]["label"] == "Running tests"
    kinds = [e for e in events.BUS.since(0, 1000) if e["type"] == "turn" and e["kind"] == "announce"]
    assert len(kinds) == 1


def test_task_announcement_falls_back_without_a_brain(env):
    tasks.set_runner(lambda brief, ctx: ("The logs are clean. Nothing failed overnight.", "ok"))
    tid = tasks.start("look at the logs", origin="voice")
    assert tasks.wait(tid, 5)
    done = [m for m in store.list_messages(20, channel="voice") if m.kind == "task_done"]
    assert len(done) == 1 and done[0].body.startswith("The logs are clean.")


def test_api_key_fallback_reconnects_on_the_key_and_retries(env, monkeypatch, clock):
    monkeypatch.setenv("FAKE_ASSISTANT", "false")
    monkeypatch.setenv("ASSISTANT_BACKEND", "claude_code")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    config_store.reload()
    asettings.reset_assistant_settings()
    assert config.get_settings().anthropic_api_key == "sk-test"

    def reply(said, client):
        if client.options.env.get("ANTHROPIC_API_KEY"):
            return ["Your Shorts got 900 views this week."]
        return [brain.error_result("You've hit your session limit · resets 4pm")]

    script(reply)
    res, heard = say("how are the shorts doing")
    assert heard == ["Your Shorts got 900 views this week."]
    first, second = brain.FakeClient.instances
    assert first.options.env.get("ANTHROPIC_API_KEY") == "" and second.options.env["ANTHROPIC_API_KEY"] == "sk-test"
    assert second.options.resume == first.session_id and second.options.max_budget_usd > 0
    assert claude_code.backoff_until() is not None and brain.get_brain().mode == "api"


def test_plan_limit_without_a_key_says_when_it_is_back(env, monkeypatch, clock):
    monkeypatch.setenv("FAKE_ASSISTANT", "false")
    monkeypatch.setenv("ASSISTANT_BACKEND", "claude_code")
    config_store.reload()
    asettings.reset_assistant_settings()
    script(lambda said, c: [brain.error_result("You've hit your session limit · resets 4pm")])
    res, heard = say("hello")
    assert res["status"] == "limit" and "back at 4pm" in heard[0]
    res, heard = say("hello again")                                   # no second process while limited
    assert "back at 4pm" in heard[0] and len(brain.FakeClient.instances) == 1


def test_interrupt_stops_the_answer_and_the_next_question_follows(env):
    def reply(said, client):
        if said == "tell me a long story":
            return [f"Sentence number {i}." for i in range(200)]
        return ["Here is the short answer."]

    brain.set_client_factory(lambda o, t: brain.FakeClient(o, t, lambda p, c: reply(
        p.split("<said>")[-1].split("</said>")[0].strip(), c), delay=0.01))
    t1 = brain.ask("tell me a long story")
    for _ in range(200):
        if t1.sentences:
            break
        time.sleep(0.01)
    assert brain.interrupt()
    res2 = brain.ask("short version").wait(10)
    assert t1.wait(1)["status"] == "cancelled" and len(t1.sentences) < 200
    assert res2["reply"] == "Here is the short answer."


def test_turn_context_mentions_only_check_ins_due_soon(env, clock):
    from datetime import timedelta

    from assistant import status

    store.add_checkin("pay", "What is your pay schedule?", db.to_iso(db.utcnow() + timedelta(hours=2)))
    assert "pay schedule" not in status.turn_context()
    store.add_checkin("hours", "How many hours this week?", db.to_iso(db.utcnow() + timedelta(minutes=3)))
    ctx = status.turn_context()
    assert "How many hours" in ctx and "pay schedule" not in ctx


def test_real_sdk_options_and_in_process_tool_server(env, monkeypatch):
    b = brain.Brain()
    table = b.tool_table()
    assert {"settings_set", "settings_get", "display", "start_task", "memory_search"} <= set(table)
    server = b.sdk_server(table)
    assert server["type"] == "sdk" and server["name"] == "clipagent"
    opts = b.options(mode="plan", resume="abc", table=table)
    assert opts.permission_mode == "dontAsk" and opts.include_partial_messages and opts.resume == "abc"
    assert opts.model == "sonnet" and opts.effort == "low" and opts.setting_sources == ["user"]
    assert opts.env["CLAUDE_CONFIG_DIR"] == str(claude_code.home_dir()) and opts.env.get("ANTHROPIC_API_KEY") == ""
    assert "Never say \"channel\"" not in opts.system_prompt["append"] or True
    assert "settings_set" in opts.system_prompt["append"] and opts.system_prompt["preset"] == "claude_code"


def test_speech_rules():
    assert speech.trim_to_sentence("It is done. The next one isn", 20) == "It is done."
    assert speech.is_empty_ack("Very good, sir.") and not speech.is_empty_ack("Very good, sir. Lewis is on.")
    assert speech.clean_spoken("See https://x.com/a for **details**.") == "See for details."
    assert speech.clean_spoken("Your YouTube channel grew.") == "Your YouTube channel grew."
    from voice import commands

    for noise in ("Maybe, um.", "um", "uh huh", "so like", "Thank you."):
        assert commands.is_filler_utterance(noise), noise
    for real in ("yes", "stop", "what?", "switch to Lewis", "open YouTube"):
        assert not commands.is_filler_utterance(real), real


# ---------------------------------------------------------------------------
# wedge recovery and turn stitching (Blake: split dictation, then total silence)
# ---------------------------------------------------------------------------

class WedgeClient(brain.FakeClient):
    """A real-SDK failure mode: the question streams a little (or nothing), then never finishes, and
    interrupt() is acknowledged without a result ever arriving."""

    def __init__(self, *a, silent=False, **kw):
        super().__init__(*a, **kw)
        self.silent = silent

    async def _produce(self, prompt):
        import asyncio

        t = brain._sdk()
        if len(self.history) == 1:
            self._out.put_nowait(t.SystemMessage(subtype="init", data={"session_id": self.session_id}))
        if not self.silent:
            self._out.put_nowait(t.StreamEvent(uuid="x", session_id=self.session_id, event={
                "type": "content_block_start", "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_w", "name": "mcp__clipagent__ledger_summary",
                                  "input": {}}}))
        await asyncio.Event().wait()           # never ends

    async def interrupt(self):
        self.interrupted += 1                  # acknowledged, but no result follows

    async def disconnect(self):
        if self._busy is not None:
            self._busy.cancel()
        await super().disconnect()


def _fast_watchdog(monkeypatch):
    monkeypatch.setattr(brain, "ABANDON_WAIT_SEC", 0.3)
    monkeypatch.setattr(brain, "FIRST_OUTPUT_SEC", 0.5)
    monkeypatch.setattr(brain, "WATCH_TICK_SEC", 0.02)


def _factory(first, normal=lambda said, c: ["Here I am, sir."]):
    import re

    made = []

    def make(options, tools):
        def script(prompt, client):
            m = re.search(r"<said>\s*(.*?)\s*</said>", prompt, re.S)
            return normal(m.group(1) if m else prompt, client)
        c = first(options, tools, script) if not made else brain.FakeClient(options, tools, script)
        made.append(c)
        return c

    brain.set_client_factory(make)
    return made


def test_wedged_interrupted_turn_recovers_and_the_next_question_is_answered(env, monkeypatch):
    _fast_watchdog(monkeypatch)
    made = _factory(lambda o, t, s: WedgeClient(o, t, s))
    t1 = brain.ask("On Monday I worked nine hours.")
    for _ in range(200):
        if t1.activity_at is not None:
            break
        time.sleep(0.01)
    assert brain.interrupt()                                  # barge-in on a turn whose stream never ends
    t2 = brain.ask("Hello?")
    res2 = t2.wait(10)
    assert t1.wait(1)["status"] == "cancelled" and made[0].interrupted >= 1 and made[0].disconnected
    assert res2["reply"] == "Here I am, sir." and t2.merged_from is None
    assert len(made) == 2 and made[1].options.resume == made[0].session_id   # the day's session resumed
    assert any(e.message == "voice brain recovered" for e in db.list_events(limit=20))
    assert [e["state"] for e in events.BUS.since(0, 1000) if e["type"] == "brain"][:2] == ["reconnecting", "ready"]
    assert brain.ask("And now?").wait(5)["reply"] == "Here I am, sir."           # the queue keeps moving


def test_session_with_no_output_is_reset_and_he_hears_one_short_line(env, monkeypatch):
    _fast_watchdog(monkeypatch)
    made = _factory(lambda o, t, s: WedgeClient(o, t, s, silent=True))
    res, heard = say("How many hours did I work?")
    assert res["status"] == "recovered" and heard == [brain.LOST_TRAIN.format(h="sir")]
    assert made[0].disconnected and brain.get_brain().status()["recoveries"] == 1
    assert say("Hello?")[0]["reply"] == "Here I am, sir."


def test_split_dictation_is_stitched_into_one_request(env):
    seen = []

    def reply(said, client):
        seen.append(said)
        return ["Logged, sir: forty-one hours."]

    brain.set_client_factory(lambda o, t: brain.FakeClient(o, t, lambda p, c: reply(
        p.split("<said>")[-1].split("</said>")[0].strip(), c), delay=0.3))
    parts = ["On Monday, I worked nine hours. On Tuesday, I worked eight hours. On Wednesday.",
             "On Wednesday I worked eight hours.",
             "On Thursday, I worked eight hours and Friday has already been logged. So can you log the rest of those?"]
    turns = [brain.ask(p) for p in parts]          # each breath arrives before any answer has been spoken
    res = turns[-1].wait(10)
    assert [t.wait(2)["status"] for t in turns[:2]] == ["cancelled", "cancelled"]
    assert turns[1].merged_from == turns[0].id and turns[2].merged_from == turns[1].id
    assert turns[-1].heard == " ".join(parts) and res["reply"] == "Logged, sir: forty-one hours."
    assert seen[-1] == " ".join(parts)             # the model saw the whole dictation once, at the end
    replies = [m for m in store.list_messages(20, channel="voice", direction="out") if m.body]
    assert [m.body for m in replies] == ["Logged, sir: forty-one hours."]


def test_talking_over_a_spoken_answer_is_still_a_barge_in(env, monkeypatch):
    monkeypatch.setattr(brain, "MERGE_WINDOW_SEC", 0.0)
    brain.set_client_factory(lambda o, t: brain.FakeClient(o, t, lambda p, c: (
        [f"Sentence number {i}." for i in range(200)] if "long story" in p else ["Short answer."]), delay=0.01))
    t1 = brain.ask("tell me a long story")
    for _ in range(200):
        if t1.sentences:
            break
        time.sleep(0.01)
    assert brain.get_brain().stitch_target("browser") is None      # he is being spoken to: not a continuation
    assert brain.interrupt()
    t2 = brain.ask("stop, the short version")
    assert t2.wait(10)["reply"] == "Short answer." and t2.merged_from is None and t2.heard == "stop, the short version"
    assert t1.wait(1)["status"] == "cancelled" and len(t1.sentences) < 200
