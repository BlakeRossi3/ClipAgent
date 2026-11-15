"""assistant/speaking_style.py: the owner-trained Speaking Style note, the speaking_style_note tool, live
feedback, the per-request evidence, the Settings "How I speak" card and the weekly tidy."""

from __future__ import annotations

import re

import pytest

import config_store
import db
from assistant import brain, persona, speaking_style, store, tools, vault
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
    yield tmp_path


def test_note_comes_from_the_template_and_goes_into_every_voice_prompt(env):
    vault.ensure_vault()
    body = vault.read_note(speaking_style.NOTE_PATH).body
    assert "## Principles" in body and f"## {speaking_style.LESSONS_HEADING}" in body
    speaking_style.record("lookup", "likes a running commentary", prompted_by="keep me posted on these")
    assert "Live lookups: likes a running commentary." in brain.system_prompt()
    assert "likes a running commentary" in "\n".join(persona.system_blocks("voice"))
    assert "likes a running commentary" not in "\n".join(persona.system_blocks("sms"))   # spoken channels only
    assert "Speaking Style" not in brain.system_prompt(worker=True)
    assert len(speaking_style.prompt_block()) <= speaking_style.PROMPT_MAX_CHARS + 400   # small: it is in every session


def test_voice_prompt_is_judgement_with_examples_not_a_timer():
    p = brain.VOICE_PROMPT
    assert "never open with a filler" not in p.lower()
    for needle in ("Opening YouTube", "Three stories so far", "a line every few seconds", "further apart",
                   "needs no acknowledgement", "speaking_style_note", "Speaking Style note"):
        assert needle in p, needle


def test_record_dedups_and_the_newest_lesson_wins(env):
    speaking_style.record("lookup", "likes a running commentary", prompted_by="keep me posted")
    speaking_style.record("lookup", "likes a running commentary")                     # the same again
    speaking_style.record("code", "only the start and the finish")
    assert [(x.kind, x.text) for x in speaking_style.lessons()] == [
        ("lookup", "likes a running commentary."), ("code", "only the start and the finish.")]
    res = speaking_style.record("lookup", "too much talking; just the headline", prompted_by="too much")
    assert res["replaced"] == ["likes a running commentary."]                         # contradiction: newest wins
    ls = speaking_style.lessons()
    assert [(x.kind, x.text) for x in ls] == [("code", "only the start and the finish."),
                                             ("lookup", "too much talking; just the headline.")]
    assert ls[1].after == "too much" and re.fullmatch(r"\d{4}-\d\d-\d\d", ls[1].day)
    for w in ("say nothing after opening", "name the app once", "skip confirmations", "whisper quietly",
              "mention failures plainly", "never repeat titles", "brief tone please", "skip pleasantries entirely"):
        speaking_style.record("action", w)
    assert sum(1 for x in speaking_style.lessons() if x.kind == "action") == speaking_style.MAX_PER_KIND


def test_merge_and_tidy_keep_the_note_short(env):
    body = vault.read_note(speaking_style.NOTE_PATH).body if vault.note_exists(speaking_style.NOTE_PATH) else ""
    speaking_style.read_note()
    n = vault.read_note(speaking_style.NOTE_PATH)
    lines = "\n".join(["- Live lookups: likes a running commentary.", "- Live lookups: only the answer, nothing else.",
                       "- Long jobs: start and finish only.", "- Long jobs: start and finish only please."])
    vault.write_note(speaking_style.NOTE_PATH, n.body.rstrip() + "\n" + lines + "\n", base_sha=n.sha)
    assert speaking_style.tidy() == {"before": 4, "after": 2}
    assert [x.text for x in speaking_style.lessons()] == ["only the answer, nothing else.",
                                                          "start and finish only please."]
    assert body is not None


def test_tool_is_registered_validates_and_marks_the_feedback(env):
    speaking_style.log_turn("t1", "what's in the news", kind="lookup", duration_ms=9000, updates=3, sentences=4)
    tools.available(["speaking_style_note"])
    ctx = tools.ToolContext(channel="voice", agent="main")
    bad = tools.dispatch("speaking_style_note", {"task_kind": "everything", "lesson": "x"}, ctx,
                         allowed={"speaking_style_note"})
    assert not bad.ok
    ok = tools.dispatch("speaking_style_note", {"task_kind": "lookup", "lesson": "fewer updates",
                                                "prompted_by": "too many updates"}, ctx,
                        allowed={"speaking_style_note"})
    assert ok.ok and speaking_style.get_turn("t1")["feedback"] == "too many updates"
    assert "speaking_style_note" in brain.get_brain().tool_table()


@pytest.mark.parametrize("text", ["that was too much", "stop narrating everything", "keep me posted on these",
                                  "you didn't tell me you'd started", "that was perfect", "talk less while you work",
                                  "give me more updates on news checks"])
def test_feedback_phrases_are_spotted(text):
    assert speaking_style.detect_feedback(text)


@pytest.mark.parametrize("text", ["what's the weather", "open youtube", "log eight hours", "who won the game"])
def test_ordinary_requests_are_not_feedback(text):
    assert not speaking_style.detect_feedback(text)


def test_kinds_are_inferred_from_the_tools_used():
    assert speaking_style.infer_kind("hi", []) == "chat"
    assert speaking_style.infer_kind("news", ["WebSearch", "show_data"]) == "lookup"
    assert speaking_style.infer_kind("open yt", ["mcp__clipagent__open_url"]) == "action"
    assert speaking_style.infer_kind("fix it", ["Read", "Edit"]) == "code"
    assert speaking_style.infer_kind("research", ["start_task"]) == "long"
    assert speaking_style.infer_kind("books", ["ledger_summary"]) == "task"


def test_feedback_is_recognised_live_and_recorded_scoped_to_the_kind_of_task(env):
    """A news lookup with commentary, then "too much": the brain says which request it was about, the
    (fake) model records the lesson for lookups, and the next session's prompt carries it."""
    def reply(prompt, client):
        said = re.search(r"<said>\s*(.*?)\s*</said>", prompt, re.S)
        said = said.group(1) if said else prompt
        if "news" in said and "too much" not in said:
            return ["Checking the headlines, sir.", brain.tool_step("WebSearch", query="news"),
                    "Three stories so far.", brain.tool_step("WebSearch", query="more news"), "That's the lot, sir."]
        if "System note" in prompt and "kind of task: lookup" in prompt:
            return [brain.tool_step("speaking_style_note", task_kind="lookup", lesson="just the headline",
                                    prompted_by=said), "Very good, sir."]
        return ["Hm."]

    brain.set_client_factory(lambda options, tools_: brain.FakeClient(options, tools_, reply))
    brain.ask("what's in the news", origin="browser").wait(10)
    first = speaking_style.last_turn()
    assert first["kind"] == "lookup" and first["updates"] == 2 and first["sentences"] == 3
    res = brain.ask("that was too much for a news check", origin="browser").wait(10)
    assert res["reply"] == "Very good, sir."
    assert [(x.kind, x.text) for x in speaking_style.lessons()] == [("lookup", "just the headline.")]
    assert speaking_style.get_turn(first["turn"])["feedback"].startswith("that was too much")
    assert "Live lookups: just the headline." in brain.system_prompt()


def test_thumbs_write_a_lesson_candidate(env):
    speaking_style.log_turn("t9", "open youtube", kind="action", duration_ms=1200, updates=1, sentences=1)
    lid = speaking_style.rate("t9", True)
    row = next(x for x in store.list_lessons("proposed") if x.id == lid)
    assert row.area == "voice" and "Quick actions" in row.lesson and "open youtube" in row.evidence
    assert speaking_style.get_turn("t9")["rating"] == 1
    assert speaking_style.rate("nope", False) is None
    d = speaking_style.digest()
    assert d["kinds"]["action"]["up"] == 1


def test_how_i_speak_card_and_thumbs_endpoint_need_auth_and_csrf(env, monkeypatch):
    from fastapi.testclient import TestClient

    import server

    monkeypatch.setenv("SETUP_COMPLETE", "true")
    config_store.reload()
    speaking_style.record("lookup", "likes a running commentary")
    speaking_style.log_turn("tA", "what is the score", kind="lookup", duration_ms=4000, updates=2, sentences=3)
    c = TestClient(server.create_app(), base_url="http://127.0.0.1:8000")
    page = c.get("/setup/")
    assert page.status_code == 200 and "How I speak" in page.text and "likes a running commentary" in page.text
    assert "what is the score" in page.text and "2 spoken updates" in page.text
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text).group(1)
    assert c.post("/setup/speaking/rate", data={"turn": "tA", "rating": "up"}).status_code == 403   # no CSRF
    remote = TestClient(server.create_app(), base_url="https://agent.example.com")
    assert remote.post("/setup/speaking/rate", data={"turn": "tA", "rating": "up", "csrf": csrf},
                       headers={"X-Forwarded-For": "203.0.113.7"}).status_code == 403            # not his machine
    assert speaking_style.get_turn("tA")["rating"] is None
    assert c.post("/setup/speaking/rate", data={"turn": "tA", "rating": "sideways", "csrf": csrf}).status_code == 400
    r = c.post("/setup/speaking/rate", data={"turn": "tA", "rating": "down", "csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and speaking_style.get_turn("tA")["rating"] == -1
    assert any(x.area == "voice" and "say less" in x.lesson for x in store.list_lessons("proposed"))
    assert c.post("/setup/speaking/rate", data={"turn": "gone", "rating": "up", "csrf": csrf}).status_code == 404
    assert db.kv_get(speaking_style.TURNS_KEY)
