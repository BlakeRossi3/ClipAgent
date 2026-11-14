"""assistant/panels.py: the display tool's sanitiser (XSS attempts stripped) and validation."""

from __future__ import annotations

import pytest

from assistant import events, panels
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")

ATTACKS = [
    '<img src=x onerror="alert(1)">',
    '<script>alert(1)</script><p class="hud-note">ok</p>',
    '<a href="javascript:alert(1)">click</a>',
    '<div style="position:fixed;inset:0;z-index:9999" class="hud-rows boot">x</div>',
    '<iframe src="https://evil.example"></iframe>',
    '<svg><script>alert(1)</script></svg>',
    '<img src="javascript:alert(1)">',
    '<p onclick="steal()" class="hud-hot">hi</p>',
    '<style>body{display:none}</style>',
    '<img src="http://192.168.1.1/x.png" onload="x()">',
    '<math><mtext><img src=x onerror=alert(1)></mtext></math>',
    '<div class="hud-note">&lt;script&gt;alert(1)&lt;/script&gt;</div>',
    '<form action="https://evil.example"><input name="pw"></form>',
    '<p>"<b>quoted</b>" <noscript><img src=x onerror=alert(1)></noscript></p>',
]


@pytest.mark.parametrize("html", ATTACKS)
def test_xss_attempts_are_stripped(html):
    out = panels.sanitize(html)
    low = out.lower()
    for bad in ("<script", "onerror", "onload", "onclick", "javascript:", "<iframe", "<svg", "<style", "<a ",
                "href", "position:fixed", "<form", "<input", "boot", "<math"):
        assert bad not in low, (html, out)


def test_design_system_survives():
    html = ('<div class="hud-rows"><div class="hud-row"><span class="hud-idx">01</span><span class="hud-main">'
            '<span class="hud-label">Tests</span><span class="hud-sub">412 passed</span></span>'
            '<span class="hud-tag">ok</span></div></div><div class="hud-bar" style="--v:0.62"></div>'
            '<img class="hud-img" src="https://i.ytimg.com/vi/x/hq.jpg" alt="thumb">')
    out = panels.sanitize(html)
    assert 'class="hud-rows"' in out and 'style="--v:0.62"' in out and "412 passed" in out
    assert '<img class="hud-img" src="https://i.ytimg.com/vi/x/hq.jpg" alt="thumb" referrerpolicy="no-referrer"' in out


def test_display_publishes_and_refuses_empty_panels():
    assert panels.display({"title": "Inbox", "html": '<p class="hud-note">Three new.</p>', "accent": "green"}) == "On screen."
    e = events.BUS.since(0)[-1]
    assert e["type"] == "panel" and e["panel"]["title"] == "Inbox" and e["panel"]["accent"] == "green"
    with pytest.raises(ValueError):
        panels.make_panel({"title": "x", "html": "<script>alert(1)</script>"})
    p = panels.make_panel({"title": "y" * 99, "html": "<b>hi</b>", "accent": "neon", "hold": "forever"})
    assert len(p["title"]) == panels.MAX_TITLE and p["accent"] == "default" and p["hold"] == "turn"


def test_browser_sanitiser_uses_the_same_allowlist():
    """static/jarvis.js carries the same class and tag allowlist (one rule set, two places)."""
    from pathlib import Path

    js = (Path(panels.__file__).resolve().parent.parent / "static" / "jarvis.js").read_text(encoding="utf-8")
    for c in panels.ALLOWED_CLASSES:
        assert f"'{c}'" in js, c
    for t in panels.ALLOWED_TAGS:
        assert f"'{t}'" in js, t
