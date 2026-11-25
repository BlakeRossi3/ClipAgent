"""Weather on the first try: a keyless forecast tool, so Cecil never says it can't be had.

The owner (backlog #24): "Never say a page's info can't be had and then manage it after opening the
page. Fetch or read it on the first try, e.g. weather and scores." A web search for "weather today"
returned stale undated pages, Cecil said it had nothing, then opened the forecast page for him.

* Data: Open-Meteo (no key): its geocoder turns a place ("Northport, NY") into coordinates, then the
  forecast API gives now / today / the next days in Fahrenheit. Tests inject :func:`set_http`.
* His home: kv ``weather:home`` (a place name); the tool uses it when no place is given and
  ``remember_home: true`` saves the place asked for.
* Tool ``weather``; CLI ``python -m assistant.weather [place] [--home PLACE]``.
"""

from __future__ import annotations

import sys
from typing import Any, Callable
from urllib.parse import urlencode

import db

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HOME_KV = "weather:home"
MAX_DAYS = 7

# WMO weather codes (Open-Meteo) -> a few spoken words.
CODES = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "cloudy", 45: "foggy", 48: "foggy",
         51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
         61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
         71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "showers", 81: "showers",
         82: "heavy showers", 85: "snow showers", 86: "heavy snow showers", 95: "thunderstorms",
         96: "thunderstorms with hail", 99: "thunderstorms with hail"}
STATES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
          "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
          "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
          "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
          "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
          "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
          "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
          "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
          "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
          "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming"}

_http: Callable[[str], dict[str, Any]] | None = None


class WeatherError(RuntimeError):
    """The place or the forecast could not be found."""


def set_http(fn: Callable[[str], dict[str, Any]] | None) -> None:
    """Tests: replace the HTTP GET (url -> parsed JSON). None = the real one."""
    global _http
    _http = fn


def _real_get(url: str) -> dict[str, Any]:
    import httpx

    with httpx.Client(timeout=15, follow_redirects=True, headers={"User-Agent": "ClipAgent/1.0 (personal assistant)"}) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.json()


def _get(url: str) -> dict[str, Any]:
    try:
        data = (_http or _real_get)(url)
    except Exception as exc:
        raise WeatherError(f"weather fetch failed: {type(exc).__name__}: {exc}") from exc
    if not isinstance(data, dict):
        raise WeatherError("weather service gave no data")
    return data


def get_home() -> str:
    return str(db.kv_get(HOME_KV, "") or "").strip()


def set_home(place: str) -> str:
    p = " ".join(str(place or "").split())[:80]
    if not p:
        raise WeatherError("home must be a place name, e.g. Northport, NY")
    db.kv_set(HOME_KV, p)
    return p


def _matches(result: dict[str, Any], qualifier: str) -> bool:
    q = qualifier.strip().lower()
    if not q:
        return True
    q_full = STATES.get(q.upper(), q).lower()
    fields = [str(result.get(k) or "").lower() for k in ("admin1", "admin2", "country", "country_code")]
    return any(f and (f == q or f == q_full or q_full in f) for f in fields)


def geocode(place: str) -> dict[str, Any]:
    """'Northport, NY' -> the best Open-Meteo match (name, admin1, latitude, longitude, timezone)."""
    parts = [p.strip() for p in str(place or "").split(",") if p.strip()]
    if not parts:
        raise WeatherError("no place given and no home saved")
    data = _get(GEOCODE_URL + "?" + urlencode({"name": parts[0], "count": 10, "language": "en", "format": "json"}))
    results = [r for r in data.get("results") or [] if isinstance(r, dict)]
    for qualifier in parts[1:]:
        results = [r for r in results if _matches(r, qualifier)] or results
    if not results:
        raise WeatherError(f"couldn't find a place called {place!r}")
    return results[0]


def fetch(lat: float, lon: float, days: int = 3) -> dict[str, Any]:
    q = {"latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": max(1, min(MAX_DAYS, days)),
         "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "precipitation_unit": "inch",
         "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
         "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"}
    data = _get(FORECAST_URL + "?" + urlencode(q))
    if not isinstance(data.get("daily"), dict):
        raise WeatherError("forecast had no days")
    return data


def _r(x: Any) -> int | None:
    try:
        return round(float(x))
    except (TypeError, ValueError):
        return None


def parse(data: dict[str, Any]) -> dict[str, Any]:
    """Forecast JSON -> {now: {...} | None, days: [{date, sky, high, low, rain}]} (pure)."""
    cur = data.get("current") or {}
    now = None
    if cur:
        now = {"temp": _r(cur.get("temperature_2m")), "feels": _r(cur.get("apparent_temperature")),
               "sky": CODES.get(int(cur.get("weather_code") or 0), "unsettled"), "wind": _r(cur.get("wind_speed_10m"))}
    d = data.get("daily") or {}
    days = []
    for i, date in enumerate(d.get("time") or []):
        def at(key: str) -> Any:
            vals = d.get(key) or []
            return vals[i] if i < len(vals) else None
        code = at("weather_code")
        days.append({"date": date, "sky": CODES.get(int(code), "unsettled") if code is not None else "unknown",
                     "high": _r(at("temperature_2m_max")), "low": _r(at("temperature_2m_min")),
                     "rain": _r(at("precipitation_probability_max"))})
    return {"now": now, "days": days}


def _label(place: dict[str, Any]) -> str:
    bits = [str(place.get("name") or "")]
    admin = str(place.get("admin1") or "")
    if admin and admin != bits[0]:
        bits.append(admin)
    return ", ".join(b for b in bits if b)


def summary(where: str, fc: dict[str, Any]) -> str:
    """One or two plain sentences (the tool's answer; Cecil turns it into speech)."""
    out = []
    now = fc["now"]
    if now and now["temp"] is not None:
        feels = f", feels like {now['feels']}" if now["feels"] is not None and abs(now["feels"] - now["temp"]) >= 3 else ""
        out.append(f"{where} now: {now['temp']}°F and {now['sky']}{feels}.")
    if fc["days"]:
        t = fc["days"][0]
        rain = f", {t['rain']}% chance of rain" if t["rain"] else ""
        out.append(f"Today {t['sky']}, high {t['high']}°F, low {t['low']}°F{rain}.")
    return " ".join(out)


def forecast(place: str | None = None, days: int = 3) -> dict[str, Any]:
    """The weather for ``place`` (or his saved home): spoken summary plus the days."""
    where = (place or "").strip() or get_home()
    if not where:
        raise WeatherError("no place given and no home saved: pass place (his vault notes say where he lives) "
                           "with remember_home: true")
    loc = geocode(where)
    fc = parse(fetch(float(loc["latitude"]), float(loc["longitude"]), days))
    label = _label(loc)
    db.log_event("assistant", f"weather: {label}", data={"days": len(fc["days"])})
    return {"place": label, "summary": summary(label, fc), "now": fc["now"], "days": fc["days"],
            "source": "Open-Meteo"}


DESCRIPTION = ("Live weather, no search needed: current conditions, today and the next few days for a place "
               "(default his saved home). Use this first for any weather question. `place` = 'Town, ST' or "
               "'City, Country'; `days` 1-7 (default 3); `remember_home: true` saves place as his home.")


def _tool(ctx: Any, a: dict[str, Any]) -> Any:
    from assistant.tools import ToolError

    try:
        place = a.get("place")
        if place and a.get("remember_home"):
            set_home(place)
        return forecast(place, int(a.get("days") or 3))
    except WeatherError as exc:
        raise ToolError(str(exc)) from None


def register_tools(register: Any) -> None:
    from assistant.tools import Tool, _obj

    register(Tool("weather", DESCRIPTION, _obj({
        "place": {"type": "string"}, "days": {"type": "integer"}, "remember_home": {"type": "boolean"},
    }), _tool, owner=__name__), replace=True)


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="python -m assistant.weather", description="Live weather (Open-Meteo)")
    p.add_argument("place", nargs="?")
    p.add_argument("--home", help="save this place as his home")
    args = p.parse_args(argv)
    if args.home:
        print("home:", set_home(args.home))
    r = forecast(args.place)
    print(r["summary"])
    for d in r["days"]:
        print(f"  {d['date']}: {d['sky']}, {d['high']}/{d['low']}°F, rain {d['rain']}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
