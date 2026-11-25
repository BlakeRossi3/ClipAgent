"""assistant/weather.py: live weather on the first try (backlog #24), plus the first-try rule in the prompt."""

from __future__ import annotations

import pytest
from assistant_fixtures import assistant_env  # noqa: F401

from assistant import store, tools, weather

pytestmark = pytest.mark.usefixtures("assistant_env")

GEO = {"results": [
    {"name": "Northport", "admin1": "Alabama", "country_code": "US", "latitude": 33.2, "longitude": -87.6},
    {"name": "Northport", "admin1": "New York", "admin2": "Suffolk", "country_code": "US",
     "latitude": 40.9, "longitude": -73.34},
]}
FC = {"current": {"temperature_2m": 44.6, "apparent_temperature": 39.2, "weather_code": 2, "wind_speed_10m": 9.1},
      "daily": {"time": ["2026-11-24", "2026-11-25"], "weather_code": [61, 0],
                "temperature_2m_max": [51.4, 48.0], "temperature_2m_min": [37.2, 33.9],
                "precipitation_probability_max": [60, 0]}}


@pytest.fixture
def env():
    store.init_store()
    calls: list[str] = []

    def get(url):
        calls.append(url)
        return GEO if "geocoding" in url else FC

    weather.set_http(get)
    yield calls
    weather.set_http(None)


def test_geocode_picks_the_state_asked_for(env):
    assert weather.geocode("Northport, NY")["admin1"] == "New York"
    assert weather.geocode("Northport")["admin1"] == "Alabama"   # no qualifier: first result
    assert "name=Northport" in env[0]


def test_forecast_summary_and_days(env):
    r = weather.forecast("Northport, NY", days=2)
    assert r["place"] == "Northport, New York" and r["source"] == "Open-Meteo"
    assert r["summary"] == ("Northport, New York now: 45°F and partly cloudy, feels like 39. "
                            "Today light rain, high 51°F, low 37°F, 60% chance of rain.")
    assert r["days"][1] == {"date": "2026-11-25", "sky": "clear", "high": 48, "low": 34, "rain": 0}
    assert "latitude=40.9" in env[1] and "temperature_unit=fahrenheit" in env[1]


def test_tool_saves_home_and_uses_it_next_time(env):
    tools.load_extensions()
    from assistant import registry

    main = registry.load_agents(skills=registry.load_skills(), known_tools=tools.known_tool_names())["main"]
    assert "weather" in main.tools
    ctx = tools.ToolContext(channel="voice", agent="main", source="owner")
    r = tools.dispatch("weather", {}, ctx, allowed=set(main.tools))
    assert not r.ok and "no home saved" in r.content
    r = tools.dispatch("weather", {"place": "Northport, NY", "remember_home": True}, ctx, allowed=set(main.tools))
    assert r.ok and "45°F" in r.content and weather.get_home() == "Northport, NY"
    r = tools.dispatch("weather", {}, ctx, allowed=set(main.tools))
    assert r.ok and "Northport, New York" in r.content


def test_feed_failure_is_a_tool_error(env):
    def boom(url):
        raise OSError("offline")

    weather.set_http(boom)
    tools.load_extensions()
    ctx = tools.ToolContext(channel="voice", agent="main", source="owner")
    r = tools.dispatch("weather", {"place": "Paris"}, ctx, allowed={"weather"})
    assert not r.ok and "offline" in r.content
    weather.set_http(lambda url: {"results": []})
    with pytest.raises(weather.WeatherError):
        weather.geocode("Nowhereville")


def test_voice_prompt_says_get_it_on_the_first_try():
    from assistant import brain

    for facts in (brain.FACTS_FAST, brain.FACTS_WEB):
        text = brain.VOICE_PROMPT.format(owner="B", name="Cecil", h="sir", facts=facts + brain.FACTS_FIRST_TRY)
        assert "weather tool" in text and "WebFetch" in text and "can't be had" in text
