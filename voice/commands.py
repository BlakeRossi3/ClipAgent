"""Words handled on the laptop, without Claude: mute, stop, never mind, start listening.

Plus the name spotter (is the assistant being addressed?) and the echo filter (is the microphone
hearing the assistant's own voice?). The name regex with common mishearings, the "words after the
name are the command" rule, and the bag-of-words echo test with override words are adapted from
adewaskar/jarvis ``src/lib/voice.ts`` (MIT License, Copyright (c) 2026 Aditya Dewaskar).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MUTE, STOP, CANCEL, START = "mute", "stop", "cancel", "start"

_PHRASES = {
    MUTE: ("stop listening", "mute", "mute yourself", "mute the mic", "mute microphone", "microphone off", "mic off",
           "go to sleep", "stop listening to me", "turn off the microphone", "turn the mic off"),
    STOP: ("stop", "quiet", "be quiet", "silence", "shut up", "enough", "that's enough", "thats enough",
           "stop talking", "hush", "okay stop", "ok stop", "wait", "hold on"),
    CANCEL: ("never mind", "nevermind", "cancel", "cancel that", "forget it", "forget about it", "scratch that",
             "forget that"),
    START: ("start listening", "unmute", "wake up", "i'm back", "im back", "listen"),
}
_LOOKUP = {p: kind for kind, phrases in _PHRASES.items() for p in phrases}
_FILLER = re.compile(r"^(?:(?:please|okay|ok|um|uh|hey|right|thanks|thank you)\s+)+|(?:\s+(?:please|now|sir|thanks|"
                     r"thank you|then|madam|ma'am))+$")

# Whisper hears the same few phrases in background noise. Dropped when they are the whole utterance.
HALLUCINATIONS = {"thank you", "thanks for watching", "you", "bye", "thank you for watching", "so", "uh", "um",
                  "subtitles by the amaraorg community", "i", "the", "oh"}

# Common transcriptions of the names it answers to. "Jarvis" is the openWakeWord word.
_NAME_ALTS = {
    "sterling": r"sterling|stirling|starling|sterlin|sterlin'|sterl[iy]ng|stelling|stealing",
    "jarvis": r"jarvis|jarvys|jervis|travis|jarv",
}


def norm(text: str) -> str:
    t = (text or "").lower().replace("’", "'")
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    return " ".join(t.split())


def name_pattern(name: str) -> re.Pattern[str]:
    alts = [re.escape(name.lower())] + [v for k, v in _NAME_ALTS.items() if k in (name.lower(), "jarvis")]
    return re.compile(rf"\b(?:(?:hey|hi|ok|okay|yo)\s+)?(?:{'|'.join(alts)})\b(?!'s)", re.IGNORECASE)


@dataclass(frozen=True)
class Addressed:
    rest: str        # what followed (or preceded) the name: usually the actual request


def addressed(text: str, name: str = "Sterling") -> Addressed | None:
    """None unless the assistant's name (or "Jarvis") is in ``text``; else the words around it."""
    n = norm(text)
    m = name_pattern(name).search(n)
    if not m:
        return None
    rest = (n[:m.start()] + " " + n[m.end():]).strip()
    return Addressed(" ".join(rest.split()))


def parse(text: str, name: str = "Sterling") -> str | None:
    """The local command ``text`` is (MUTE/STOP/CANCEL/START), or None for anything for Claude.

    Exact phrases only (after dropping the name, "please", "sir"...): "stop the upload" goes to Claude.
    """
    n = norm(text)
    m = name_pattern(name).search(n)
    if m:
        n = (n[:m.start()] + " " + n[m.end():]).strip()
    for _ in range(3):
        n = _FILLER.sub("", " ".join(n.split())).strip()
    return _LOOKUP.get(n)


def is_noise(text: str) -> bool:
    n = norm(text)
    return not n or n in HALLUCINATIONS


_OVERRIDE = re.compile(r"\b(stop|wait|sterling|jarvis|cancel|enough|quiet|hold on|shut up|never ?mind|forget it|no)\b")
_STOPWORDS = set((
    "a an the and or but so of to in on at by for with from is are was were be it its this that these those i you "
    "he she we they me him her them my your our their what which who how why when where do does did can could "
    "would should will shall not no yes if then than as about into over under out up down one two three first "
    "second third now here there just very really got get have has had say said tell okay ok well right sir").split())


def is_echo(heard: str, spoken: str) -> bool:
    """Is the microphone hearing the assistant's own voice? (Compared as bags of distinctive words.)"""
    if not spoken:
        return False
    h = norm(heard)
    if _OVERRIDE.search(h):
        return False
    words = h.split()
    if not words:
        return True
    mine = set(norm(spoken).split())
    content = [w for w in words if w not in _STOPWORDS]
    if len(content) < 2:
        return len(words) >= 2 and all(w in mine for w in words)
    return sum(w in mine for w in content) / len(content) >= 0.6


_CONTINUES = re.compile(r"\b(and|or|but|so|because|if|when|while|that|which|to|of|in|on|at|by|for|with|from|about|"
                        r"the|a|an|my|your|is|are|was|be|can|could|would|should|will|than|then|as)$")


def looks_unfinished(text: str) -> bool:
    """"Remind me to call..." trailing off on a joining word: wait a moment longer for the rest."""
    t = (text or "").strip()
    return bool(t) and (t.endswith((",", ";", ":", "-", "...")) or bool(_CONTINUES.search(norm(t))))
