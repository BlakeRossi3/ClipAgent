"""How Cecil talks while he works: a speaking-style memory the owner trains.

The cadence of what Cecil says during a request (nothing, one line, a running commentary, a start and a
result) is his judgement, not a timer. This module gives that judgement a memory and evidence:

* **The note** ``Agent/Speaking Style.md`` in the vault (shipped as ``vault_template``; the owner and
  Cecil may both edit it): principles, examples per kind of task, and a "What he has told me" list of
  short lessons scoped to a kind of task. :func:`prompt_block` puts it in the voice session's system
  prompt every session (small and stable, so it is cached).
* **Learning live**: :func:`detect_feedback` is a light local hint that what he just said is about *how*
  Cecil spoke ("too much", "keep me posted more on these", "that was perfect"); the brain then tells the
  model which request it is probably about, and the model records the lesson with the
  ``speaking_style_note`` tool (:func:`record`): one line per lesson, de-duplicated, and a newer lesson
  about the same kind of task replaces an older one that disagrees (newest wins). Each lesson keeps the
  words that prompted it.
* **Evidence**: one row per voice request (:func:`log_turn`, kv ``speaking:turns``, the newest
  :data:`KEEP_TURNS`): kind of task (inferred from the tools used), duration, how many spoken updates,
  and whether he gave style feedback right after. The Settings page shows them with a thumbs up/down
  (:func:`rate`: each thumb writes a proposed lesson for the weekly review), and the weekly
  self-improvement review tidies the note (:func:`tidy`) and sees :func:`digest`.

Importing this module touches nothing (no vault, DB or settings reads).
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any, Iterable

import db

NOTE_PATH = "Agent/Speaking Style.md"
LESSONS_HEADING = "What he has told me"
TURNS_KEY = "speaking:turns"
KEEP_TURNS = 200
PROMPT_MAX_CHARS = 3200
MAX_PER_KIND = 4                 # the note stays short: older lessons about the same kind are merged away
MAX_LESSONS = 24
LESSON_MAX_CHARS = 200

# kind of task -> the label used in the note (one line per lesson: "- Live lookups: ...")
TASK_KINDS: dict[str, str] = {
    "chat": "Conversation",
    "lookup": "Live lookups",
    "action": "Quick actions",
    "task": "Few-second work",
    "long": "Long jobs",
    "code": "Code changes",
    "general": "Generally",
}
_LABEL_TO_KIND = {v.lower(): k for k, v in TASK_KINDS.items()}

LOOKUP_TOOLS = frozenset({"WebSearch", "WebFetch", "fast_search", "web_search"})
ACTION_TOOLS = frozenset({"open_url", "open_in_explorer", "settings_set", "desktop_mode_run", "show_map", "show_media", "show_data", "show_chart",
                          "ui_theme", "ui_ring", "ui_rails", "ui_effect", "ui_reset"})
CODE_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit", "app_update"})
LONG_TOOLS = frozenset({"start_task", "Agent", "Task", "delegate"})

_lock = threading.Lock()

# A hint only: the model decides whether it really is feedback about how he was spoken to.
_FEEDBACK = re.compile(
    r"\b(too (much|chatty|many updates|wordy|talkative|quiet)|(stop|quit) (narrating|commentating|announcing|"
    r"telling me (what|every))|less (talking|chatter|narration)|talk(ing)? less|say less|fewer updates|"
    r"keep me (posted|updated|informed|in the loop)|more updates|update me (more|as you go)|"
    r"(give|keep) me (a )?running commentary|as you (go|find)|"
    r"(didn'?t|did not|never) (tell|say|let me know)|you didn'?t say (you|that)|"
    r"(that|this) was (perfect|spot on|just right|exactly right|too much|a lot)|just right|"
    r"no need to (say|tell|announce|confirm|acknowledge)|(don'?t|do not) (narrate|announce|tell me every|"
    r"acknowledge)|just do it|stop talking (while|when) you)\b", re.I)

_MORE = re.compile(r"\b(more|keep me (posted|updated|informed|in the loop)|update me|updates as|running commentary|"
                   r"as (you go|they arrive|you find)|tell me (when|that) you|let me know|every few seconds|often|"
                   r"likes? (updates|commentary|hearing))\b", re.I)
_LESS = re.compile(r"\b(less|fewer|only|quiet|quieter|stop|don'?t|do not|no need|too much|too many|just do|"
                   r"silent|silently|sparse|sparsely|rarely|nothing (until|but)|no (updates|narration|commentary))\b",
                   re.I)
_STOP = frozenset("a an the and or but so of to in on at by for with from is are was were be it this that he him "
                  "his me my i you your when what about as do does just".split())


# ---------------------------------------------------------------------------
# the note
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Lesson:
    kind: str
    text: str
    day: str = ""
    after: str = ""

    def line(self) -> str:
        tail = []
        if self.day:
            tail.append(self.day)
        if self.after:
            tail.append(f'after "{self.after}"')
        return f"- {TASK_KINDS.get(self.kind, TASK_KINDS['general'])}: {self.text}" + \
            (f" _({'; '.join(tail)})_" if tail else "")


_LINE = re.compile(r"^-\s+(?P<label>[A-Za-z][A-Za-z -]{1,30}):\s+(?P<text>.+?)"
                   r"(?:\s+_\((?P<meta>[^)]*)\)_)?\s*$")


def parse_lessons(body: str) -> list[Lesson]:
    """The lessons under "What he has told me", in file order (oldest first). Unknown labels -> general."""
    out = []
    for line in _section(body).splitlines():
        m = _LINE.match(line.strip())
        if not m:
            continue
        meta = m.group("meta") or ""
        day = next((p.strip() for p in meta.split(";") if re.fullmatch(r"\s*\d{4}-\d\d-\d\d\s*", p)), "")
        after = ""
        am = re.search(r'after "([^"]*)"', meta)
        if am:
            after = am.group(1)
        out.append(Lesson(_LABEL_TO_KIND.get(m.group("label").strip().lower(), "general"), m.group("text").strip(),
                          day, after))
    return out


def _section(body: str) -> str:
    m = re.search(rf"^##\s+{re.escape(LESSONS_HEADING)}\s*$", body or "", re.M)
    if not m:
        return ""
    rest = body[m.end():]
    nxt = re.search(r"^##\s", rest, re.M)
    return rest[:nxt.start()] if nxt else rest


def _with_section(body: str, lessons: list[Lesson]) -> str:
    lines = "\n".join(x.line() for x in lessons)
    block = f"## {LESSONS_HEADING}\n" + (lines + "\n" if lines else "")
    m = re.search(rf"^##\s+{re.escape(LESSONS_HEADING)}\s*$", body or "", re.M)
    if not m:
        return (body or "").rstrip("\n") + "\n\n" + block
    rest = body[m.end():]
    nxt = re.search(r"^##\s", rest, re.M)
    after = rest[nxt.start():] if nxt else ""
    return body[:m.start()] + block + ("\n" + after if after else "")


def _vault():
    from assistant import vault

    vault.ensure_vault()
    return vault


def read_note() -> tuple[str, str | None]:
    """(body, sha) of the note, created from the template when missing. ("", None) if the vault fails."""
    try:
        v = _vault()
        if not v.note_exists(NOTE_PATH):
            v.write_note(NOTE_PATH, _default_body(), meta={"type": "agent", "tags": ["agent", "voice"]},
                         source="speaking_style", note_type="agent")
        n = v.read_note(NOTE_PATH)
        return n.body, n.sha
    except Exception as exc:
        db.log_event("assistant", "speaking style note unreadable", level="warning", data={"error": repr(exc)[:200]})
        return "", None


def _default_body() -> str:
    from assistant import vault

    src = vault.TEMPLATE_DIR / NOTE_PATH
    if src.is_file():
        return vault.parse_note_text(src.read_text(encoding="utf-8"))[1]
    return f"# Speaking Style\n\n## {LESSONS_HEADING}\n"


def prompt_block(max_chars: int = PROMPT_MAX_CHARS) -> str:
    """The note for the voice session's system prompt (stable within a session, so it is cached)."""
    from assistant.persona import escape_untrusted

    body, _ = read_note()
    body = re.sub(r"\n{3,}", "\n\n", (body or "").strip())
    if not body:
        return ""
    if len(body) > max_chars:      # keep the lessons (the end) whole; trim the examples above them
        sec = _section(body)
        head = body[: max(0, max_chars - len(sec) - 40)].rsplit("\n", 1)[0]
        body = head + f"\n...\n## {LESSONS_HEADING}\n" + sec.strip()
    return ("# Your speaking style (your own note, Agent/Speaking Style.md; he trains it, you may edit it)\n"
            "Follow it, the lessons at the end first: they are what he has told you about how much to say "
            "for each kind of task.\n\n" + escape_untrusted(body))


# ---------------------------------------------------------------------------
# lessons: record, merge, tidy
# ---------------------------------------------------------------------------

def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z']+", (s or "").lower()) if w not in _STOP and len(w) > 2}


def polarity(text: str) -> int:
    """+1 wants more said, -1 wants less, 0 neither (or both equally)."""
    more, less = len(_MORE.findall(text or "")), len(_LESS.findall(text or ""))
    return (more > less) - (less > more)


def _same_point(a: Lesson, b: Lesson) -> bool:
    if a.kind != b.kind:
        return False
    pa, pb = polarity(a.text), polarity(b.text)
    if pa and pb and pa != pb:
        return True                                  # contradiction: the newer one wins
    wa, wb = _words(a.text), _words(b.text)
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.5   # the same lesson said again


def merge(lessons: Iterable[Lesson]) -> list[Lesson]:
    """Oldest first in, oldest first out: duplicates and contradicted older lessons removed (newest wins),
    at most :data:`MAX_PER_KIND` per kind and :data:`MAX_LESSONS` in all."""
    kept: list[Lesson] = []
    for new in lessons:
        kept = [old for old in kept if not _same_point(old, new)]
        kept.append(new)
    out: list[Lesson] = []
    for x in reversed(kept):
        if sum(1 for y in out if y.kind == x.kind) < MAX_PER_KIND and len(out) < MAX_LESSONS:
            out.append(x)
    return list(reversed(out))


def _clean(s: str, n: int) -> str:
    return " ".join(str(s or "").replace('"', "'").replace("_(", "(").split())[:n].strip()


def record(task_kind: str, lesson: str, *, prompted_by: str = "", source: str = "assistant") -> dict[str, Any]:
    """Add a lesson to the note (de-duplicated; newest wins over an older one that disagrees)."""
    kind = task_kind if task_kind in TASK_KINDS else "general"
    text = _clean(lesson, LESSON_MAX_CHARS).rstrip(".") + "."
    if len(text) < 4:
        raise ValueError("the lesson is empty")
    with _lock:
        v = _vault()
        body, sha = read_note()
        before = parse_lessons(body)
        new = Lesson(kind, text, db.local_today().isoformat(), _clean(prompted_by, 120))
        after = merge([*before, new])
        dropped = [x.text for x in before if x not in after]
        v.write_note(NOTE_PATH, _with_section(body, after), base_sha=sha, on_conflict="raise",
                     source=source, note_type="agent")
    db.log_event("assistant", "speaking style lesson recorded",
                 data={"kind": kind, "lesson": text, "replaced": dropped[:4]})
    return {"kind": kind, "lesson": text, "replaced": dropped, "lessons": len(after)}


def tidy() -> dict[str, Any]:
    """Weekly: merge duplicates and contradictions across the whole list and cap it (newest wins)."""
    with _lock:
        body, sha = read_note()
        if sha is None:
            return {"before": 0, "after": 0}
        before = parse_lessons(body)
        after = merge(before)
        if after != before:
            _vault().write_note(NOTE_PATH, _with_section(body, after), base_sha=sha, on_conflict="raise",
                                source="improve", note_type="agent")
    return {"before": len(before), "after": len(after)}


def lessons() -> list[Lesson]:
    return parse_lessons(read_note()[0])


# ---------------------------------------------------------------------------
# feedback and evidence
# ---------------------------------------------------------------------------

def detect_feedback(text: str) -> bool:
    """A light hint that he is commenting on how Cecil spoke (the model makes the real call)."""
    return bool(_FEEDBACK.search(text or ""))


def infer_kind(heard: str, tools: Iterable[str], *, task_started: bool = False, duration_ms: float | None = None) -> str:
    names = {str(t).split("__")[-1] for t in tools}
    if task_started or names & LONG_TOOLS:
        return "long"
    if names & CODE_TOOLS:
        return "code"
    if names & LOOKUP_TOOLS:
        return "lookup"
    if names & ACTION_TOOLS and len(names - ACTION_TOOLS) <= 1:
        return "action"
    if names:
        return "long" if duration_ms is not None and duration_ms > 90_000 else "task"
    return "chat"


def _turns() -> list[dict[str, Any]]:
    rows = db.kv_get(TURNS_KEY, []) or []
    return rows if isinstance(rows, list) else []


def log_turn(turn_id: str, heard: str, *, kind: str, duration_ms: float, updates: int, sentences: int,
             tools: Iterable[str] = (), origin: str = "") -> None:
    """One voice request's evidence (never fatal)."""
    try:
        with _lock:
            row = {"turn": turn_id, "ts": db.now_iso(), "heard": _clean(heard, 160), "kind": kind,
                   "duration_ms": int(duration_ms), "updates": int(updates), "sentences": int(sentences),
                   "tools": sorted({str(t).split("__")[-1] for t in tools})[:12], "origin": origin,
                   "feedback": "", "rating": None}
            db.kv_update(TURNS_KEY, lambda cur: ([r for r in (cur if isinstance(cur, list) else [])
                                                  if r.get("turn") != turn_id] + [row])[-KEEP_TURNS:], [])
    except Exception as exc:
        db.log_event("assistant", "speaking style turn log failed", level="warning", data={"error": repr(exc)[:200]})


def recent_turns(limit: int = 10) -> list[dict[str, Any]]:
    """Newest first."""
    return list(reversed(_turns()))[:limit]


def get_turn(turn_id: str) -> dict[str, Any] | None:
    return next((r for r in _turns() if r.get("turn") == turn_id), None)


def last_turn() -> dict[str, Any] | None:
    rows = _turns()
    return rows[-1] if rows else None


def mark_feedback(turn_id: str, text: str) -> bool:
    with _lock:
        rows = _turns()
        hit = next((r for r in rows if r.get("turn") == turn_id), None)
        if hit is None:
            return False
        hit["feedback"] = _clean(text, 200)
        db.kv_set(TURNS_KEY, rows)
    return True


def describe(row: dict[str, Any]) -> str:
    secs = round((row.get("duration_ms") or 0) / 1000)
    return (f"\"{row.get('heard', '')}\" ({TASK_KINDS.get(row.get('kind', ''), 'task').lower()}, "
            f"{row.get('updates', 0)} spoken update{'' if row.get('updates') == 1 else 's'} over {secs} s)")


def feedback_note(prev: dict[str, Any]) -> str:
    """The system note the brain adds when he seems to be commenting on how Cecil spoke."""
    return ("(System note, not from him: what he says next may be feedback on how you spoke during his previous "
            f"request, {describe(prev)}, kind of task: {prev.get('kind', 'general')}. If it is, change how you speak "
            "from now on in this conversation and record it in a few words with speaking_style_note "
            "(task_kind, lesson, prompted_by = his words), then acknowledge in one short sentence. If it is not, "
            "ignore this note.)")


def rate(turn_id: str, up: bool) -> int | None:
    """A thumb on a recent request's narration: stored on the row and written as a proposed lesson."""
    from assistant import store

    with _lock:
        rows = _turns()
        hit = next((r for r in rows if r.get("turn") == turn_id), None)
        if hit is None:
            return None
        hit["rating"] = 1 if up else -1
        db.kv_set(TURNS_KEY, rows)
    label = TASK_KINDS.get(hit.get("kind", ""), "Generally")
    n = hit.get("updates", 0)
    if up:
        text = f"{label}: {n} spoken update{'' if n == 1 else 's'} for a request like this was right."
    elif n >= 2:
        text = f"{label}: {n} spoken updates was too many for a request like this; say less."
    else:
        text = f"{label}: the narration for a request like this was wrong ({n} spoken updates); ask or adjust."
    return store.add_lesson("voice", text, evidence=f"thumb {'up' if up else 'down'} on {describe(hit)}")


def digest(limit: int = 60) -> dict[str, Any]:
    """For the weekly review: per kind of task, how much he was told, and what he said about it."""
    rows = _turns()[-limit:]
    per: dict[str, dict[str, Any]] = {}
    for r in rows:
        k = per.setdefault(r.get("kind", "general"), {"turns": 0, "updates": 0, "up": 0, "down": 0, "feedback": []})
        k["turns"] += 1
        k["updates"] += int(r.get("updates") or 0)
        k["up"] += r.get("rating") == 1
        k["down"] += r.get("rating") == -1
        if r.get("feedback"):
            k["feedback"].append(r["feedback"][:120])
    return {"kinds": per, "lessons": [x.line() for x in lessons()]}


# ---------------------------------------------------------------------------
# the tool
# ---------------------------------------------------------------------------

def _note_tool(ctx: Any, a: dict[str, Any]) -> Any:
    from assistant.tools import ToolError

    kind = str(a.get("task_kind") or "general")
    if kind not in TASK_KINDS:
        raise ToolError(f"task_kind must be one of {', '.join(TASK_KINDS)}")
    try:
        res = record(kind, str(a.get("lesson") or ""), prompted_by=str(a.get("prompted_by") or ""),
                     source=f"assistant:{getattr(ctx, 'channel', '')}")
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    prev = last_turn()
    if prev and a.get("prompted_by"):
        mark_feedback(prev["turn"], str(a.get("prompted_by")))
    return res


def register_tools(register: Any) -> None:
    from assistant.tools import Tool, _obj

    register(Tool(
        "speaking_style_note",
        "Record how he wants you to speak while you work, for one kind of task, in your Speaking Style note "
        "(read every session). Use it when he reacts to how you spoke ('too much', 'keep me posted on these', "
        "'that was perfect', 'you didn't tell me you'd started'). One short lesson, e.g. 'likes a running "
        "commentary' or 'only the start and the result'. A newer lesson replaces an older one that disagrees.",
        _obj({"task_kind": {"type": "string", "enum": list(TASK_KINDS),
                            "description": "chat, lookup (news, scores, live facts), action (open a page, switch "
                                           "voice), task (a few seconds of work), long (minutes, background), "
                                           "code (changing his apps), general."},
              "lesson": {"type": "string", "description": "A few words, e.g. 'likes a running commentary'."},
              "prompted_by": {"type": "string", "description": "His words that prompted it."}},
             required=["task_kind", "lesson"]),
        _note_tool, owner="assistant.speaking_style", side_effects=False), replace=True)
