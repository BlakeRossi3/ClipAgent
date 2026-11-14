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
from typing import Callable

# Said only when the fast voice front has not started answering within VOICE_FILLER_DELAY_SEC.
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
