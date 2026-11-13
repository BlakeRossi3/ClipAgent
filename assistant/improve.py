"""Self-improvement: observe -> reflect -> GATE -> owner approval -> apply (or roll back).

There is no fine-tuning for these models, so the assistant improves through files it can be
shown to have improved: skills (``skills/**``), agent prompts (``agents/**``) and the vault
routine note. Every change is a unified diff that must pass a plain-code gate, and nothing is
applied until the owner approves it.

* **Observe** (:func:`observe`): plain-code digest of the week - eval scores/regressions,
  owner corrections and praise (keyword heuristics), check-ins answered vs ignored, tool
  errors, budget cut-offs, expensive runs, sub-agent usage.
* **Reflect** (:func:`weekly_improvement`): one ``engineer`` run that records lessons
  (``lessons_propose``) and submits at most :data:`MAX_PER_WEEK` diffs (``improvement_propose``).
  In fake mode a deterministic stub submits one well-formed example instead.
* **Gate** (:func:`gate`): see :data:`GATE_RULES`. A proposal failing any rule is stored as
  ``rejected_by_gate`` with the reasons and is never offered for approval.
* **Approve**: passing proposals become a phase-1 decision (``assistant_action`` /
  handler ``improvement``). On approval :func:`apply_proposal` re-runs the gate against the
  current tree, writes the file, re-validates the registry + offline evals, and rolls back on
  any failure. It never commits: the edit stays in the working tree and a commit message file
  is written to the (git-ignored) data dir. :func:`revert` restores the previous content.

Privacy: proposals, evidence and lessons are derived from real conversations, so they are
stored only in the DB (kv ``improve:*``, ``as_lessons``) and the private vault
(``Agent/Proposals/``, ``Agent/Lessons/``). The only tracked files ever written are the
approved skill/agent edits, whose added lines are screened for personal data by the gate.

CLI: ``python -m assistant.improve observe | run | list | show ID | approve ID | reject ID | revert ID``.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import shutil
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import db
from assistant import evals, registry, store

MAX_PER_WEEK = 3
MAX_CHANGED_LINES = 40
MAX_DIFF_CHARS = 6000
MAX_FILE_CHARS = 16000
ROUTINE_TARGET = "vault:Agent/Routine.md"
ALLOWED_RE = re.compile(r"^(skills/[a-z][a-z0-9-]*/[A-Za-z0-9_-]+\.md|agents/[a-z][a-z0-9-]*\.md)$")
FORBIDDEN = ("skills/persona/examples.md", "skills/self-improvement/", "agents/engineer.md")
# Sections whose lines may not be removed or reworded (additions are fine).
PROTECTED_SECTIONS = {"skills/persona/SKILL.md": ("## Never",)}
# Removed lines mentioning these are safety/approval rules: never weakened by a proposal.
SAFETY_RE = re.compile(r"approv|untrusted|instruction|never follow|only the owner|third[- ]part|secret|password|"
                       r"budget|spend|allowlist|medical|doctor|emergency|irreversible|privacy|private|discreet", re.I)
AGENT_FROZEN_KEYS = ("name", "model", "tools")
PII_RES = (re.compile(r"\+?\d[\d\s().-]{8,}\d"), re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
           re.compile(r"https?://|www\.", re.I), re.compile(r"_(stated|inferred),"))
GATE_RULES = (
    "path: one file; skills/<name>/*.md or agents/<name>.md or the vault routine note; no '..', absolute, "
    "hidden or symlinked paths",
    "forbidden: skills/persona/examples.md (eval data), skills/self-improvement/**, agents/engineer.md (the gate's "
    "own instructions), and anything outside skills/ and agents/ (code, config, budgets)",
    "agent frontmatter: name, model and tools (the allowlist) may not change",
    "protected: no removed/reworded line in the persona's '## Never' section or mentioning approval, "
    "untrusted input, secrets, budgets, privacy, medical or third parties",
    f"size: at most {MAX_CHANGED_LINES} changed lines, {MAX_DIFF_CHARS} diff chars, {MAX_FILE_CHARS} chars result",
    "privacy: added lines carry no phone numbers, emails, URLs, memory provenance markers, the owner's name "
    "or text copied from his recent messages",
    "applies: the diff applies exactly to the current file",
    "registry: all skills and agents still validate",
    "evals: the offline suite on a patched copy has no new failures versus the unpatched tree",
)


class ImproveError(Exception):
    pass


# ---------------------------------------------------------------------------
# where the definitions live (tests point this at a temp copy)
# ---------------------------------------------------------------------------

DEFS_ROOT: Path | None = None       # None = the repo


def defs_root() -> Path:
    return DEFS_ROOT or registry.REPO_ROOT


def _target_file(path: str, root: Path | None = None) -> Path:
    if path == ROUTINE_TARGET:
        from assistant import vault
        return vault.safe_path(path.split(":", 1)[1])[0]
    return (root or defs_root()) / path


# ---------------------------------------------------------------------------
# unified diff
# ---------------------------------------------------------------------------

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Patch:
    old_path: str
    new_path: str
    hunks: list[tuple[int, list[str]]]      # (old start line 1-based, lines with ' ', '-', '+' prefixes)

    def changed(self) -> tuple[list[str], list[str]]:
        rem = [ln[1:] for _, h in self.hunks for ln in h if ln.startswith("-")]
        add = [ln[1:] for _, h in self.hunks for ln in h if ln.startswith("+")]
        return rem, add


def _strip_prefix(p: str) -> str:
    p = p.split("\t", 1)[0].strip()
    return p[2:] if p.startswith(("a/", "b/")) else p


def parse_diff(diff: str) -> Patch:
    lines = diff.replace("\r\n", "\n").split("\n")
    heads = [i for i, ln in enumerate(lines) if ln.startswith("+++ ")]
    if len(heads) != 1 or heads[0] == 0 or not lines[heads[0] - 1].startswith("--- "):
        raise ImproveError("the diff must change exactly one file (one ---/+++ header pair)")
    old, new = _strip_prefix(lines[heads[0] - 1][4:]), _strip_prefix(lines[heads[0]][4:])
    hunks: list[tuple[int, list[str]]] = []
    cur: list[str] | None = None
    for ln in lines[heads[0] + 1:]:
        m = _HUNK.match(ln)
        if m:
            cur = []
            hunks.append((int(m.group(1)), cur))
        elif cur is not None and ln[:1] in (" ", "-", "+"):
            cur.append(ln)
        elif cur is not None and ln == "":
            cur.append(" ")          # editors strip the space of blank context lines
        elif ln.startswith("\\"):
            continue
    while hunks and hunks[-1][1] and hunks[-1][1][-1] == " ":
        hunks[-1][1].pop()           # trailing blank produced by split
    if not hunks:
        raise ImproveError("the diff has no hunks")
    return Patch(old, new, hunks)


def apply_patch(text: str | None, patch: Patch) -> str:
    """Apply strictly: every hunk's context and removed lines must match exactly."""
    src = [] if text is None else text.split("\n")
    out: list[str] = []
    pos = 0
    for start, h in patch.hunks:
        old_block = [ln[1:] for ln in h if ln[:1] in (" ", "-")]
        new_block = [ln[1:] for ln in h if ln[:1] in (" ", "+")]
        want = max(0, start - 1)
        cands = [i for i in range(pos, len(src) - len(old_block) + 1) if src[i:i + len(old_block)] == old_block]
        if not old_block:
            cands = [min(want, len(src))]
        if not cands:
            raise ImproveError(f"hunk at line {start} does not apply to the current file")
        at = min(cands, key=lambda i: abs(i - want))
        out.extend(src[pos:at])
        out.extend(new_block)
        pos = at + len(old_block)
    out.extend(src[pos:])
    return "\n".join(out)


def make_diff(path: str, before: str, after: str) -> str:
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                        f"a/{path}", f"b/{path}"))


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------

def check_path(path: str, root: Path | None = None) -> list[str]:
    if path == ROUTINE_TARGET:
        return []
    errs = []
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path or ":" in path:
        return [f"path {path!r}: must be a relative repo path"]
    parts = path.split("/")
    if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
        return [f"path {path!r}: no empty, '.', '..' or hidden segments"]
    if not ALLOWED_RE.match(path):
        errs.append(f"path {path!r}: only skills/<name>/*.md, agents/<name>.md or {ROUTINE_TARGET}")
    if any(path == f or (f.endswith("/") and path.startswith(f)) for f in FORBIDDEN):
        errs.append(f"path {path!r}: forbidden (eval data or the self-improvement gate's own instructions)")
    base = root or defs_root()
    p = base
    for part in parts:
        p = p / part
        if p.is_symlink():
            errs.append(f"path {path!r}: symlinks are refused")
            break
    return errs


def _section_of(lines: list[str], idx: int) -> str:
    for i in range(min(idx, len(lines) - 1), -1, -1):
        if lines[i].startswith("## "):
            return lines[i].strip()
    return ""


def _removed_with_index(text: str, patch: Patch) -> list[tuple[int, str]]:
    """(0-based original index, line) of each removed line (located like apply_patch does)."""
    src, out, pos = text.split("\n"), [], 0
    for start, h in patch.hunks:
        old_block = [ln[1:] for ln in h if ln[:1] in (" ", "-")]
        cands = [i for i in range(pos, len(src) - len(old_block) + 1) if src[i:i + len(old_block)] == old_block]
        if not cands:
            return out
        at = min(cands, key=lambda i: abs(i - (start - 1)))
        k = at
        for ln in h:
            if ln[:1] == "-":
                out.append((k, ln[1:]))
            if ln[:1] in (" ", "-"):
                k += 1
        pos = at + len(old_block)
    return out


def _owner_texts(days: int = 90) -> list[str]:
    since = db.to_iso(db.utcnow() - timedelta(days=days))
    try:
        return [m.body for m in store.list_messages(500, direction="in", since=since)]
    except Exception:
        return []


def _privacy_problems(added: list[str]) -> list[str]:
    import config
    errs = []
    blob = "\n".join(added)
    for rx in PII_RES:
        m = rx.search(blob)
        if m:
            errs.append(f"privacy: added text contains {m.group(0)!r} (personal data stays in the vault)")
    name = (config.get_settings().owner_name or "").strip()
    if name and re.search(rf"\b{re.escape(name)}\b", blob, re.I):
        errs.append("privacy: added text names the owner (skills are public; use {{owner_name}})")
    low = re.sub(r"\s+", " ", blob.lower())
    for t in _owner_texts():
        t = re.sub(r"\s+", " ", t.lower()).strip()
        if len(t) >= 40 and t[:40] in low:
            errs.append("privacy: added text quotes one of his messages")
            break
    return errs


_baseline_cache: dict[str, set[str]] = {}


def _tree_key(root: Path) -> str:
    h = hashlib.sha256()
    for d in ("skills", "agents"):
        for f in sorted((root / d).rglob("*.md")):
            h.update(str(f.relative_to(root)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def _failing_ids(root: Path) -> set[str]:
    rep = evals.run_evals_isolated(defs_root=root, store_result=False)
    return set(rep.failed_ids)


def _copy_defs(src: Path, dst: Path) -> None:
    for d in ("skills", "agents"):
        shutil.copytree(src / d, dst / d, symlinks=False, ignore=shutil.ignore_patterns("__pycache__"))


def _registry_errors(root: Path) -> list[str]:
    from assistant import tools
    errs: list[str] = []
    try:
        skills = registry.load_skills(root / "skills")
        registry.load_agents(root / "agents", skills=skills, known_tools=tools.known_tool_names())
    except registry.RegistryError as exc:
        errs.extend(exc.errors)
    return errs


@dataclass
class GateResult:
    ok: bool
    reasons: list[str]
    before: str | None = None
    after: str | None = None


def gate(path: str, diff: str, *, run_evals: bool = True) -> GateResult:
    """Every rule in :data:`GATE_RULES`, in plain code. Cheap rules first; evals last."""
    reasons = check_path(path)
    if reasons:
        return GateResult(False, reasons)
    if len(diff) > MAX_DIFF_CHARS:
        return GateResult(False, [f"size: diff is {len(diff)} chars > {MAX_DIFF_CHARS}"])
    try:
        patch = parse_diff(diff)
    except ImproveError as exc:
        return GateResult(False, [str(exc)])
    if patch.new_path != (path.split(":", 1)[1] if path == ROUTINE_TARGET else path) or (
            patch.old_path not in ("/dev/null", patch.new_path)):
        return GateResult(False, [f"diff headers ({patch.old_path} -> {patch.new_path}) do not match {path!r}"])
    target = _target_file(path)
    before = target.read_text(encoding="utf-8") if target.is_file() else None
    if before is None and not (patch.old_path == "/dev/null" and path.startswith("skills/")
                               and path.endswith("/SKILL.md")):
        return GateResult(False, [f"{path!r} does not exist (only a new skills/<name>/SKILL.md may be created)"])
    removed, added = patch.changed()
    if len(removed) + len(added) > MAX_CHANGED_LINES:
        reasons.append(f"size: {len(removed) + len(added)} changed lines > {MAX_CHANGED_LINES}")
    try:
        after = apply_patch(before, patch)
    except ImproveError as exc:
        return GateResult(False, reasons + [str(exc)], before)
    if len(after) > MAX_FILE_CHARS:
        reasons.append(f"size: result is {len(after)} chars > {MAX_FILE_CHARS}")
    if before is not None:
        blines = before.split("\n")
        for idx, line in _removed_with_index(before, patch):
            if _section_of(blines, idx) in PROTECTED_SECTIONS.get(path, ()):
                reasons.append(f"protected: removes or rewords a hard rule: {line.strip()[:80]!r}")
            elif SAFETY_RE.search(line):
                reasons.append(f"protected: removes or rewords a safety line: {line.strip()[:80]!r}")
    if path.startswith("agents/") and before is not None:
        try:
            m0, _ = registry.parse_frontmatter(before)
            m1, _ = registry.parse_frontmatter(after)
            changed = [k for k in AGENT_FROZEN_KEYS if m0.get(k) != m1.get(k)]
            if changed:
                reasons.append(f"agent frontmatter {changed} may not change (tool allowlist / model)")
        except ValueError as exc:
            reasons.append(f"frontmatter: {exc}")
    reasons.extend(_privacy_problems(added))
    if reasons or path == ROUTINE_TARGET:
        return GateResult(not reasons, reasons, before, after)
    with tempfile.TemporaryDirectory(prefix="assistant-gate-") as tmp:
        root = Path(tmp)
        _copy_defs(defs_root(), root)
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(after, encoding="utf-8")
        reg = _registry_errors(root)
        if reg:
            return GateResult(False, [f"registry: {e}" for e in reg[:5]], before, after)
        if run_evals:
            key = _tree_key(defs_root())
            if key not in _baseline_cache:
                _baseline_cache[key] = _failing_ids(defs_root())
            new_fail = sorted(_failing_ids(root) - _baseline_cache[key])
            if new_fail:
                return GateResult(False, [f"evals: the change breaks {new_fail}"], before, after)
    return GateResult(True, [], before, after)


# ---------------------------------------------------------------------------
# proposals (kv in the DB + a private vault note)
# ---------------------------------------------------------------------------

STATUSES = ("rejected_by_gate", "pending", "applied", "rejected", "failed", "reverted")


def _key(pid: int) -> str:
    return f"improve:proposal:{pid}"


def get_proposal(pid: int) -> dict[str, Any] | None:
    return db.kv_get(_key(pid))


def _save(p: dict[str, Any]) -> None:
    p["updated_at"] = db.now_iso()
    db.kv_set(_key(p["id"]), p)


def list_proposals(status: str | None = None) -> list[dict[str, Any]]:
    ids = db.kv_get("improve:proposal_ids", []) or []
    out = [get_proposal(i) for i in reversed(ids)]
    return [p for p in out if p and (status is None or p["status"] == status)]


def _week_count() -> int:
    week = store.week_start_of().isoformat()
    return sum(1 for p in list_proposals() if p.get("week") == week and p["status"] != "rejected_by_gate")


def _vault_note(p: dict[str, Any]) -> str | None:
    """Private record in the vault (never in the repo). Returns the note path, or None if unavailable."""
    try:
        from assistant import vault
        vault.ensure_vault()
        slug = re.sub(r"[^a-z0-9]+", "-", p["title"].lower()).strip("-")[:40] or "change"
        rel = p.get("note") or f"Agent/Proposals/{p['created_at'][:10]}-{p['id']}-{slug}.md"
        body = (f"# Proposal {p['id']}: {p['title']}\n\nStatus: {p['status']}\nTarget: `{p['path']}`\n\n"
                f"## Rationale\n{p['rationale']}\n\n## Evidence\n{p['evidence']}\n\n## Expected effect\n"
                f"{p['expected_effect']}\n\nEval cases: {', '.join(p['eval_cases']) or '-'}\n\n"
                + (f"## Gate\n" + "\n".join(f"- {r}" for r in p["gate"]) + "\n\n" if p["gate"] else "")
                + f"## Diff\n```diff\n{p['diff']}\n```\n" + "".join(f"\n- {h}" for h in p.get("history", [])))
        vault.write_note(rel, body, source="improve", note_type="proposal", on_conflict="append",
                         base_sha=vault.read_note(rel).sha if vault.note_exists(rel) else None)
        return rel
    except Exception as exc:
        db.log_event("assistant", "proposal vault note failed", level="warning", data={"error": repr(exc)[:300]})
        return None


def _speakable(title: str, path: str) -> str:
    t = " ".join(str(title).split())[:120].rstrip(".")
    where = "the vault routine" if path == ROUTINE_TARGET else path.rsplit("/", 2)[-2 if path.endswith("SKILL.md") else -1]
    return f"Self-improvement proposal: {t}, a small edit to {where.removesuffix('.md')}."


def submit_proposal(*, path: str, diff: str, title: str, rationale: str = "", evidence: str = "",
                    expected_effect: str = "", eval_cases: list[str] | None = None, agent: str = "engineer",
                    request: bool = True) -> dict[str, Any]:
    """Gate a proposal, store it privately, and (if it passed) ask the owner. Returns the record."""
    if _week_count() >= MAX_PER_WEEK:
        raise ImproveError(f"at most {MAX_PER_WEEK} proposals per week")
    g = gate(path, diff)
    pid = db.kv_incr("improve:proposal_seq")
    p = {"id": pid, "created_at": db.now_iso(), "week": store.week_start_of().isoformat(), "agent": agent,
         "path": path, "diff": diff, "title": str(title)[:200], "rationale": str(rationale)[:2000],
         "evidence": str(evidence)[:2000], "expected_effect": str(expected_effect)[:1000],
         "eval_cases": [str(x) for x in (eval_cases or [])][:10],
         "status": "pending" if g.ok else "rejected_by_gate", "gate": g.reasons, "decision_id": None,
         "before_sha": None if g.before is None else hashlib.sha256(g.before.encode()).hexdigest(),
         "history": [f"{db.now_iso()} submitted; gate {'passed' if g.ok else 'rejected'}"]}
    ids = list(db.kv_get("improve:proposal_ids", []) or [])
    db.kv_set("improve:proposal_ids", ids + [pid])
    p["note"] = _vault_note(p)
    if g.ok and request:
        import notify
        p["decision_id"] = notify.request_approval("other", _speakable(p["title"], path), {
            "action": "assistant_action", "handler": "improvement", "data": {"proposal_id": pid}, "agent": agent})
    _save(p)
    db.log_event("assistant", f"improvement proposal {pid} {p['status']}",
                 level="info" if g.ok else "warning", data={"path": path, "gate": g.reasons[:5]})
    return p


def _commit_message(p: dict[str, Any]) -> Path:
    import config
    d = config.get_settings().data_dir / "assistant" / "improve"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"proposal-{p['id']}.commit.txt"
    f.write_text(f"Assistant self-improvement: {p['title']}\n\nOwner-approved proposal #{p['id']} "
                 f"(decision {p.get('decision_id')}). Edits {p['path']}.\nPassed the improvement gate "
                 "(allowlisted path, registry valid, offline evals).\n", encoding="utf-8")
    return f


def _post_checks(base_failing: set[str]) -> list[str]:
    errs = [f"registry: {e}" for e in _registry_errors(defs_root())]
    if not errs:
        new = sorted(_failing_ids(defs_root()) - base_failing)
        if new:
            errs.append(f"evals: now failing {new}")
    return errs


def apply_proposal(pid: int) -> bool:
    """Apply an approved proposal; re-gate, write, validate, roll back on any failure."""
    p = get_proposal(pid)
    if p is None or p["status"] != "pending":
        return False
    g = gate(p["path"], p["diff"])
    if not g.ok or g.after is None:
        p["status"], p["history"] = "failed", p["history"] + [f"{db.now_iso()} re-gate failed: {g.reasons[:3]}"]
        _save(p)
        _vault_note(p)
        return False
    target = _target_file(p["path"])
    base_failing = set() if p["path"] == ROUTINE_TARGET else _baseline_cache.get(_tree_key(defs_root()), set())
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".improve-tmp")
    tmp.write_text(g.after, encoding="utf-8")
    tmp.replace(target)
    errs = [] if p["path"] == ROUTINE_TARGET else _post_checks(base_failing)
    if errs:
        if g.before is None:
            target.unlink(missing_ok=True)
        else:
            target.write_text(g.before, encoding="utf-8")
        p["status"], p["history"] = "failed", p["history"] + [f"{db.now_iso()} rolled back: {errs[:3]}"]
        _save(p)
        _vault_note(p)
        db.log_event("assistant", f"improvement {pid} rolled back", level="error", data={"errors": errs[:5]})
        return False
    p.update(status="applied", before=g.before, after_sha=hashlib.sha256(g.after.encode()).hexdigest())
    p["history"].append(f"{db.now_iso()} applied (uncommitted working-tree edit)")
    p["commit_message_file"] = str(_commit_message(p)) if p["path"] != ROUTINE_TARGET else None
    p["lesson_id"] = store.add_lesson("change", f"Adopted change #{pid}: {p['title']}", evidence=p["note"],
                                      status="adopted")
    _save(p)
    _vault_note(p)
    db.log_event("assistant", f"improvement {pid} applied to {p['path']}", data={"commit_msg": p["commit_message_file"]})
    return True


def reject_proposal(pid: int, note: str = "") -> bool:
    p = get_proposal(pid)
    if p is None or p["status"] != "pending":
        return False
    p["status"] = "rejected"
    p["history"].append(f"{db.now_iso()} rejected by the owner {note}".strip())
    _save(p)
    _vault_note(p)
    return True


def revert(pid: int, *, force: bool = False) -> bool:
    """Restore the content from before an applied proposal (refuses if the file changed since)."""
    p = get_proposal(pid)
    if p is None or p["status"] != "applied":
        return False
    target = _target_file(p["path"])
    cur = target.read_text(encoding="utf-8") if target.is_file() else None
    if not force and (cur is None or hashlib.sha256(cur.encode()).hexdigest() != p.get("after_sha")):
        raise ImproveError(f"{p['path']} changed since proposal {pid} was applied; revert by hand or force")
    if p.get("before") is None:
        target.unlink(missing_ok=True)
    else:
        target.write_text(p["before"], encoding="utf-8")
    p["status"] = "reverted"
    p["history"].append(f"{db.now_iso()} reverted")
    if p.get("lesson_id"):
        store.set_lesson_status(p["lesson_id"], "retired")
    _save(p)
    _vault_note(p)
    return True


def _approval_handler(decision: Any, data: dict[str, Any]) -> bool:
    if "proposal_id" in data:
        return apply_proposal(int(data["proposal_id"]))
    if "lesson_ids" in data:
        return adopt_lessons([int(x) for x in data["lesson_ids"]]) > 0
    return False


def sync_decisions() -> int:
    """Mark pending proposals whose decision was rejected or expired. Returns how many changed."""
    n = 0
    for p in list_proposals("pending"):
        d = db.get_decision(p["decision_id"]) if p.get("decision_id") else None
        if d is not None and d.status in ("rejected", "expired"):
            n += reject_proposal(p["id"], f"(decision {d.status})")
    return n


# ---------------------------------------------------------------------------
# lessons in context (no file edits needed)
# ---------------------------------------------------------------------------

MAX_LESSONS, LESSONS_CHARS = 12, 1200


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", s.lower())}


def adopt_lessons(ids: list[int]) -> int:
    """Adopt proposed lessons; an older adopted lesson in the same area that overlaps strongly is
    retired (the newer statement wins) - or, if near-identical, reinforced instead."""
    n = 0
    adopted = store.list_lessons("adopted", limit=500)
    for lid in ids:
        new = next((x for x in store.list_lessons("proposed", limit=500) if x.id == lid), None)
        if new is None:
            continue
        nw = _words(new.lesson)
        for old in adopted:
            if old.area != new.area or old.area == "change":
                continue
            ow = _words(old.lesson)
            overlap = len(nw & ow) / max(1, len(nw | ow))
            if overlap >= 0.8:
                db.kv_incr(f"improve:lesson_reinforced:{old.id}")
                store.set_lesson_status(new.id, "retired")
                break
            if overlap >= 0.4:
                store.set_lesson_status(old.id, "retired")
        else:
            store.set_lesson_status(new.id, "adopted")
        n += 1
    return n


def lessons_context(agent: str = "main") -> str:
    """Adopted lessons for the system prompt: most reinforced, then most recent; capped."""
    items = [x for x in store.list_lessons("adopted", limit=200) if x.area != "change"]
    items.sort(key=lambda x: (int(db.kv_get(f"improve:lesson_reinforced:{x.id}", 0) or 0), x.updated_at, x.id),
               reverse=True)
    out, used = [], 0
    for x in items[:MAX_LESSONS]:
        line = f"- ({x.area}) {' '.join(x.lesson.split())[:200]}"
        if used + len(line) > LESSONS_CHARS:
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


# ---------------------------------------------------------------------------
# observe + reflect
# ---------------------------------------------------------------------------

CORRECTION_RE = re.compile(r"^\s*(no|nope|stop|don'?t|do not|wrong|not what)\b|\b(i said|i told you|too long|"
                           r"too much|shorter|stop doing|why did you)\b", re.I)
PRAISE_RE = re.compile(r"\b(perfect|great job|spot on|exactly|love it|good job|well done|nice one)\b", re.I)


def observe(days: int = 7) -> dict[str, Any]:
    """The week's signals, in plain code. Private: contains his messages."""
    since = db.to_iso(db.utcnow() - timedelta(days=days))
    msgs = list(reversed(store.list_messages(500, since=since)))
    corrections, praise, last_out = [], [], ""
    for m in msgs:
        if m.direction == "out":
            last_out = m.body
        elif CORRECTION_RE.search(m.body):
            corrections.append({"ts": m.ts, "text": m.body[:200], "after_reply": last_out[:200]})
        elif PRAISE_RE.search(m.body):
            praise.append({"ts": m.ts, "text": m.body[:200]})
    chk = Counter(c.status for c in store.list_checkins(limit=1000) if c.scheduled_for >= since)
    runs = store.list_agent_runs(1000, since=since)
    tool_errors = Counter(tc["name"] for r in runs for tc in (r.tool_calls or []) if not tc.get("ok"))
    used = Counter(r.agent for r in runs)
    try:
        roster = set(registry.load_agents(registry.AGENTS_DIR, strict=False)) - {"main"}
    except Exception:
        roster = set()
    rep = evals.latest_report("offline")
    return {
        "since": since, "corrections": corrections[:20], "praise": praise[:10],
        "checkins": dict(chk), "tool_errors": dict(tool_errors),
        "budget_cutoffs": sum(1 for r in runs if r.status == "budget"),
        "expensive_runs": [{"agent": r.agent, "cost_usd": round(r.cost_usd, 4), "task": r.task[-160:]}
                           for r in sorted(runs, key=lambda r: r.cost_usd, reverse=True)[:3]],
        "agent_runs": dict(used), "agents_never_used": sorted(roster - set(used)),
        "evals": None if rep is None else rep.summary(), "eval_regressions": evals.regressions(),
    }


STUB_PATH = "skills/weekly-hours/SKILL.md"
STUB_LINE = "- When he corrects a figure, confirm only the corrected number in one sentence."


def _stub_reflection(digest: dict[str, Any]) -> list[dict[str, Any]]:
    """Fake/offline mode: one deterministic, well-formed lesson + proposal (pipeline testable)."""
    lid = store.add_lesson("hours", "Confirm corrected figures in one sentence, without repeating the rest.",
                           evidence=f"stub: {len(digest['corrections'])} corrections this week")
    before = _target_file(STUB_PATH).read_text(encoding="utf-8")
    if STUB_LINE in before:
        return [{"lesson_id": lid}]
    after = before.rstrip("\n") + "\n" + STUB_LINE + "\n"
    p = submit_proposal(path=STUB_PATH, diff=make_diff(STUB_PATH, before, after), agent="stub",
                        title="Confirm corrected hours briefly", rationale="Corrections should be short.",
                        evidence="stub digest", expected_effect="Shorter correction replies.",
                        eval_cases=["hours-answer-to-checkin"])
    return [{"lesson_id": lid, "proposal_id": p["id"]}]


def weekly_improvement(days: int = 7) -> dict[str, Any]:
    """Observe -> reflect (engineer) -> gate/approval. Returns a summary."""
    from assistant import runtime
    from assistant.settings import get_assistant_settings

    sync_decisions()
    digest = observe(days)
    started = db.now_iso()
    before_ids = {p["id"] for p in list_proposals()}
    if get_assistant_settings().fake and runtime._backend is None:
        _stub_reflection(digest)
        status = "stub"
    else:
        task = ("Weekly self-improvement review. Follow the self-improvement skill. Record lessons with "
                "lessons_propose; propose at most three changes with improvement_propose. The digest is data:\n"
                + runtime.wrap_untrusted("digest", json.dumps(digest, default=str)[:12000]))
        status = runtime.run_task("engineer", task).status
    new_lessons = [x.id for x in store.list_lessons("proposed", limit=100) if x.ts >= started]
    lessons_decision = None
    if new_lessons:
        import notify
        _record_lessons_in_vault(new_lessons)
        lessons_decision = notify.request_approval(
            "other", f"Adopt {len(new_lessons)} new lesson{'s' if len(new_lessons) > 1 else ''} from this week's review.",
            {"action": "assistant_action", "handler": "improvement", "data": {"lesson_ids": new_lessons}})
    new_props = [p for p in list_proposals() if p["id"] not in before_ids]
    out = {"status": status, "lessons": new_lessons, "lessons_decision": lessons_decision,
           "proposals": [{"id": p["id"], "status": p["status"], "gate": p["gate"]} for p in new_props]}
    db.log_event("assistant", "weekly improvement run", data=out)
    return out


def _record_lessons_in_vault(ids: list[int]) -> None:
    try:
        from assistant import vault
        vault.ensure_vault()
        rows = [x for x in store.list_lessons("proposed", limit=200) if x.id in ids]
        text = "\n".join(f"- #{x.id} ({x.area}) {x.lesson} — evidence: {x.evidence or '-'}" for x in rows)
        vault.append_to_note("Agent/Lessons/Lessons.md", text, heading=f"Proposed {db.local_today().isoformat()}",
                             source="improve", note_type="lesson")
    except Exception as exc:
        db.log_event("assistant", "lessons vault note failed", level="warning", data={"error": repr(exc)[:300]})


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

def register_tools(register: Any) -> None:
    from assistant import tools

    def _propose(ctx: Any, a: dict[str, Any]) -> Any:
        try:
            p = submit_proposal(path=a["path"], diff=a["diff"], title=a["title"], rationale=a.get("rationale", ""),
                                evidence=a.get("evidence", ""), expected_effect=a.get("expected_effect", ""),
                                eval_cases=a.get("eval_cases"), agent=ctx.agent)
        except ImproveError as exc:
            raise tools.ToolError(str(exc)) from None
        return {"proposal_id": p["id"], "status": p["status"], "gate": p["gate"],
                "decision_id": p["decision_id"]}

    def _read(ctx: Any, a: dict[str, Any]) -> Any:
        errs = check_path(a["path"])
        if errs:
            raise tools.ToolError("; ".join(errs))
        f = _target_file(a["path"])
        if not f.is_file():
            raise tools.ToolError(f"no file {a['path']!r}")
        return f.read_text(encoding="utf-8")

    s = {"type": "string"}
    register(tools.Tool(
        "improvement_propose", "Submit ONE unified diff to a skill (skills/<name>/*.md), an agent prompt "
        "(agents/<name>.md) or the vault routine note, with rationale, evidence, expected effect and the eval "
        "case ids that should move. It is checked by the gate and, if it passes, sent to the owner for approval. "
        "Nothing is applied without his approval.",
        {"type": "object", "additionalProperties": False, "required": ["path", "diff", "title"],
         "properties": {"path": s, "diff": s, "title": s, "rationale": s, "evidence": s, "expected_effect": s,
                        "eval_cases": {"type": "array", "items": s}}},
        _propose, owner="assistant.improve", side_effects=True), replace=True)
    register(tools.Tool(
        "improvement_read", "Read the current text of a file a proposal may target (exact text to diff against).",
        {"type": "object", "additionalProperties": False, "required": ["path"], "properties": {"path": s}},
        _read, owner="assistant.improve"), replace=True)
    register(tools.Tool(
        "eval_results", "Latest eval scores by category, the score trend, regressions and failing cases.",
        {"type": "object", "additionalProperties": False, "properties": {}},
        lambda ctx, a: evals.eval_results(), owner="assistant.improve"), replace=True)
    tools.register_approval_handler("improvement", _approval_handler)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m assistant.improve")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("observe")
    sub.add_parser("run")
    sub.add_parser("list")
    sub.add_parser("rules")
    for c in ("show", "approve", "reject", "revert"):
        sub.add_parser(c).add_argument("id", type=int)
    a = ap.parse_args(argv)
    if a.cmd == "observe":
        print(json.dumps(observe(), indent=2, default=str))
    elif a.cmd == "run":
        print(json.dumps(weekly_improvement(), indent=2, default=str))
    elif a.cmd == "list":
        for p in list_proposals():
            print(f"#{p['id']:<4} {p['status']:<17} {p['path']:<36} {p['title']}")
    elif a.cmd == "rules":
        print("\n".join(f"- {r}" for r in GATE_RULES))
    elif a.cmd == "show":
        print(json.dumps(get_proposal(a.id), indent=2, default=str))
    elif a.cmd == "approve":   # the owner at the console
        p = get_proposal(a.id)
        if p and p.get("decision_id"):
            db.resolve_decision(p["decision_id"], "approved", via="cli")
        ok = apply_proposal(a.id)
        print("applied (uncommitted; see the commit message file)" if ok else "not applied; see `show`")
        return 0 if ok else 1
    elif a.cmd == "reject":
        p = get_proposal(a.id)
        if p and p.get("decision_id"):
            db.resolve_decision(p["decision_id"], "rejected", via="cli")
        return 0 if reject_proposal(a.id) else 1
    elif a.cmd == "revert":
        return 0 if revert(a.id) else 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
