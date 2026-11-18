"""assistant/tilecheck.py: look at each tile in the real page and say whether it can be read."""

from __future__ import annotations

import pytest
from assistant_fixtures import assistant_env  # noqa: F401

from assistant import tilecheck, tiles, tools

pytestmark = pytest.mark.usefixtures("assistant_env")


def _tile(**kw):
    base = {"title": "Weather", "width": 400, "height": 120, "left": 900, "right": 1300, "top": 200,
            "visible": True, "min_font": 13.0, "clipped": [], "text": "Weather 58°F Light rain"}
    base.update(kw)
    return base


def test_a_clean_tile_passes():
    rep = tilecheck.analyse([_tile()], viewport_width=1400)
    assert rep["ok"] is True and rep["tiles"][0]["problems"] == []


def test_each_kind_of_unreadable_tile_is_named():
    rep = tilecheck.analyse([
        _tile(title="Clipped", clipped=["dtile-note"]),
        _tile(title="Tiny", min_font=8.5),
        _tile(title="Narrow", width=90),
        _tile(title="Off", left=1300, right=1700),
        _tile(title="Hidden", visible=False, width=0, height=0),
        _tile(title="Short", text="Settings > Skills: tick the…"),
        _tile(title="Cramped", width=190, height=560),
        _tile(title="Below", hidden_px=200),
    ], viewport_width=1400)
    assert rep["ok"] is False
    probs = {t["title"]: " ".join(t["problems"]) for t in rep["tiles"]}
    assert "cut off" in probs["Clipped"]
    assert "too small" in probs["Tiny"]
    assert "too narrow" in probs["Narrow"]
    assert "off the screen" in probs["Off"]
    assert "not visible" in probs["Hidden"]
    assert "shortened" in probs["Short"]
    assert "cramped" in probs["Cramped"]
    assert "has to scroll" in probs["Below"]


def test_no_tiles_is_reported_not_passed_silently():
    rep = tilecheck.analyse([], viewport_width=1400)
    assert rep["ok"] is True and rep["tiles"] == [] and "no tiles" in rep["summary"]


def test_check_without_a_browser_says_so(monkeypatch):
    def boom(*a, **k):
        raise tilecheck.CheckError("no Chrome or Edge found")
    monkeypatch.setattr(tilecheck, "measure", boom)
    with pytest.raises(tilecheck.CheckError):
        tilecheck.check()


def test_tile_check_tool_is_registered_and_returns_the_report(monkeypatch):
    monkeypatch.setattr(tilecheck, "measure", lambda **k: ([_tile(title="Clipped", clipped=["dtile-body"])],
                                                           1400, "C:/tmp/shot.png"))
    tools.load_extensions()
    t = tools.REGISTRY["tile_check"]
    out = t.fn(None, {})
    assert out["ok"] is False and out["screenshot"] == "C:/tmp/shot.png"
    assert "Clipped" in out["summary"]


def test_tile_check_tool_turns_a_missing_browser_into_a_tool_error(monkeypatch):
    def boom(**k):
        raise tilecheck.CheckError("no Chrome or Edge found")
    monkeypatch.setattr(tilecheck, "measure", boom)
    tools.load_extensions()
    with pytest.raises(tools.ToolError):
        tools.REGISTRY["tile_check"].fn(None, {})


def test_tile_show_tells_the_model_to_check_it():
    assert "tile_check" in tiles.SHOW_DESCRIPTION
