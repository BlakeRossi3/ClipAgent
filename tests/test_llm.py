from types import SimpleNamespace

import anthropic
import httpx2
import pytest

import budget
import config
import db
import llm

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 1, "maximum": 10},
        "reason": {"type": "string", "maxLength": 200},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "kind": {"type": "string", "enum": ["funny", "awe", "drama"]},
    },
    "required": ["score", "reason", "tags", "kind"],
}


def test_default_fake_returns_schema_sample(fake_llm):
    res = llm.complete("haiku", system="Score it.", user="window text", purpose="pick.score_window",
                       json_schema=SCORE_SCHEMA, max_tokens=256)
    assert res.data == {"score": 5, "reason": "fake", "tags": ["fake"], "kind": "funny"}
    assert res.fake and res.model == "claude-haiku-5-5" and res.tier == "haiku"
    assert fake_llm.purposes() == ["pick.score_window"]
    req = fake_llm.calls[0]
    assert req.system == ("Score it.",) and req.effort == "low" and req.cache_system


def test_fake_llm_env_without_handler():
    res = llm.complete("sonnet", system="s", user="hello there", purpose="x.text")
    assert res.text.startswith("[fake sonnet:x.text]") and res.data is None
    assert res.fake


def test_usage_recorded_with_refs(fake_llm, make_clip):
    cid = make_clip()
    fake_llm.queue(llm.FakeReply(text="ok", usage=llm.Usage(input_tokens=100_000, output_tokens=0)))
    res = llm.complete("haiku", system="s", user="u", purpose="pick.title", clip_id=cid)
    assert res.cost_usd == pytest.approx(0.01)
    row = db.list_llm_usage()[0]
    assert row.purpose == "pick.title" and row.clip_id == cid and row.fake and row.tier == "haiku"
    assert budget.spent_today() == pytest.approx(0.01)


def test_json_retry_then_success(fake_llm):
    fake_llm.queue("not json", {"score": 9, "reason": "r", "tags": [], "kind": "awe"})
    res = llm.complete("haiku", system="s", user="u", purpose="p", json_schema=SCORE_SCHEMA)
    assert res.data["score"] == 9 and res.attempts == 2
    assert len(db.list_llm_usage()) == 2  # both attempts billed
    assert db.list_events(module="llm", level="warning")


def test_json_validation_failure_raises(fake_llm):
    bad = {"score": 11, "reason": "r", "tags": [], "kind": "awe"}  # maximum enforced client-side
    fake_llm.queue(bad, bad)
    with pytest.raises(llm.LLMOutputError, match="maximum"):
        llm.complete("haiku", system="s", user="u", purpose="p", json_schema=SCORE_SCHEMA)


def test_fenced_json_is_accepted(fake_llm):
    fake_llm.queue('```json\n{"score": 3, "reason": "r", "tags": [], "kind": "drama"}\n```')
    assert llm.complete("haiku", system="s", user="u", purpose="p", json_schema=SCORE_SCHEMA).data["score"] == 3


def test_refusal_raises(fake_llm):
    fake_llm.queue(llm.FakeReply(text="", stop_reason="refusal"))
    with pytest.raises(llm.LLMRefusal):
        llm.complete("sonnet", system="s", user="u", purpose="p")
    assert len(db.list_llm_usage()) == 1


def test_budget_blocks_before_call(fake_llm):
    budget.record_usage("claude-sonnet-5-5", "earlier", input_tokens=0, output_tokens=99_990)  # $0.9999
    with pytest.raises(budget.BudgetExceeded):
        llm.complete("sonnet", system="s", user="u", purpose="p", max_tokens=4096)
    assert fake_llm.calls == []


def test_bad_args():
    with pytest.raises(ValueError):
        llm.complete("opus", system="s", user="u", purpose="p")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        llm.complete("haiku", system=[], user="u", purpose="p")


def test_schema_helpers():
    api = llm.to_api_schema(SCORE_SCHEMA)
    assert api["additionalProperties"] is False
    assert "minimum" not in api["properties"]["score"] and "maxItems" not in api["properties"]["tags"]
    assert "minimum" in SCORE_SCHEMA["properties"]["score"]  # original untouched
    assert llm.validate_json({"score": 1, "reason": "", "tags": [], "kind": "awe", "x": 1}, SCORE_SCHEMA)
    assert llm.validate_json({"a": None}, {"type": "object", "properties": {"a": {"type": ["string", "null"]}}}) == []
    assert llm.validate_json(3, {"anyOf": [{"type": "string"}, {"type": "integer"}]}) == []


# ---------------------------------------------------------------------------
# real code path against an injected fake client (no network)
# ---------------------------------------------------------------------------

class RecordingClient:
    def __init__(self, text='{"score": 7, "reason": "r", "tags": [], "kind": "awe"}', stop="end_turn",
                 model="claude-haiku-5-5", exc=None):
        self.kwargs: list[dict] = []
        self._resp = SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=4000,
                                  cache_creation_input_tokens=None),
            model=model, stop_reason=stop, _request_id="req_123", id="msg_1")
        self._exc = exc
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.kwargs.append(kwargs)
        if self._exc:
            raise self._exc
        return self._resp


def test_api_request_shape_haiku(clock):
    client = RecordingClient()
    llm.set_client(client)
    res = llm.complete("haiku", system=["INSTRUCTIONS", "STRATEGY"], user="window", purpose="pick.score_window",
                       max_tokens=512, json_schema=SCORE_SCHEMA)
    kw = client.kwargs[0]
    assert kw["model"] == "claude-haiku-5-5" and kw["max_tokens"] == 512
    assert kw["system"] == [{"type": "text", "text": "INSTRUCTIONS"},
                            {"type": "text", "text": "STRATEGY", "cache_control": {"type": "ephemeral"}}]
    assert kw["messages"] == [{"role": "user", "content": "window"}]
    assert kw["output_config"]["effort"] == "low"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert "minimum" not in kw["output_config"]["format"]["schema"]["properties"]["score"]
    assert "betas" not in kw and "temperature" not in kw and "thinking" not in kw
    assert res.data["score"] == 7 and not res.fake and res.request_id == "req_123"
    assert res.usage == llm.Usage(1000, 200, 4000, 0)
    assert res.cost_usd == pytest.approx((1000 * 0.10 + 200 * 0.50 + 4000 * 0.01) / 1e6)


def test_api_request_shape_sonnet_uses_fallbacks():
    client = RecordingClient(text="Title", model="claude-sonnet-5-5")
    llm.set_client(client)
    res = llm.complete("sonnet", system="S", user="U", purpose="pick.finalize", cache_system=False)
    kw = client.kwargs[0]
    assert kw["betas"] == [llm.SONNET_FALLBACK_BETA] and kw["fallbacks"] == "default"
    assert kw["output_config"] == {"effort": "medium"}
    assert "cache_control" not in kw["system"][0]
    assert res.text == "Title"


def test_sonnet_fallbacks_can_be_disabled(monkeypatch):
    monkeypatch.setenv("LLM_SERVER_FALLBACKS", "false")
    config.reset_settings()
    client = RecordingClient(text="x")
    llm.set_client(client)
    llm.complete("sonnet", system="S", user="U", purpose="p")
    assert "betas" not in client.kwargs[0]


def test_api_error_wrapped_and_logged():
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.InternalServerError("boom", response=httpx2.Response(500, request=req), body=None)
    llm.set_client(RecordingClient(exc=err))
    with pytest.raises(llm.LLMError, match="500"):
        llm.complete("haiku", system="s", user="u", purpose="p")
    assert db.list_events(module="llm", level="error")


def test_missing_key_without_fake(monkeypatch):
    monkeypatch.setenv("FAKE_LLM", "false")
    config.reset_settings()
    with pytest.raises(llm.LLMError, match="ANTHROPIC_API_KEY"):
        llm.complete("haiku", system="s", user="u", purpose="p")


def test_real_client_built_from_settings_only(monkeypatch):
    monkeypatch.setenv("FAKE_LLM", "false")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    config.reset_settings()
    client = llm._get_client()
    assert isinstance(client, anthropic.Anthropic)
    assert client.api_key == "sk-test-not-real" and client.max_retries == 3
    assert str(client.base_url).startswith("https://api.anthropic.com")


def test_end_to_end_through_real_sdk_with_mock_transport():
    """Exercise the real Anthropic SDK serialisation without any network."""
    import json

    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((request.headers.get("anthropic-beta"), json.loads(request.content)))
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
            "content": [{"type": "text", "text": '{"score": 8, "reason": "r", "tags": ["a"], "kind": "awe"}'}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 50, "output_tokens": 20, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 600},
        })

    client = anthropic.Anthropic(api_key="sk-test", base_url="https://api.anthropic.com", max_retries=0,
                                 http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    llm.set_client(client)
    res = llm.complete("sonnet", system="stable", user="u", purpose="pick.finalize", json_schema=SCORE_SCHEMA)
    beta, body = seen[0]
    assert beta == llm.SONNET_FALLBACK_BETA and body["fallbacks"] == "default"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert res.data["score"] == 8 and res.usage.cache_write_tokens == 600
    assert res.cost_usd == pytest.approx((50 * 2.0 + 20 * 10.0 + 600 * 2.5) / 1e6)
