"""Back-to-back texts: joined into one request, one reply; a newer text replaces the answer in progress."""

from __future__ import annotations

import time

import pytest

from assistant import sms_inbox, store, tasks
from assistant import settings as asettings
from assistant_fixtures import assistant_env, fake_model  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")


@pytest.fixture
def on(monkeypatch, clock):
    monkeypatch.setenv("ASSISTANT_ENABLED", "true")
    asettings.reset_assistant_settings()


def _batch(sec: float) -> None:
    asettings.set_assistant_settings(asettings.get_assistant_settings().replace(sms_batch_sec=sec))


def _owner_text(req) -> str:
    content = req.messages[0]["content"]
    return content if isinstance(content, str) else str(content)


def _replies():
    return [m for m in store.list_messages(50, direction="out") if m.kind == "reply"]


def test_one_text_one_reply(on, fake_model):
    fake_model.queue("Two meetings today.")
    out = sms_inbox.receive("what's on today?")
    assert out["joined"] is False and out["rounds"] == 1
    assert [m.body for m in _replies()] == ["Two meetings today."]
    assert len(store.list_messages(50, direction="in")) == 1      # no duplicate inbound row


def test_texts_sent_in_a_row_become_one_request(on, fake_model):
    _batch(0.3)
    more = iter(["can you check the reel", "the one from this morning"])

    def sleeper(sec):
        nxt = next(more, None)
        if nxt is not None:
            assert sms_inbox.receive(nxt)["joined"] is True       # joins; never a second answer
        time.sleep(sec)

    sms_inbox.set_sleeper(sleeper)
    fake_model.queue("Watched it: strong hook, weak ending.")
    out = sms_inbox.receive("hey")
    assert out["rounds"] == 1 and len(fake_model.requests) == 1
    asked = _owner_text(fake_model.requests[0])
    assert "hey\ncan you check the reel\nthe one from this morning" in asked and "3 texts" in asked
    assert len(_replies()) == 1 and len(store.list_messages(50, direction="in")) == 3
    assert _replies()[0].reply_to == store.list_messages(1, direction="in")[0].id   # answers the last text


def test_a_text_during_the_answer_replaces_it(on, fake_model):
    calls = []

    def first(req):
        calls.append(_owner_text(req))
        if len(calls) == 1:   # he texts again while the first answer is being worked on
            assert sms_inbox.receive("actually make it tomorrow")["joined"] is True
            return "Booked for today."
        return "Booked for tomorrow."

    fake_model.queue(first, first)
    out = sms_inbox.receive("book the dentist")
    assert out["rounds"] == 2
    assert "book the dentist\nactually make it tomorrow" in calls[1]
    assert [m.body for m in _replies()] == ["Booked for tomorrow."]       # the stale answer never went out
    rows = sorted(tasks.list_tasks(), key=lambda t: t["started"])
    assert [r["status"] for r in rows] == ["cancelled", "done"]
    assert rows[0]["summary"] == sms_inbox.SUPERSEDED and rows[1]["meta"]["via"] == "sms"
    assert sms_inbox.pending() == []


def test_a_text_after_the_reply_is_a_new_request(on, fake_model):
    fake_model.queue("The first is done.", "The second is done.")
    sms_inbox.receive("first")
    sms_inbox.receive("second")
    assert [m.body for m in reversed(_replies())] == ["The first is done.", "The second is done."]


def test_cancelled_from_the_rail_is_not_rerun(on, fake_model):
    def cancel(req):
        tasks.cancel(tasks.running_tasks()[0]["id"], "removed from the list by him")
        return "Never sent."

    fake_model.queue(cancel)
    out = sms_inbox.receive("do the thing")
    assert out["rounds"] == 1 and _replies() == [] and sms_inbox.pending() == []


def test_webhook_text_goes_through_the_inbox(on, fake_model, monkeypatch):
    import notify

    seen = []
    monkeypatch.setattr(sms_inbox, "receive", lambda text, **kw: seen.append((text, kw)) or {})
    notify.assistant_inbound("what's the weather")
    assert seen == [("what's the weather", {})]
