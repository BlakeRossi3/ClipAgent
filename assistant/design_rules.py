"""The design guidelines Blake approved from the reels he sent (backlog item #41), as one short checklist.

He asked to "just take the design guidelines" from the Impeccable reel (installed text-only as
``skills/impeccable-guides``) alongside ``skills/frontend-design``. This module is the distilled,
in-our-own-words version that gets put in front of every job that builds something he looks at
(the morning surprise today; tile and page work read the same text from ``agents/main.md``), plus the
guide files to open for the full detail. Pure text: no I/O at import.
"""

from __future__ import annotations

from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"

# Guides worth opening for anything visual, relative to SKILLS_DIR (a test checks they exist).
GUIDES = (
    "impeccable-guides/reference/polish.md",
    "impeccable-guides/reference/critique-rubric.md",
    "frontend-design/SKILL.md",
)

RULES = (
    "One clear focal point; the main thing he came for is the biggest, everything else steps back.",
    "Real content, never lorem ipsum or filler badges, stats or labels that say nothing.",
    "Avoid the generic AI look: no ALL-CAPS eyebrow labels over every heading, no identical rounded cards "
    "with the same grey shadow, no gradient washes as decoration, no '01/02/03' unless it really is a sequence.",
    "One or two typefaces with a clear size scale; lines under about 80 characters; consistent icon style.",
    "Group related things tightly and separate groups generously; align to one spacing scale.",
    "Every state designed: loading, empty, error and success, not just the happy path.",
    "Motion only to show what changed or one deliberate moment, never on everything.",
    "Readable at a glance: strong contrast, nothing cut off, works at small and large sizes.",
    "Inside ClipAgent keep the house look (static/theme.css: dark liquid glass, cyan accent); sharpen it, never restyle it.",
)


def checklist() -> str:
    """The design checklist as prompt text, naming the guides to open for detail."""
    lines = "\n".join(f"  - {r}" for r in RULES)
    guides = ", ".join(f"skills/{g}" for g in GUIDES)
    return ("Design guidelines (from the impeccable-guides and frontend-design skills he approved):\n"
            f"{lines}\n  Before calling it done, check it against skills/impeccable-guides/reference/polish.md "
            f"(full detail: {guides}).")
