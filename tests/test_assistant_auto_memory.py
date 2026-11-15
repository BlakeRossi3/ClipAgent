"""assistant.auto_memory: after-turn fact extraction - filing with provenance, de-dup, debounce, no secrets or
trivia, deferral, the setting, the turn/rollover hooks and the home feed. Fake model, offline."""

from __future__ import annotations

import json

import pytest

import db
from assistant import auto_memory, claude_code, runtime, store, usage, vault
from assistant import settings as asettings
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")


def reply(*facts: tuple[str, str, str]) -> str:
    return json.dumps({"facts": [{"fact": f, "area": a, "confidence": c} for f, a, c in facts]})


def chat(channel: str, said: str, answer: str = "Noted, sir.") -> None:
    mid = store.add_message("in", channel, said, kind="chat")
    store.add_message("out", channel, answer, kind="reply", agent="main", reply_to=mid)


@pytest.fixture
def model():
    fb = runtime.FakeBackend()
    runtime.set_backend(fb)
    return fb


def test_extracts_files_facts_with_provenance_and_feeds_home(model, clock):
    chat("voice", "I've gone vegetarian. My sister Maya is visiting on December 20th.")
    model.queue(reply(("Is vegetarian.", "preferences", "stated"),
                      ("His sister Maya visits on 2026-12-20.", "person:Maya", "stated"),
                      ("Probably likes cooking at home.", "meals", "inferred")))
    res = auto_memory.extract("voice")
    assert res["status"] == "ok" and [a["fact"] for a in res["added"]][:2] == [
        "Is vegetarian.", "His sister Maya visits on 2026-12-20."]
    req = model.requests[-1]
    assert req.agent == "auto_memory" and "vegetarian" in req.messages[0]["content"]
    assert "He: I've gone vegetarian" in req.messages[0]["content"]
    body = vault.read_note("Profile/Preferences.md").body
    assert "Is vegetarian." in body and "_stated, auto-memory voice #" in body
    assert "_inferred, auto-memory voice" in vault.read_note("Health/Meals.md").body
    assert vault.note_exists("People/Maya.md")
    ev = auto_memory.recent_remembered()
    assert {e.message for e in ev} >= {"Remembered: Is vegetarian."}
    import home_live

    work = home_live.recent_work()
    assert any(w["kind"] == "memory" and w["text"] == "Remembered: Is vegetarian." for w in work)
    # nothing new since the cursor -> no model call
    n = len(model.requests)
    assert auto_memory.extract("voice")["status"] == "nothing" and len(model.requests) == n


def test_duplicates_secrets_and_trivia_are_not_filed(model, clock):
    chat("sms", "I'm vegetarian.")
    model.queue(reply(("Is vegetarian.", "preferences", "stated")))
    auto_memory.extract("sms")
    chat("sms", "Still vegetarian btw. My bank password is hunter2, card 4111 1111 1111 1111. Weather?")
    model.queue(reply(("Is a vegetarian.", "preferences", "stated"),
                      ("His bank password is hunter2.", "finance", "stated"),
                      ("His card number is 4111 1111 1111 1111.", "finance", "stated"),
                      ("He asked about the weather today.", "inbox", "stated"),
                      ("Ok.", "inbox", "stated"),
                      ("Plays five-a-side football on Thursdays.", "nonsense-area", "stated")))
    res = auto_memory.extract("sms")
    assert res["duplicates"] == 1 and res["dropped"] == 4
    assert [a["fact"] for a in res["added"]] == ["Plays five-a-side football on Thursdays."]
    assert res["added"][0]["note"] == "Inbox/Inbox.md"          # unknown area -> inbox
    everything = "\n".join(vault.read_note(p).body for p in vault.list_notes() if p.endswith(".md"))
    assert "hunter2" not in everything and "4111" not in everything
    assert auto_memory.looks_secret("Door code is 4412, PIN 1234") and not auto_memory.looks_secret(
        "Dentist appointment on 2026-12-03 at 10:30.")


def test_debounce_one_pass_per_two_minutes_per_channel(model, clock):
    timers: list[tuple[float, object]] = []
    auto_memory.set_scheduler(lambda delay, fn: timers.append((delay, fn)))
    assert auto_memory.after_turn("voice") is True
    assert auto_memory.after_turn("voice") is False            # one already pending
    assert auto_memory.after_turn("dashboard") is True         # per channel
    assert auto_memory.after_turn("system") is False           # not a conversation channel
    assert [d for d, _ in timers] == [auto_memory.SETTLE_SEC, auto_memory.SETTLE_SEC]
    chat("voice", "I usually go to the gym at 6am on weekdays.")
    model.queue(reply(("Goes to the gym at 6am on weekdays.", "about", "stated")))
    timers[0][1]()                                             # the timer fires
    assert auto_memory.pending() == ["dashboard"]
    assert "gym" in vault.read_note("Profile/About.md").body
    assert auto_memory.after_turn("voice") is True
    assert timers[-1][0] == pytest.approx(auto_memory.DEBOUNCE_SEC)   # just ran: wait out the 2 minutes
    timers[-1][1]()
    clock.advance(minutes=5)
    assert auto_memory.after_turn("voice") is True and timers[-1][0] == auto_memory.SETTLE_SEC


def test_off_switch_and_deferral(model, clock, monkeypatch):
    chat("dashboard", "Remind me that I prefer window seats when flying.")
    monkeypatch.setenv("ASSISTANT_AUTO_MEMORY", "false")
    asettings.reset_assistant_settings()
    assert auto_memory.after_turn("dashboard") is False and auto_memory.extract("dashboard")["status"] == "off"
    monkeypatch.setenv("ASSISTANT_AUTO_MEMORY", "true")
    asettings.reset_assistant_settings()
    deferred: list[tuple[str, str]] = []
    monkeypatch.setattr(usage, "should_defer", lambda kind, prompt, **kw: deferred.append((kind, prompt)) or 7)
    n = len(model.requests)
    assert auto_memory.extract("dashboard")["status"] == "deferred" and len(model.requests) == n
    assert deferred[0][0] == "auto_memory" and "window seats" in deferred[0][1] and "memory_remember" in deferred[0][1]
    assert auto_memory.extract("dashboard")["status"] == "nothing"   # the deferred job owns those messages


def test_turn_and_rollover_hooks_queue_a_pass(model, clock):
    model.queue("Very good, sir.")
    res = runtime.run_turn("My partner's name is Sam.", channel="dashboard")
    assert res.status == "ok" and "dashboard" in auto_memory.pending()
    db.kv_set(claude_code.KV_SESSION.format(channel="sms"), {"id": "s1", "day": "2026-11-12",
                                                            "started": "2026-11-12T10:00:00Z", "turns": 3})
    assert claude_code.rollover("sms", "new day") is True
    assert "sms" in auto_memory.pending()
