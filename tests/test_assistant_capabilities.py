"""capability_check finds what exists, and a twice-failed need is not rebuilt blindly."""

from __future__ import annotations

import pytest

from assistant import capabilities
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")


def test_existing_tool_is_found_first():
    out = capabilities.check("log the hours he worked this week", catalog=False)
    assert out["next_step"] == "use_existing" and any("hours" in t["name"] for t in out["tools"])


def test_unknown_need_says_build_then_ask_after_two_failures(clock):
    need = "control the living room smart lights zigbee"
    assert capabilities.check(need, catalog=False)["next_step"] == "build"
    capabilities.record(need, "failed", "tried a zigbee bridge module")
    capabilities.record(need, "gave_up", "no bridge on this laptop")
    out = capabilities.check(need, catalog=False)
    assert out["next_step"] == "ask_first" and len(out["past_attempts"]) == 2


def test_record_validates_and_writes_the_note(clock):
    with pytest.raises(ValueError):
        capabilities.record("x", "nope")
    capabilities.record("summarise a pdf", "installed_skill", "pdf skill from anthropics/skills")
    from assistant import vault
    assert "summarise a pdf" in vault.read_note(capabilities.NOTE).body


def test_coding_skills_are_shipped_with_their_licence():
    from assistant import registry
    skills = registry.load_skills(strict=True)
    for name in ("systematic-debugging", "test-driven-development", "verification-before-completion", "writing-skills"):
        assert name in skills
        assert (skills[name].path.parent / "LICENSE-superpowers").is_file() if skills[name].path.name == "SKILL.md" \
            else (skills[name].path / "LICENSE-superpowers").is_file()


def test_superpowers_is_a_default_trusted_source():
    from assistant import skill_catalog
    assert skill_catalog.DEFAULT_REPOS == ("anthropics/skills", "obra/superpowers")


def test_tool_search_is_on_for_claude_code_and_the_voice_session():
    from assistant import brain, claude_code
    assert claude_code.base_env()["ENABLE_TOOL_SEARCH"] == "true"
    assert "ToolSearch" in brain.VOICE_BUILTINS


def test_frontend_design_skill_ships_with_its_apache_licence():
    from assistant import registry
    sk = registry.load_skills(strict=True)["frontend-design"]
    folder = sk.path.parent if sk.path.name == "SKILL.md" else sk.path
    assert "Apache License" in (folder / "LICENSE.txt").read_text()
