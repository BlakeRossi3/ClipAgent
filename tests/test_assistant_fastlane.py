"""assistant/fastlane.py: routing, context parity with the brain, hand-off, and one memory (the brain is told)."""

from __future__ import annotations

import re

import pytest

import config_store
from assistant import brain, events, fastlane, quick, runtime, store, vault
from assistant import settings as asettings
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPAGENT_CONFIG_FILE", str(tmp_path / "cfg" / "config.env"))
    monkeypatch.setenv("ASSISTANT_ENABLED", "true")
    monkeypatch.setenv("FAKE_ASSISTANT", "true")
    config_store.reload()
    asettings.reset_assistant_settings()
    prompts: list[str] = []

    def reply(prompt, client):
        prompts.append(prompt)
        said = re.search(r"<said>\s*(.*?)\s*</said>", prompt, re.S)
        if brain.ESCALATED[:20] in prompt and not said:
            return ["Carrying on."]
        return [f"Brain here, sir: {(said.group(1) if said else '')[:40]}."]

    brain.set_client_factory(lambda options, tools: brain.FakeClient(options, tools, reply))
    yield prompts


def quick_model(*steps):
    fb = runtime.FakeBackend(*steps)
    quick.set_backend(fb)
    return fb


def say(text, origin="browser"):
    heard = []
    res = brain.ask(text, origin=origin, on_sentence=heard.append).wait(10)
    return res, heard


def test_routing_rules():
    for t in ("how are you", "what time is it", "thanks very much", "what did I just say", "good morning, sir"):
        assert fastlane.route(t) == "fast", t
    for t in ("search for the Ohio State score", "open YouTube", "what's the weather tomorrow", "remember I like tea",
              "switch to Lewis", "read my latest email", "check https://example.com", "show me a map of Paris",
              "what is the news"):
        assert fastlane.route(t) == "brain", t
    assert fastlane.route(" ".join(["word"] * 30)) == "brain"                       # long requests
    assert fastlane.route("yes please", last_reply="Shall I send it, sir?") == "brain"   # answering its question
    assert fastlane.route("how are you", merged=True) == "brain"
    assert fastlane.route("") == "brain"


def test_off_without_an_api_key_or_when_switched_off(env, monkeypatch):
    assert not fastlane.enabled()                         # practice mode, no key: the brain answers everything
    quick_model("Hello.")
    assert fastlane.enabled()
    monkeypatch.setenv("VOICE_FAST_LANE", "off")
    import config

    config.reset_settings()
    assert not fastlane.enabled()


def test_fast_answer_skips_the_brain_and_the_brain_hears_it_next_turn(env):
    fb = quick_model("It is ten in the morning, sir.")
    res, heard = say("what time is it")
    assert heard == ["It is ten in the morning, sir."] and res["route"] == "fast"
    assert not brain.FakeClient.instances or not brain.FakeClient.instances[0].history   # the brain was not asked
    row = brain.get_brain().timing_for(res["turn"])
    assert row["route"] == "fast" and row["fast_ms"] is not None and row["fast_first_token_ms"] is not None
    out = store.list_messages(5, channel="voice", direction="out")[0]
    assert out.body == "It is ten in the morning, sir." and out.meta["route"] == "fast"
    assert fb.requests[0].model == asettings.get_assistant_settings().quick_model
    # one memory: the brain's next prompt carries the fast exchange
    res2, heard2 = say("open YouTube")
    assert res2["route"] == "brain" and heard2 and heard2[0].startswith("Brain here")
    prompt = env[-1]
    assert "<fast_replies>" in prompt and "what time is it" in prompt and "ten in the morning" in prompt
    say("open the dashboard")
    assert "<fast_replies>" not in env[-1]                # told once


def test_context_parity_with_the_brain(env):
    vault.ensure_vault()
    vault.remember("His favourite colour is green", area="preferences", source="test")
    fb = quick_model("Green, sir.")
    turn = brain.ask("what is my favourite colour", origin="browser")
    turn.wait(10)
    sent = fb.requests[0].messages[0]["content"]
    assert turn.context and all(block in sent for block in turn.context)         # the very same blocks...
    assert all(block in turn.prompt for block in turn.context)                   # ...the brain's prompt is built from
    assert "<context>" in sent and "Local time" in sent and "green" in sent.lower()
    assert "<said>" in sent and "favourite colour" in sent
    # today's transcript tail goes too (the brain holds it in its session)
    quick_model("You asked about your favourite colour, sir.")
    say("what did I just ask")
    sent2 = quick._backend.requests[0].messages[0]["content"]
    assert "<conversation_today>" in sent2 and "He: what is my favourite colour" in sent2 and "You: Green, sir." in sent2
    assert "He: what did I just ask" not in sent2.split("<said>")[0]               # not twice


def test_hand_off_with_an_ack_goes_to_the_brain_and_the_marker_is_never_spoken(env):
    quick_model("Let me think about that, sir. " + fastlane.HANDOFF)
    res, heard = say("what do you reckon about it")
    assert res["route"] == "fast>brain"
    assert heard[0] == "Let me think about that, sir." and heard[-1].startswith("Brain here")
    assert all("handoff" not in h for h in heard)
    assert "already told him" in env[-1] and "Let me think about that" in env[-1]
    row = brain.get_brain().timing_for(res["turn"])
    assert row["route"] == "fast>brain" and row["first_token_ms"] is not None


def test_bare_hand_off_and_failures_fall_through_to_the_brain(env):
    quick_model(fastlane.HANDOFF)
    res, heard = say("tell me something")
    assert res["route"] == "fast>brain" and heard[0].startswith("Brain here")

    def boom(req):
        raise RuntimeError("api down")

    quick_model(boom)
    res, heard = say("how are you")
    assert res["status"] == "ok" and heard[0].startswith("Brain here")
    assert any(e.message.startswith("voice fast lane failed") for e in __import__("db").list_events(limit=20))


def test_tool_requests_never_touch_the_quick_model(env):
    fb = quick_model("should not be used")
    res, heard = say("search for the Ohio State score")
    assert res["route"] == "brain" and not fb.requests and heard[0].startswith("Brain here")


def test_events_carry_the_fast_sentences(env):
    quick_model("Very well, sir.")
    after = events.BUS.seq
    res, _ = say("good evening")
    sents = [e for e in events.BUS.since(after, 100) if e["type"] == "sentence"]
    assert [e["text"] for e in sents] == ["Very well, sir."] and sents[0]["turn"] == res["turn"]
