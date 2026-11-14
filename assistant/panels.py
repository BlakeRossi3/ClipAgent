"""The ``display`` tool: Sterling composes a small HUD panel for the Jarvis page's activity rail.

Ported from adewaskar/jarvis ``bridge/panels.mjs`` and ``src/ui/sanitise.ts`` (MIT License,
Copyright (c) 2026 Aditya Dewaskar). The model writes the markup itself, from a fixed design system
of ``hud-*`` classes, so a list of results, a single figure and a picture each look right while the
page still looks like one interface. The markup is untrusted (the model may just have read a web
page), so it is sanitised twice with the same allowlist: here, before it is published, and again in
the browser (``static/jarvis.js``, ``sanitizePanel``) before it touches the DOM.

Allowed: the tags in :data:`ALLOWED_TAGS`, the classes in :data:`ALLOWED_CLASSES`, ``style`` only as
``--v:<number>`` (the progress bar), ``src`` only on ``<img>`` and only ``https://``, ``http://`` or
``data:image/...``. Everything else (scripts, styles, event handlers, links, forms, iframes, SVG,
other URLs, unknown classes) is removed; the text inside a removed harmless tag is kept.

Remote images in a panel are never loaded by the page directly: their URLs are registered with
``assistant.hud.allow_url`` and the page rewrites each http(s) ``src`` to the SSRF-guarded proxy
``/voice/api/img?u=`` (as adewaskar/jarvis does with its ``/img`` endpoint). Other panel kinds (maps,
media, data, lists) are built by ``assistant/hud.py`` and rendered by the page with its own code.
"""

from __future__ import annotations

import html
import itertools
import re
import time
from html.parser import HTMLParser
from typing import Any

ALLOWED_TAGS = frozenset({"div", "span", "p", "ul", "ol", "li", "img", "b", "strong", "em", "i", "br", "small",
                          "table", "thead", "tbody", "tr", "td", "th", "code", "pre", "h3", "h4"})
VOID_TAGS = frozenset({"img", "br"})
# Removed together with everything inside them.
DROP_WITH_CONTENT = frozenset({"script", "style", "iframe", "object", "embed", "svg", "math", "template", "noscript",
                               "textarea", "select", "button", "form", "frame", "frameset", "head", "title", "video",
                               "audio", "canvas", "link", "meta", "base"})
ALLOWED_CLASSES = frozenset({
    "hud-rows", "hud-row", "hud-idx", "hud-main", "hud-label", "hud-sub", "hud-tag", "hud-metric", "hud-unit",
    "hud-note", "hud-img", "hud-caption", "hud-grid", "hud-bar", "hud-dim", "hud-hot", "hud-gallery", "hud-thumb",
    "hud-figure"})
ACCENTS = ("default", "amber", "violet", "green", "red")
HOLDS = ("turn", "sticky")
MAX_HTML = 12_000
MAX_TITLE = 40
_SRC_OK = re.compile(r"^(?:https?://[^\s\"'<>`]+|data:image/(?:png|jpe?g|gif|webp);base64,[A-Za-z0-9+/=]+)$", re.I)
_V = re.compile(r"--v\s*:\s*([0-9]*\.?[0-9]+)")

DESIGN_SYSTEM = """LAYOUT CLASSES: compose these and nothing else (any other class is stripped):
  .hud-rows     vertical list container        .hud-row    one row: .hud-idx, .hud-main, .hud-tag inside
  .hud-idx      leading index, dim, monospaced  .hud-main   the row's text column
  .hud-label    primary line                    .hud-sub    secondary line, dimmed
  .hud-tag      small trailing tag              .hud-metric huge numeral for one headline figure
  .hud-unit     caption under a metric          .hud-note   a short passage of prose
  .hud-img      full-width <img>                .hud-caption one line under an image
  .hud-grid     two-column grid                 .hud-bar    progress bar: style="--v:0.62" for 62%
  .hud-dim      de-emphasise                    .hud-hot    emphasise (accent colour)
  .hud-gallery  grid of .hud-thumb images       .hud-figure an image with its caption
Tags: div span p ul ol li b strong em i br small table thead tbody tr td th code pre h3 h4 img.
Images: only https URLs that appeared verbatim in a tool result (never invent one). No links,
no <style>, <script>, forms, iframes or event handlers (all stripped). No inline colours: pick an
accent instead. Keep it to about six rows or forty words: it is glanced at while listening.

Examples:
<div class="hud-rows"><div class="hud-row"><span class="hud-idx">01</span><span class="hud-main"><span class="hud-label">Tests</span><span class="hud-sub">412 passed</span></span><span class="hud-tag">ok</span></div></div>
<div><span class="hud-metric">1,284</span><span class="hud-unit">views this week</span></div>
<p class="hud-note">Three uploads queued. <span class="hud-hot">One failed</span> on quota.</p>"""

DESCRIPTION = ("Put a small panel on the Jarvis page beside the voice ring, for answers worth seeing rather than "
               "hearing: a list of results, a figure, a short table, a status readout, an image from a tool "
               "result. Call it before or while you speak. Never read the panel aloud: say what it means in one "
               "or two sentences.\n\n" + DESIGN_SYSTEM)

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["title", "html"],
          "properties": {
              "title": {"type": "string", "description": "Two to four words, e.g. 'TEST RESULTS'."},
              "html": {"type": "string", "description": "The panel body, composed from the design system."},
              "accent": {"type": "string", "enum": list(ACCENTS),
                         "description": "default cyan; amber pending; violet generated; green healthy; red failure."},
              "hold": {"type": "string", "enum": list(HOLDS),
                       "description": "turn = cleared when he next speaks (default); sticky = stays until replaced."},
              "size": {"type": "string", "enum": ["card", "blade"],
                       "description": "card = beside the ring (default); blade = large, over the ring, for reading."},
          }}

_seq = itertools.count(1)


class _Sanitiser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []
        self.drop = 0          # depth inside a DROP_WITH_CONTENT element

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self.drop:
            if tag in DROP_WITH_CONTENT and tag not in VOID_TAGS:
                self.drop += 1
            return
        if tag in DROP_WITH_CONTENT:
            self.drop = 1
            return
        if tag not in ALLOWED_TAGS:
            return
        kept: list[str] = []
        for name, value in attrs:
            name, value = name.lower(), (value or "")
            if name == "class":
                cls = [c for c in value.split() if c in ALLOWED_CLASSES]
                if cls:
                    kept.append(f'class="{" ".join(cls)}"')
            elif name == "style":
                m = _V.search(value)
                if m:
                    kept.append(f'style="--v:{min(1.0, float(m.group(1))):g}"')
            elif name == "src" and tag == "img":
                v = value.strip()
                if _SRC_OK.match(v):
                    kept.append(f'src="{html.escape(v, quote=True)}"')
            elif name in ("alt", "title"):
                kept.append(f'{name}="{html.escape(value[:200], quote=True)}"')
            elif name in ("colspan", "rowspan") and value.isdigit() and int(value) < 20:
                kept.append(f'{name}="{value}"')
        if tag == "img":
            if not any(k.startswith("src=") for k in kept):
                return
            kept += ['referrerpolicy="no-referrer"', 'loading="lazy"']
        self.out.append(f"<{tag}{(' ' + ' '.join(kept)) if kept else ''}>")
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID_TAGS and self.stack and self.stack[-1] == tag.lower():
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.drop:
            if tag in DROP_WITH_CONTENT:
                self.drop -= 1
            return
        if tag in self.stack:
            while self.stack:
                t = self.stack.pop()
                self.out.append(f"</{t}>")
                if t == tag:
                    break

    def handle_data(self, data: str) -> None:
        if not self.drop:
            self.out.append(html.escape(data, quote=False))

    def result(self) -> str:
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def sanitize(markup: str) -> str:
    """Model-authored panel markup -> safe HTML (same allowlist as the browser's ``sanitizePanel``)."""
    p = _Sanitiser()
    p.feed((markup or "")[:MAX_HTML])
    p.close()
    return p.result()


def visible(markup: str) -> bool:
    """A panel needs visible text or an image (a blank card reads as a broken interface)."""
    return bool(re.sub(r"<[^>]*>", "", markup).strip()) or "<img " in markup


def make_panel(args: dict[str, Any], *, turn: str | None = None) -> dict[str, Any]:
    """Validate + sanitise ``display`` arguments -> the panel published to the page. Raises ValueError."""
    body = sanitize(str(args.get("html") or ""))
    if not visible(body):
        raise ValueError("Not shown: the panel body was empty after cleaning. Put visible text or an https "
                         "image in html, using only the hud-* classes.")
    accent = str(args.get("accent") or "default")
    hold = str(args.get("hold") or "turn")
    title = " ".join(str(args.get("title") or "").split())[:MAX_TITLE] or "DISPLAY"
    size = str(args.get("size") or "card")
    from assistant import hud

    for src in re.findall(r'<img [^>]*src="(https?://[^"]+)"', body):
        hud.allow_url(html.unescape(src))
    return {"id": f"p{int(time.time() * 1000):x}-{next(_seq)}", "kind": "markup", "title": title, "html": body,
            "accent": accent if accent in ACCENTS else "default", "hold": hold if hold in HOLDS else "turn",
            "size": size if size in ("card", "blade") else "card", "turn": turn}


def display(args: dict[str, Any], *, turn: str | None = None) -> str:
    """The tool: publish a panel. Returns the tool result text."""
    from assistant import events

    panel = make_panel(args, turn=turn)
    events.publish("panel", panel=panel)
    return "On screen."
