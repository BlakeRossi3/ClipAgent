"""Look at the dashboard the way the owner does: a headless-Chrome screenshot of a local page.

Use it after any visual change (a tile, a panel, CSS) and Read the PNG to check it is legible.
CLI: ``python -m assistant.screenshot [PATH] [--out FILE] [--size 1400x1000]`` (default PATH
``/dashboard/``; prints the PNG path). Uses a throwaway browser profile so it never touches his own.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

CHROME_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)
BASE = os.environ.get("CLIPAGENT_BASE_URL", "http://127.0.0.1:8000")


def find_browser() -> str | None:
    return next((p for p in CHROME_PATHS if Path(p).exists()), None)


def build_command(browser: str, url: str, out: Path, profile: str, size: str = "1400x1000") -> list[str]:
    return [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            f"--user-data-dir={profile}", f"--window-size={size.replace('x', ',')}",
            f"--screenshot={out}", url]


def shoot(path: str = "/dashboard/", out: Path | None = None, size: str = "1400x1000") -> Path:
    browser = find_browser()
    if not browser:
        raise RuntimeError("no Chrome or Edge found")
    out = out or Path(tempfile.gettempdir()) / "cecil-shot.png"
    with tempfile.TemporaryDirectory() as profile:
        subprocess.run(build_command(browser, BASE + path, out, profile, size),
                       timeout=60, capture_output=True, check=False)
    if not out.exists():
        raise RuntimeError("screenshot failed")
    return out


if __name__ == "__main__":
    args = sys.argv[1:]
    out = Path(args[args.index("--out") + 1]) if "--out" in args else None
    size = args[args.index("--size") + 1] if "--size" in args else "1400x1000"
    pos = [a for i, a in enumerate(args) if not a.startswith("--") and (i == 0 or not args[i - 1].startswith("--"))]
    print(shoot(pos[0] if pos else "/dashboard/", out, size))
