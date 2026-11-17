"""Invincible's corner: the silly home-page widget is wired up."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_widget_in_home_template():
    html = (ROOT / "templates" / "home.html").read_text(encoding="utf-8")
    assert 'data-widget="invincible"' in html
    assert "data-inv-hero" in html
    assert "/dashboard/static/invincible.js" in html


def test_widget_assets_exist():
    js = (ROOT / "static" / "invincible.js").read_text(encoding="utf-8")
    assert "[data-inv]" in js and "fetch(" not in js
    css = (ROOT / "static" / "home.css").read_text(encoding="utf-8")
    assert ".inv-hero" in css
