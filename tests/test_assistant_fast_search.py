"""assistant/fast_search.py: Brave / Exa with a faked HTTP client, key redaction, the dated fallback."""

from __future__ import annotations

import pytest

import config
from assistant import admin, events, fast_search, tools
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")
FAKE_KEY = "test-" + "k" * 12          # built at runtime; not a realistic key


@pytest.fixture(autouse=True)
def _reset():
    yield
    fast_search.set_http(None)


def _keys(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    config.reset_settings()


def test_brave_returns_top_five_and_shows_a_panel(monkeypatch, clock):
    _keys(monkeypatch, BRAVE_SEARCH_API_KEY=FAKE_KEY, EXA_API_KEY="other-" + "x" * 8)
    calls = []

    def http(method, url, headers, params, body, timeout):
        calls.append((method, url, headers, params))
        return 200, {"web": {"results": [{"title": f"<b>Ohio State</b> {i}", "url": f"https://e.example/{i}",
                                          "description": f"Won 31-10 {i}", "age": "2 hours ago"} for i in range(8)]}}

    fast_search.set_http(http)
    tools.load_extensions()
    res = tools.dispatch("fast_search", {"query": "Ohio State score", "freshness": "day"}, tools.ToolContext(),
                         allowed={"fast_search"})
    assert res.ok and "Ohio State 0" in res.content and "2 hours ago" in res.content and FAKE_KEY not in res.content
    method, url, headers, params = calls[0]
    assert url == fast_search.BRAVE_URL and params["freshness"] == "pd" and params["count"] == 5
    assert headers["X-Subscription-Token"] == FAKE_KEY
    out = fast_search.search("Ohio State score")
    assert out["provider"] == "brave" and len(out["results"]) == 5
    assert set(out["results"][0]) == {"title", "url", "snippet", "age"} and out["results"][0]["title"] == "Ohio State 0"
    panel = [e for e in events.BUS.since(0, 1000) if e["type"] == "panel"][-1]["panel"]
    assert panel["kind"] == "list" or panel.get("items") or panel.get("list")


def test_exa_when_only_exa_is_set(monkeypatch, clock):
    _keys(monkeypatch, EXA_API_KEY=FAKE_KEY)
    seen = {}

    def http(method, url, headers, params, body, timeout):
        seen.update(method=method, url=url, headers=headers, body=body)
        return 200, {"results": [{"title": "Rates", "url": "https://r.example", "publishedDate": "2026-11-13T12:00:00Z",
                                  "highlights": ["The Fed held rates."]}]}

    fast_search.set_http(http)
    out = fast_search.search("fed interest rate news", "week")
    assert seen["method"] == "POST" and seen["url"] == fast_search.EXA_URL and seen["headers"]["x-api-key"] == FAKE_KEY
    assert seen["body"]["startPublishedDate"].startswith("2026-11-06") and seen["body"]["numResults"] == 5
    assert out["provider"] == "exa" and out["results"][0] == {"title": "Rates", "url": "https://r.example",
                                                             "snippet": "The Fed held rates.", "age": "3 hours ago"}


def test_errors_never_show_the_key(monkeypatch, clock):
    _keys(monkeypatch, BRAVE_SEARCH_API_KEY=FAKE_KEY)

    def boom(*a):
        raise ConnectionError(f"failed for token {FAKE_KEY}")

    fast_search.set_http(boom)
    tools.load_extensions()
    res = tools.dispatch("fast_search", {"query": "news"}, tools.ToolContext(), allowed={"fast_search"})
    assert not res.ok and FAKE_KEY not in res.content and "brave search failed" in res.content
    fast_search.set_http(lambda *a: (401, {"error": FAKE_KEY}))
    with pytest.raises(fast_search.SearchError) as e:
        fast_search.search("news")
    assert FAKE_KEY not in str(e.value)
    assert {"BRAVE_SEARCH_API_KEY", "EXA_API_KEY"} <= admin.SECRET_KEYS


def test_without_keys_sports_and_news_get_today_and_latest(monkeypatch, clock):
    fast_search.set_http(lambda *a: pytest.fail("no request without a key"))
    out = fast_search.search("Ohio State score")
    assert out["provider"] == "none" and out["results"] == []
    assert out["use_web_search"] == "Ohio State score latest November 13, 2026" and "WebSearch" in out["note"]
    assert fast_search.dated_query("capital of France") == "capital of France"        # not current: unchanged
    assert fast_search.dated_query("latest news 2026") == "latest news 2026"


def test_the_voice_brain_offers_it_with_a_key_and_goes_straight_to_websearch_without(monkeypatch):
    from assistant import brain

    # no key: fast_search would only hand back a query (a whole wasted model round), so it is not offered
    assert "fast_search" not in brain.Brain().tool_table()
    prompt = brain.system_prompt(lean=True)
    assert "one WebSearch call" in prompt and "fast_search" not in prompt
    _keys(monkeypatch, BRAVE_SEARCH_API_KEY=FAKE_KEY)
    assert "fast_search" in brain.Brain().tool_table()
    prompt = brain.system_prompt(lean=True)
    assert "fast_search first" in prompt and "WebSearch only for deeper research" in prompt
