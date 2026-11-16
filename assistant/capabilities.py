"""Before Cecil builds a new ability, look for one that already exists - and remember every attempt.

The order (skills/toolsmith/SKILL.md): 1. a tool or skill he already has -> use it; 2. a skill from a
trusted source (anthropics/skills, obra/superpowers, SKILLS_TRUSTED_REPOS) -> request the vetted install;
3. only then build it (self-edit, tested). Every outcome is recorded (kv ``capabilities:log`` and the
readable vault note ``Agent/Capabilities.md``) so the same gap is never rebuilt blindly: a need that
failed twice is flagged "ask him before trying again".

Tools: ``capability_check(need)``, ``capability_record(need, outcome, how)``.
"""

from __future__ import annotations

import re
from typing import Any, Callable

import db

KV_LOG = "capabilities:log"
NOTE = "Agent/Capabilities.md"
OUTCOMES = ("used_existing", "installed_skill", "built", "failed", "gave_up")
MAX_LOG = 300
_STOP = {"the", "a", "an", "to", "and", "or", "of", "for", "in", "on", "my", "me", "i", "it", "be", "can", "with",
         "his", "her", "this", "that", "from", "into", "is", "are", "do", "does", "want", "need", "able", "some"}


def _words(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z0-9]{3,}", (text or "").lower()):
        if w in _STOP:
            continue
        out.add(w[:-1] if len(w) > 4 and w.endswith("s") else w)
    return out


def _score(need: set[str], text: str) -> float:
    have = _words(text)
    return len(need & have) / max(1, len(need)) if need else 0.0


def log() -> list[dict[str, Any]]:
    v = db.kv_get(KV_LOG, [])
    return v if isinstance(v, list) else []


def check(need: str, *, catalog: bool = True) -> dict[str, Any]:
    """What already covers ``need``: tools, installed skills, trusted-source skills, past attempts."""
    from assistant import registry, tools

    words = _words(need)
    tools.load_extensions()
    have_tools = sorted(((_score(words, f"{t.name} {t.description}"), t.name, t.description[:160])
                         for t in tools.REGISTRY.values()), reverse=True)
    skills = registry.load_skills(strict=False)
    have_skills = sorted(((_score(words, f"{s.name} {s.description}"), s.name, s.description[:160])
                          for s in skills.values()), reverse=True)
    out: dict[str, Any] = {
        "need": need,
        "tools": [{"name": n, "about": d, "match": round(sc, 2)} for sc, n, d in have_tools[:5] if sc >= 0.34],
        "skills": [{"name": n, "about": d, "match": round(sc, 2)} for sc, n, d in have_skills[:4] if sc >= 0.34],
        "catalog": [], "past_attempts": []}
    if catalog:
        try:
            from assistant import skill_catalog

            rows = sorted(((_score(words, f"{r.get('name')} {r.get('title', '')} {r.get('description', '')}"), r)
                           for r in skill_catalog.search("", limit=5000)), key=lambda x: -x[0])
            out["catalog"] = [{"name": r.get("name"), "repo": r.get("repo"), "about": str(r.get("description"))[:160],
                               "match": round(sc, 2)} for sc, r in rows[:5] if sc >= 0.34]
        except Exception as exc:   # offline / GitHub rate limit: the rest still answers
            out["catalog_error"] = f"{type(exc).__name__}"
    past = [e for e in log() if _score(words, e.get("need", "")) >= 0.5]
    out["past_attempts"] = past[-5:]
    failures = sum(1 for e in past if e.get("outcome") in ("failed", "gave_up"))
    if out["tools"] or out["skills"]:
        step = "use_existing"
        why = "Something you already have looks like it covers this: try it first."
    elif out["catalog"]:
        step = "install_skill"
        why = ("A skill from a trusted source may cover it: read its description, then skill_request_install "
               "(vetted, he approves). Build only if none fits.")
    else:
        step = "build"
        why = ("Nothing covers it: build the smallest tool or skill (toolsmith skill), test-first "
               "(test-driven-development), then capability_record the outcome.")
    if failures >= 2:
        step, why = "ask_first", (f"This was tried {failures} times before and did not work (see past_attempts). "
                                  "Do not rebuild it blindly: tell him what failed and ask how to proceed.")
    out["next_step"], out["why"] = step, why
    return out


def record(need: str, outcome: str, how: str = "", *, notes: str = "") -> dict[str, Any]:
    """Remember how a capability gap ended (for the next check and for his weekly review)."""
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}")
    need = " ".join((need or "").split())[:200]
    if not need:
        raise ValueError("describe the need")
    entry = {"at": db.now_iso(), "need": need, "outcome": outcome, "how": " ".join(how.split())[:300],
             "notes": " ".join(notes.split())[:300]}
    entries = (log() + [entry])[-MAX_LOG:]
    db.kv_set(KV_LOG, entries)
    try:
        from assistant import vault

        vault.ensure_vault()
        line = f"- {entry['at'][:10]} **{outcome.replace('_', ' ')}**: {need}" + (f" — {entry['how']}" if entry["how"] else "")
        vault.append_to_note(NOTE, line, heading="Log", source="capabilities", note_type="lesson")
    except Exception:
        pass
    db.log_event("assistant", f"capability {outcome}: {need[:80]}", data=entry)
    return entry


def register_tools(register: Callable[..., None]) -> None:
    from assistant.tools import Tool, ToolError, _obj

    def _check(ctx: Any, a: dict[str, Any]) -> Any:
        return check(str(a.get("need") or ""))

    def _record(ctx: Any, a: dict[str, Any]) -> Any:
        try:
            return record(str(a.get("need") or ""), str(a.get("outcome") or ""), str(a.get("how") or ""),
                          notes=str(a.get("notes") or ""))
        except ValueError as exc:
            raise ToolError(str(exc)) from None

    register(Tool("capability_check", "BEFORE building any new ability: what already covers this need (my tools, "
                  "installed skills, skills from trusted sources) and what was tried before. Returns next_step: "
                  "use_existing | install_skill | build | ask_first.",
                  _obj({"need": {"type": "string"}}, ["need"]), _check, owner="assistant.capabilities"), replace=True)
    register(Tool("capability_record", "After a capability gap is handled: record the outcome (used_existing, "
                  "installed_skill, built, failed, gave_up) and how, so it is never rebuilt blindly.",
                  _obj({"need": {"type": "string"}, "outcome": {"type": "string", "enum": list(OUTCOMES)},
                        "how": {"type": "string"}, "notes": {"type": "string"}}, ["need", "outcome"]),
                  _record, owner="assistant.capabilities", side_effects=True), replace=True)
