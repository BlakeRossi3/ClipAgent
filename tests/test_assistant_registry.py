"""assistant.registry: every shipped agent and skill file validates."""

from __future__ import annotations

import pytest

from assistant import registry, tools
from assistant.settings import get_assistant_settings
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")

ROSTER = {"main", "studio", "ledger", "quartermaster", "archivist", "researcher", "engineer", "briefer"}
SKILLS = {"persona", "morning-brief", "evening-wrap", "weekly-hours", "meal-suggestions", "reels-report",
          "memory-curation", "weekly-review"}


def test_shipped_files_validate():
    assert registry.validate_all(known_tools=tools.known_tool_names()) == []
    skills = registry.load_skills()
    agents = registry.load_agents(skills=skills, known_tools=tools.known_tool_names())
    assert ROSTER <= set(agents)
    assert SKILLS <= set(skills)
    for a in agents.values():
        for s in a.skills:
            assert s in skills, f"{a.name} names missing skill {s}"
        assert a.prompt and a.description
    assert "examples.md" in skills["persona"].resources


def test_models_resolve_from_settings():
    s = get_assistant_settings()
    agents = registry.load_agents()
    assert agents["main"].resolved_model(s) == s.model == "claude-fable-5-2"
    for name in ROSTER - {"main"}:
        assert agents[name].resolved_model(s) == "claude-opus-5-5"
    assert "delegate" in agents["main"].tools
    assert all("delegate" not in a.tools for n, a in agents.items() if n != "main")


def _write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_clear_errors(tmp_path):
    sk = tmp_path / "skills"
    _write(sk / "good" / "SKILL.md", "---\nname: good\ndescription: fine\n---\nDo it.")
    _write(sk / "misnamed" / "SKILL.md", "---\nname: other\ndescription: x\n---\nbody")
    _write(sk / "nofront" / "SKILL.md", "just text")
    (sk / "empty").mkdir()
    with pytest.raises(registry.RegistryError) as ei:
        registry.load_skills(sk)
    msg = " | ".join(ei.value.errors)
    assert "does not match" in msg and "missing YAML frontmatter" in msg and "skills/empty/SKILL.md: missing" in msg
    skills = registry.load_skills(sk, strict=False)
    ag = tmp_path / "agents"
    _write(ag / "main.md", "---\nname: main\ndescription: m\nmodel: main\ntools: [delegate]\nskills: [good]\n---\nP")
    _write(ag / "bad.md", "---\nname: bad\ndescription: b\nmodel: gpt-4\ntools: [shell, delegate]\n"
                          "skills: [ghost]\n---\n")
    with pytest.raises(registry.RegistryError) as ei:
        registry.load_agents(ag, skills=skills, known_tools={"delegate"})
    msg = " | ".join(ei.value.errors)
    for needle in ("model must be", "unknown tool 'shell'", "unknown skill 'ghost'", "only the main agent may delegate",
                   "empty system prompt"):
        assert needle in msg
    _write(ag / "main.md", "---\nname: notmain\ndescription: m\n---\nP")
    with pytest.raises(registry.RegistryError):
        registry.load_agents(ag, skills=skills, strict=True)
