"""What Sterling says: filler lines, sentence splitting for streaming TTS, spoken-reply shaping.

Several ideas here are adapted from adewaskar/jarvis (MIT License, Copyright (c) 2026 Aditya
Dewaskar; ``src/lib/fillers.ts`` and ``src/lib/tts.ts``): the three filler pools (working /
acknowledge / attention) with a picker that never repeats the previous line, the character's
grammar for those lines (no "let me", no exclamation, "sir" at the end and only sometimes), and the
sentence splitter that ignores abbreviations and cuts run-on clauses at about 220 characters.
"""

from __future__ import annotations

import random
import re
from typing import Any, Callable, Mapping

# Said only when the brain has not started answering within VOICE_FILLER_DELAY_SEC.
WORKING = ("One moment, {h}.", "Working on it, {h}.", "Very good, {h}.", "Checking now.", "Right away, {h}.",
           "Looking into it.", "Under way.", "Certainly, {h}. One moment.")
# Still waiting after STILL_AFTER_SEC more: one more line, then quiet until the answer.
STILL = ("Still working on it, {h}.", "Nearly there, {h}.", "Bear with me, {h}.")
# Answering to his name before he has said what he wants.
ATTENTION = ("Yes, {h}?", "{H}?", "At your service, {h}.", "Standing by.", "Listening, {h}.")
ACKNOWLEDGE = ("Very good, {h}.", "As you wish, {h}.", "Understood.", "Certainly, {h}.")

STILL_AFTER_SEC = 14.0
LONG_REPLY_CHARS = 420      # longer than this (or > 4 sentences): speak only the first sentences
SUMMARY_SENTENCES = 3
MAX_UNSPOKEN = 220


class Picker:
    """Random line from a pool, never the same one twice running (canned lines sound canned)."""

    def __init__(self, pool: tuple[str, ...], rng: random.Random | None = None):
        self.pool, self.rng, self.last = pool, rng or random.Random(), -1

    def __call__(self, honorific: str = "sir") -> str:
        i = self.last
        if len(self.pool) > 1:
            while i == self.last:
                i = self.rng.randrange(len(self.pool))
        else:
            i = 0
        self.last = i
        return fill(self.pool[i], honorific)


def fill(line: str, honorific: str) -> str:
    return line.replace("{H}", honorific[:1].upper() + honorific[1:]).replace("{h}", honorific)


_ABBREV = re.compile(r"(?:^|\s)(mr|mrs|ms|dr|prof|sr|jr|st|vs|etc|e\.g|i\.e|approx|inc|ltd|no|a\.m|p\.m|u\.s|u\.k)\.$",
                     re.IGNORECASE)
_END = re.compile(r"[.!?][\"'”’)\]]?(?=\s)|\n\n")


def split_sentences(text: str) -> list[str]:
    """Sentences to speak one at a time, so speech starts before the whole answer is synthesised."""
    text = " ".join((text or "").replace("\n\n", " \n\n ").split(" ")).strip()
    out: list[str] = []
    buf = ""
    pos = 0
    for m in _END.finditer(text):
        piece = text[pos:m.end()]
        if _ABBREV.search(piece.strip()):
            continue  # "Mr. Stark": not a sentence end; keep accumulating from the same start
        buf = text[pos:m.end()]
        pos = m.end()
        out.extend(_cut_long(buf))
    rest = text[pos:]
    out.extend(_cut_long(rest))
    return [" ".join(s.split()) for s in out if s.strip()]


def _cut_long(s: str) -> list[str]:
    s = " ".join(s.split())
    parts = []
    while len(s) > MAX_UNSPOKEN:
        cut = max(s.rfind(", ", 0, MAX_UNSPOKEN), s.rfind("; ", 0, MAX_UNSPOKEN))
        if cut < 40:
            cut = s.rfind(" ", 0, MAX_UNSPOKEN)
        if cut <= 0:
            cut = MAX_UNSPOKEN
        parts.append(s[:cut + 1].strip())
        s = s[cut + 1:].strip()
    if s:
        parts.append(s)
    return parts


def prepare_reply(text: str, *, honorific: str = "sir",
                  polish: Callable[[str], str] | None = None) -> tuple[str, bool]:
    """Turn the assistant's answer into what is said aloud -> (spoken text, was_shortened).

    ``polish`` defaults to ``persona.polish(..., "voice")`` (no markdown, no URLs read aloud,
    600 characters at most). A long answer is cut to its first three sentences: the voice speaks the
    answer, it never points him at the dashboard (the full text is in the history anyway).
    """
    if polish is None:
        from assistant import persona

        def polish(t: str) -> str:  # noqa: F811
            return persona.polish(t, "voice")
    raw = (text or "").strip()
    spoken = polish(raw) if raw else ""
    sentences = split_sentences(spoken)
    long = len(raw) > LONG_REPLY_CHARS or len(sentences) > 4
    if long:
        spoken = " ".join(sentences[:SUMMARY_SENTENCES]) or spoken[:240]
    return spoken.strip(), long


# ---------------------------------------------------------------------------
# streamed answers: sentences as they complete, cleaned for speaking
# ---------------------------------------------------------------------------

class SentenceStream:
    """Feed text deltas; get back finished sentences (the last piece waits for more text or flush).

    As adewaskar/jarvis ``src/lib/tts.ts`` (MIT): a sentence is spoken the moment it is complete,
    abbreviations do not end a sentence, and a run-on clause is cut at a word boundary past
    ``MAX_UNSPOKEN`` characters so speech never stalls. Nothing is ever cut mid-word."""

    def __init__(self) -> None:
        self.buf = ""

    def feed(self, delta: str) -> list[str]:
        self.buf += delta
        parts = split_sentences(self.buf)
        if len(parts) <= 1:
            return []
        done = parts[:-1]
        self.buf = parts[-1] + (" " if self.buf[-1:].isspace() else "")   # keep the word boundary
        return done

    def flush(self) -> list[str]:
        out, self.buf = split_sentences(self.buf), ""
        return out


_URL = re.compile(r"\b(?:https?://|www\.)\S*[^\s.,;:!?)\]]", re.I)
_MD = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MARKS = re.compile(r"[*_`#>|]+")
# Plumbing Blake should never hear (the old two-layer voice leaked it). "channel" is only plumbing
# with these words: "your YouTube channel" is his business and stays.
_PLUMBING_SUBS = (
    (re.compile(r"\s*\b(?:from|on|in|over|via|through) (?:this|the|that|my|your) (?:voice |text |sms |dashboard )?"
                r"channel\b", re.I), ""),
    (re.compile(r"\bmy full self\b", re.I), "I"),
    (re.compile(r"\b(?:the|my) full self\b", re.I), "the full system"),
    (re.compile(r"\bI(?:'ll| will) delegate (?:this|that|it)(?: to [\w\s-]{1,30}?)?(?=[.,;!?]|$)", re.I),
     "I'll get on with it"),
    (re.compile(r"\bdelegat(?:e|ed|ing)\b", re.I), "hand"),
)
_PLUMBING = re.compile(r"\b(?:full self|delegat\w*|(?:voice|fast|slow) (?:front|worker|layer)|front[- ]end|"
                       r"the front\b(?! door| of)|(?:this|voice|text|sms|dashboard) channel)\b", re.I)
_ACK = re.compile(r"^(?:(?:very good|understood|certainly|right away|of course|as you wish|noted|got it|okay|ok|"
                  r"sure|right|will do|on it|consider it done|directly|at once|indeed|yes)"
                  r"(?:,?\s*(?:sir|madam|ma'am|blake))?[.!]?\s*)+$", re.I)


def clean_spoken(text: str) -> str:
    """One sentence as it is said: no links or markdown, no plumbing words (channel, front, full self,
    delegate), whitespace tidied. Returns "" when nothing speakable is left."""
    t = _MD.sub(r"\1", text or "")
    t = _URL.sub("", t)
    t = _MARKS.sub("", t)
    for rx, rep in _PLUMBING_SUBS:
        t = rx.sub(rep, t)
    t = " ".join(t.split()).strip()
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)
    if _PLUMBING.search(t):
        return ""
    return t if re.search(r"[A-Za-z0-9]", t) else ""


def has_plumbing(text: str) -> bool:
    return bool(_PLUMBING.search(text or ""))


def is_empty_ack(text: str) -> bool:
    """"Understood, sir." and nothing else: an acknowledgement that neither answers nor does anything."""
    t = " ".join((text or "").split())
    return bool(t) and bool(_ACK.match(t))


def trim_to_sentence(text: str, limit: int) -> str:
    """At most ``limit`` characters, cut at the last sentence end (never mid-word or mid-sentence)."""
    t = " ".join((text or "").split())
    if len(t) <= limit:
        return t
    out = ""
    for s in split_sentences(t):
        if len(out) + len(s) + 1 > limit:
            break
        out = f"{out} {s}".strip()
    if out:
        return out
    cut = t.rfind(" ", 0, limit)
    return (t[:cut] if cut > 0 else t[:limit]).rstrip(",;:") + "."


# ---------------------------------------------------------------------------
# what a tool step is called on the activity rail ("Reading home.py", "Running tests")
# ---------------------------------------------------------------------------

def _base(path: Any) -> str:
    p = str(path or "").replace("\\", "/").rstrip("/")
    return p.rsplit("/", 1)[-1] or p


def _host(url: str) -> str:
    m = re.match(r"^\w+://(?:www\.)?([^/:?#]+)", url or "")
    h = (m.group(1) if m else url or "").lower()
    named = {"youtube.com": "YouTube", "m.youtube.com": "YouTube", "studio.youtube.com": "YouTube Studio",
             "youtu.be": "YouTube", "github.com": "GitHub", "claude.ai": "Claude", "google.com": "Google",
             "mail.google.com": "Gmail", "instagram.com": "Instagram", "tiktok.com": "TikTok"}
    return named.get(h, h)


def tool_label(name: str, inp: Mapping[str, Any] | None = None) -> str:
    """A short human label for a tool call (adapted from adewaskar/jarvis ``fillers.ts`` forTool)."""
    inp = inp or {}
    raw = name or ""
    tool = raw.split("__")[-1] if raw.startswith("mcp__") else raw
    desc = " ".join(str(inp.get("description") or "").split())[:70]
    if tool == "Read":
        return f"Reading {_base(inp.get('file_path'))}" if inp.get("file_path") else "Reading a file"
    if tool in ("Edit", "MultiEdit", "NotebookEdit"):
        return f"Editing {_base(inp.get('file_path') or inp.get('notebook_path'))}" if inp else "Editing a file"
    if tool == "Write":
        return f"Writing {_base(inp.get('file_path'))}" if inp.get("file_path") else "Writing a file"
    if tool in ("Bash", "PowerShell"):
        cmd = str(inp.get("command") or "")
        if re.search(r"\bpytest\b|\bnpm (?:run )?test\b|\bunittest\b", cmd):
            return "Running tests"
        if re.search(r"^\s*git\b", cmd):
            return "Checking git"
        return desc or ("Running a command" if not cmd else f"Running {cmd.split()[0][:30]}")
    if tool in ("Grep", "Glob"):
        pat = str(inp.get("pattern") or "")[:30]
        return f"Searching the code for {pat}" if pat else "Searching the code"
    if tool == "WebSearch":
        q = str(inp.get("query") or "")[:50]
        return f"Searching the web: {q}" if q else "Searching the web"
    if tool == "WebFetch":
        return f"Reading {_host(str(inp.get('url') or ''))}" if inp.get("url") else "Reading a web page"
    if tool in ("Agent", "Task"):
        return f"Helper: {desc}" if desc else "Starting a helper"
    if tool in ("TodoWrite", "TaskCreate", "TaskUpdate"):
        return "Planning the steps"
    if tool == "Skill":
        return f"Using the {inp.get('skill') or inp.get('name') or ''} skill".replace("the  skill", "a skill")
    labels = {"open_url": lambda: f"Opening {_host(str(inp.get('url') or ''))}" if inp.get("url") else "Opening a page",
              "memory_search": lambda: "Checking my notes", "memory_read": lambda: "Reading my notes",
              "memory_remember": lambda: "Noting that down", "journal_append": lambda: "Writing in the journal",
              "settings_set": lambda: f"Changing the {str(inp.get('setting') or 'setting').replace('_', ' ')}",
              "settings_get": lambda: "Checking my settings", "studio_status": lambda: "Checking the Shorts channel",
              "ledger_summary": lambda: "Checking the books", "ledger_log_hours": lambda: "Logging your hours",
              "usage_status": lambda: "Checking plan usage", "desktop_mode_run": lambda: "Switching desktop mode",
              "start_task": lambda: "Starting a background task", "task_list": lambda: "Checking background tasks",
              "request_approval": lambda: "Asking for your approval", "message_owner": lambda: "Texting you",
              "goals_list": lambda: "Checking your goals", "heartbeat_status": lambda: "Checking the heartbeat",
              "show_map": lambda: f"Drawing a map of {str(inp.get('query'))[:40]}" if inp.get("query") else "Drawing a map",
              "show_media": lambda: "Putting a video on screen" if inp.get("kind") in ("video", "youtube") or
              inp.get("clip_id") else "Putting a picture on screen",
              "show_data": lambda: "Putting the figures on screen", "selfcheck_run": lambda: "Running the self-check",
              "ui_theme": lambda: "Changing the screen", "ui_ring": lambda: "Changing the ring",
              "ui_rails": lambda: "Changing the screen", "ui_effect": lambda: "Changing the screen",
              "ui_reset": lambda: "Resetting the screen"}
    if tool in labels:
        return labels[tool]()
    return tool.replace("_", " ").strip().capitalize() or "Working"
