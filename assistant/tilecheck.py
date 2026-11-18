"""Look at every tile in the real Jarvis page and say whether he can read it.

The owner's rule: before a tile counts as done, look at it in the actual page. This opens the
dashboard in his installed Chrome (or Edge) through Playwright, headless and with a throwaway
profile (never his own), measures each dynamic tile (``.dtile``) in the browser and takes a
screenshot. :func:`analyse` turns the measurements into plain problems:

* text cut off (an element whose content overflows a box that hides it),
* text too small (under :data:`MIN_FONT_PX`),
* tile too narrow (under :data:`MIN_WIDTH_PX`) or cramped (a tall thin column of text),
* off the screen, not visible, or partly hidden in its scrolling panel (the rail),
* text shortened with "…" (the store trimmed it; put the detail in rows or the body).

Tool: ``tile_check`` (returns the report and the PNG path; Read the PNG too).
CLI: ``python -m assistant.tilecheck [PATH]`` (default ``/dashboard/``).
Note: waits for ``load``, not ``networkidle`` - the page polls forever so it never goes idle.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

BASE = os.environ.get("CLIPAGENT_BASE_URL", "http://127.0.0.1:8000")
VIEWPORT = (1400, 1000)
MIN_FONT_PX = 10.5
MIN_WIDTH_PX = 140
CRAMPED_RATIO = 2.6   # height / width above this, on a tile under CRAMPED_WIDTH_PX, reads as a thin column
CRAMPED_WIDTH_PX = 260
CHANNELS = ("chrome", "msedge")

# Runs in the page: one record per dynamic tile.
_MEASURE_JS = r"""
() => Array.from(document.querySelectorAll('.dtile')).map(el => {
  const r = el.getBoundingClientRect();
  const cs = getComputedStyle(el);
  const nodes = [el, ...el.querySelectorAll('*')].filter(n => n.tagName !== 'FORM' && !n.closest('form'));
  let minFont = 99; const clipped = [];
  for (const n of nodes) {
    const s = getComputedStyle(n);
    const hasText = Array.from(n.childNodes).some(c => c.nodeType === 3 && c.textContent.trim());
    if (hasText) minFont = Math.min(minFont, parseFloat(s.fontSize) || 99);
    const hides = v => v === 'hidden' || v === 'clip' || v === 'auto' || v === 'scroll';
    if ((hides(s.overflowX) && n.scrollWidth > n.clientWidth + 1) ||
        (hides(s.overflowY) && n.scrollHeight > n.clientHeight + 1) ||
        (s.textOverflow === 'ellipsis' && n.scrollWidth > n.clientWidth + 1) ||
        (parseInt(s.webkitLineClamp) > 0 && n.scrollHeight > n.clientHeight + 1)) {
      clipped.push((n.className && String(n.className)) || n.tagName.toLowerCase());
    }
  }
  let box = el.parentElement, hiddenPx = 0;
  while (box && box !== document.body) {
    const o = getComputedStyle(box).overflowY;
    if (o !== 'visible') {
      const b = box.getBoundingClientRect();
      hiddenPx = Math.max(0, r.bottom - b.bottom, b.top - r.top);
      break;
    }
    box = box.parentElement;
  }
  const title = (el.querySelector('.dtile-title') || {}).innerText || '';
  return {title: title.trim(), width: r.width, height: r.height, left: r.left, right: r.right, top: r.top,
          visible: r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none',
          min_font: minFont, clipped, hidden_px: hiddenPx, text: el.innerText || ''};
})
"""


class CheckError(RuntimeError):
    """The page could not be looked at (no browser, app not running)."""


def _problems(t: dict[str, Any], viewport_width: int) -> list[str]:
    out: list[str] = []
    if not t.get("visible"):
        return ["not visible on the page"]
    if t.get("clipped"):
        out.append("text cut off in " + ", ".join(sorted(set(t["clipped"])))[:120])
    if float(t.get("min_font", 99)) < MIN_FONT_PX:
        out.append(f"text too small ({float(t['min_font']):.1f}px)")
    w, h = float(t.get("width", 0)), float(t.get("height", 0))
    if w < MIN_WIDTH_PX:
        out.append(f"too narrow ({w:.0f}px)")
    elif w < CRAMPED_WIDTH_PX and h / max(w, 1) > CRAMPED_RATIO:
        out.append(f"cramped: a thin column {w:.0f}px wide and {h:.0f}px tall; make it wide or shorter")
    if float(t.get("hidden_px", 0)) > 4:
        out.append(f"{float(t['hidden_px']):.0f}px hidden outside its panel; he has to scroll to read it")
    if float(t.get("left", 0)) < 0 or float(t.get("right", 0)) > viewport_width + 1:
        out.append("partly off the screen")
    if "…" in str(t.get("text", "")):
        out.append("text shortened with '…'; the full detail never reaches the page")
    return out


def analyse(measured: list[dict[str, Any]], viewport_width: int = VIEWPORT[0]) -> dict[str, Any]:
    """Measurements from the page -> ``{ok, summary, tiles: [{title, width, height, problems}]}``."""
    rows = [{"title": t.get("title") or "(untitled)", "width": round(float(t.get("width", 0))),
             "height": round(float(t.get("height", 0))), "problems": _problems(t, viewport_width)}
            for t in measured]
    bad = [r for r in rows if r["problems"]]
    if not rows:
        summary = "no tiles on the page"
    elif not bad:
        summary = f"all {len(rows)} tile(s) readable"
    else:
        summary = "; ".join(f"{r['title']}: {', '.join(r['problems'])}" for r in bad)
    return {"ok": not bad, "summary": summary, "tiles": rows}


def measure(path: str = "/dashboard/", out: Path | None = None) -> tuple[list[dict[str, Any]], int, str]:
    """Open the page in a headless system browser; return (tile measurements, viewport width, PNG path)."""
    try:
        from playwright.sync_api import Error as PwError
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise CheckError("Playwright is not installed in the app's environment") from None
    out = out or Path(tempfile.gettempdir()) / "cecil-tiles.png"
    try:
        with sync_playwright() as p:
            browser = None
            for ch in CHANNELS:
                try:
                    browser = p.chromium.launch(channel=ch, headless=True)
                    break
                except PwError:
                    continue
            if browser is None:
                raise CheckError("no Chrome or Edge found")
            try:
                page = browser.new_page(viewport={"width": VIEWPORT[0], "height": VIEWPORT[1]})
                page.goto(BASE + path, wait_until="load", timeout=20000)
                page.wait_for_timeout(1200)   # tiles fade in (.35 s) and the live refresh lands
                measured = page.evaluate(_MEASURE_JS)
                box = page.locator(".jv-tiles")
                if measured and box.count():
                    box.first.screenshot(path=str(out))
                else:
                    page.screenshot(path=str(out))
            finally:
                browser.close()
    except PwError as exc:
        raise CheckError(f"could not open the page: {str(exc).splitlines()[0][:160]}") from None
    return list(measured or []), VIEWPORT[0], str(out)


def check(path: str = "/dashboard/") -> dict[str, Any]:
    measured, vw, shot = measure(path=path)
    rep = analyse(measured, viewport_width=vw)
    rep["screenshot"] = shot
    return rep


def _tool_check(ctx: Any, a: dict[str, Any]) -> Any:
    from assistant.tools import ToolError

    try:
        return check(a.get("path") or "/dashboard/")
    except CheckError as exc:
        raise ToolError(str(exc)) from None


def register_tools(register: Any) -> None:
    from assistant.tools import Tool, _obj

    register(Tool("tile_check",
                  "Look at the tiles in the real Jarvis page (headless Chrome): says per tile whether text is cut "
                  "off, too small, cramped or off screen, and gives a screenshot path to Read. Run after tile_show "
                  "and fix any problem before saying it's done.",
                  _obj({"path": {"type": "string", "description": "page path, default /dashboard/"}}),
                  _tool_check, owner=__name__), replace=True)


if __name__ == "__main__":
    try:
        print(json.dumps(check(sys.argv[1] if len(sys.argv) > 1 else "/dashboard/"), indent=2, ensure_ascii=False))
    except CheckError as e:
        print(f"tile check failed: {e}", file=sys.stderr)
        sys.exit(1)
