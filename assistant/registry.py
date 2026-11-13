"""Load and validate sub-agents (``agents/*.md``) and skills (``skills/<name>/SKILL.md``).

Both are plain markdown files with YAML frontmatter - editable, diffable, versioned in git,
and the unit of self-improvement (the engineer agent proposes patches to them).

Agent file::

    ---
    name: ledger                      # == file stem
    description: one line              # when the main agent should delegate here
    model: subagent                   # main | subagent | a full model id (claude-...)
    tools: [ledger_log_hours, ...]    # names from assistant.tools (or runtime-provided)
    skills: [weekly-hours]            # skill names that must exist
    ---
    System prompt body.

Skill file (standard Claude skill format)::

    ---
    name: weekly-hours                # == directory name
    description: what it does and when to use it
    ---
    Instructions. Optional extra files (scripts/resources) live next to SKILL.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "agents"
SKILLS_DIR = REPO_ROOT / "skills"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
MODEL_ALIASES = ("main", "subagent")
MAX_DESCRIPTION = 1024


class RegistryError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True, slots=True)
class SkillSpec:
    name: str
    description: str
    body: str
    path: Path
    resources: tuple[str, ...] = ()    # other files in the skill directory (relative names)


@dataclass(frozen=True, slots=True)
class AgentSpec:
    name: str
    description: str
    model: str
    tools: tuple[str, ...]
    skills: tuple[str, ...]
    prompt: str
    path: Path
    extra: dict[str, Any] = field(default_factory=dict)

    def resolved_model(self, settings: Any) -> str:
        """``main`` -> settings.model, ``subagent`` -> settings.subagent_model, else the id itself."""
        if self.model == "main":
            return settings.model
        if self.model == "subagent":
            return settings.subagent_model
        return self.model


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter, body). Raises ValueError when the frontmatter is missing or invalid."""
    import yaml

    if text.startswith("﻿"):
        text = text[1:]
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", text, re.S)
    if not m:
        raise ValueError("missing YAML frontmatter (--- ... ---)")
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML frontmatter: {exc}") from None
    if not isinstance(meta, dict):
        raise ValueError("frontmatter must be a mapping")
    return meta, m.group(2).strip()


def _str_list(meta: dict[str, Any], key: str, where: str, errors: list[str]) -> tuple[str, ...]:
    v = meta.get(key, [])
    if v is None:
        return ()
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        errors.append(f"{where}: '{key}' must be a list of names")
        return ()
    return tuple(x.strip() for x in v)


def _common(meta: dict[str, Any], expected_name: str, where: str, errors: list[str]) -> tuple[str, str]:
    name = meta.get("name")
    desc = meta.get("description")
    if not isinstance(name, str) or not NAME_RE.match(name):
        errors.append(f"{where}: 'name' must match {NAME_RE.pattern}")
        name = expected_name
    elif name != expected_name:
        errors.append(f"{where}: name {name!r} does not match its file/directory name {expected_name!r}")
    if not isinstance(desc, str) or not desc.strip():
        errors.append(f"{where}: 'description' is required")
        desc = ""
    elif len(desc) > MAX_DESCRIPTION:
        errors.append(f"{where}: description longer than {MAX_DESCRIPTION} characters")
    return name, (desc or "").strip()


def load_skills(skills_dir: Path | None = None, *, strict: bool = True) -> dict[str, SkillSpec]:
    """All skills by name. ``strict`` raises :class:`RegistryError` listing every problem."""
    base = skills_dir or SKILLS_DIR
    errors: list[str] = []
    out: dict[str, SkillSpec] = {}
    for d in sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))) if base.is_dir() else []:
        f = d / "SKILL.md"
        where = f"skills/{d.name}/SKILL.md"
        if not f.is_file():
            errors.append(f"{where}: missing")
            continue
        try:
            meta, body = parse_frontmatter(f.read_text(encoding="utf-8"))
        except ValueError as exc:
            errors.append(f"{where}: {exc}")
            continue
        name, desc = _common(meta, d.name, where, errors)
        if not body:
            errors.append(f"{where}: empty instructions")
        resources = tuple(sorted(p.relative_to(d).as_posix() for p in d.rglob("*")
                                 if p.is_file() and p.name != "SKILL.md" and "__pycache__" not in p.parts))
        out[name] = SkillSpec(name, desc, body, f, resources)
    if errors and strict:
        raise RegistryError(errors)
    return out


def load_agents(agents_dir: Path | None = None, *, skills: dict[str, SkillSpec] | None = None,
                known_tools: set[str] | None = None, strict: bool = True) -> dict[str, AgentSpec]:
    """All agents by name, validated against the skills (and, if given, the known tool names)."""
    base = agents_dir or AGENTS_DIR
    skills = skills if skills is not None else load_skills(strict=strict)
    errors: list[str] = []
    out: dict[str, AgentSpec] = {}
    for f in sorted(base.glob("*.md")) if base.is_dir() else []:
        where = f"agents/{f.name}"
        try:
            meta, body = parse_frontmatter(f.read_text(encoding="utf-8"))
        except ValueError as exc:
            errors.append(f"{where}: {exc}")
            continue
        name, desc = _common(meta, f.stem, where, errors)
        model = meta.get("model", "subagent")
        if not isinstance(model, str) or not (model in MODEL_ALIASES or re.match(r"^claude-[a-z0-9.-]+$", model)):
            errors.append(f"{where}: model must be one of {MODEL_ALIASES} or a full claude-* model id")
            model = "subagent"
        tools = _str_list(meta, "tools", where, errors)
        agent_skills = _str_list(meta, "skills", where, errors)
        for s in agent_skills:
            if s not in skills:
                errors.append(f"{where}: unknown skill {s!r}")
        if known_tools is not None:
            for t in tools:
                if t not in known_tools:
                    errors.append(f"{where}: unknown tool {t!r}")
        if "delegate" in tools and name != "main":
            errors.append(f"{where}: only the main agent may delegate")
        if not body:
            errors.append(f"{where}: empty system prompt")
        extra = {k: v for k, v in meta.items() if k not in ("name", "description", "model", "tools", "skills")}
        out[name] = AgentSpec(name, desc, model, tools, agent_skills, body, f, extra)
    if "main" not in out:
        errors.append("agents/main.md: the main agent is required")
    if errors and strict:
        raise RegistryError(errors)
    return out


def validate_all(known_tools: set[str] | None = None) -> list[str]:
    """Every problem in the shipped agents and skills (empty list = all good)."""
    errors: list[str] = []
    try:
        skills = load_skills()
    except RegistryError as exc:
        errors.extend(exc.errors)
        skills = load_skills(strict=False)
    try:
        load_agents(skills=skills, known_tools=known_tools)
    except RegistryError as exc:
        errors.extend(exc.errors)
    return errors
