"""Small text helpers shared by every module that cleans model- or page-supplied text.

Before this, modules each grew their own ``_clean`` / ``_s`` / ``_line`` with slightly different rules.
New code uses these; a module keeps its own variant only when it really does something more (strip HTML,
add an ellipsis for display), and says so in its docstring.
"""

from __future__ import annotations

import re
from typing import Any

_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def one_line(text: Any, limit: int) -> str:
    """Whitespace (and control characters) collapsed to single spaces, cut to ``limit`` characters."""
    return " ".join(_CTRL.sub(" ", str(text or "")).split())[:limit]


def paragraphs(text: Any, limit: int) -> str:
    """Each line's whitespace collapsed, line breaks kept, ends trimmed, cut to ``limit`` characters."""
    lines = (" ".join(_CTRL.sub(" ", ln).split()) for ln in str(text or "").replace("\r", "").split("\n"))
    return "\n".join(lines).strip()[:limit]
