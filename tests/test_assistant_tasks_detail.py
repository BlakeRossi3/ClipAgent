"""assistant/tasks.detail + GET /voice/api/task/<id>: the expanded task row shows the full context -
the request, the conversation that led to it, what was said while it ran, the full result."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
from assistant import store, tasks
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")


@pytest.fixture(autouse=True)
def _runner():
    yield
    tasks.set_runner(None)


def _run_task(clock, runner, text="check the upload logs", heard="can you check the upload logs"):
    tasks.set_runner(runner)
    tid = tasks.start(text, heard=heard, origin="any")
    assert tasks.wait(tid, 5)
    return tid


def test_detail_has_the_conversation_the_steps_and_the_full_result(clock):
    clock.advance(minutes=-45)
    store.add_message("in", "voice", "an old remark from long before", kind="chat")
    clock.advance(minutes=40)
    store.add_message("in", "voice", "the uploads look stuck", kind="chat")
    store.add_message("out", "voice", "I'll look into it, sir.", kind="chat", agent="main")
    clock.advance(minutes=5)

    def runner(brief, ctx, on_step):
        on_step("Reading the logs")
        clock.advance(minutes=1)
        store.add_message("in", "voice", "also what's for dinner", kind="chat")   # said while it ran
        clock.advance(minutes=1)
        return "Two uploads failed on quota. Both are queued for tomorrow.", "ok"

    tid = _run_task(clock, runner)
    clock.advance(minutes=1)
    store.add_message("in", "voice", "a later unrelated thing", kind="chat")

    d = tasks.detail(tid)
    assert d["task"] == "check the upload logs" and d["heard"] == "can you check the upload logs"
    assert d["status"] == "done" and d["steps"][0]["label"] == "Reading the logs"
    assert "<background_task>" in d["brief"]
    assert d["result"].startswith("Two uploads failed on quota.") and "queued for tomorrow" in d["result"]
    assert d["announced"].startswith("Two uploads failed")
    convo = [(m["who"], m["text"]) for m in d["conversation"]]
    assert convo == [("you", "the uploads look stuck"), ("cecil", "I'll look into it, sir.")]
    assert [m["text"] for m in d["followups"]] == ["also what's for dinner"]
    texts = str(d)
    assert "an old remark" not in texts and "a later unrelated thing" not in texts


def test_detail_unknown_task_is_none_and_conversation_is_capped(clock):
    assert tasks.detail("nope") is None
    for i in range(tasks.CONVO_KEEP + 5):
        store.add_message("in", "voice", f"line {i}", kind="chat")
    clock.advance(seconds=5)
    tid = _run_task(clock, lambda brief, ctx: ("Done.", "ok"))
    d = tasks.detail(tid)
    assert len(d["conversation"]) == tasks.CONVO_KEEP
    assert d["conversation"][-1]["text"] == f"line {tasks.CONVO_KEEP + 4}"


def test_task_endpoint_returns_detail_404_and_needs_auth(clock, monkeypatch):
    import server

    store.add_message("in", "voice", "look at the logs please", kind="chat")
    clock.advance(seconds=5)
    tid = _run_task(clock, lambda brief, ctx: ("The logs are clean.", "ok"), text="look at the logs", heard="")
    c = TestClient(server.create_app(), base_url="http://127.0.0.1:8000")
    r = c.get(f"/voice/api/task/{tid}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["task"]["id"] == tid and body["task"]["result"] == "The logs are clean."
    assert body["task"]["conversation"][-1]["text"] == "look at the logs please"
    assert c.get("/voice/api/task/unknown").status_code == 404
    monkeypatch.setenv("DASHBOARD_TOKEN", "tok")
    config.reset_settings()
    assert c.get(f"/voice/api/task/{tid}").status_code == 401


def test_page_fetches_the_detail_when_a_row_is_opened():
    js = (Path(__file__).resolve().parents[1] / "static" / "jarvis.js").read_text(encoding="utf-8")
    assert "/voice/api/task/" in js and "loadTaskDetail(t.id" in js and "appendConvo(box" in js
