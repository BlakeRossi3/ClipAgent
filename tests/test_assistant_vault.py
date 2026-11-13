"""assistant.vault: Obsidian-compatible memory, path safety, human-edit preservation, FTS search."""

from __future__ import annotations

import os
from datetime import date

import pytest

from assistant import vault
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")


@pytest.fixture
def root(assistant_env, clock):
    return vault.ensure_vault()


def test_ensure_vault_from_template(root):
    for rel in ("Home.md", "Profile/About.md", "Profile/Open Questions.md", "Health/Meals.md", "Agent/Persona.md",
                "Agent/Roster.md", "Agent/Lessons/Lessons.md", "Inbox/Inbox.md"):
        assert (root / rel).is_file(), rel
    for d in ("Journal", "Projects", "People", "Decisions", "Finance"):
        assert (root / d).is_dir()
    assert not list(root.rglob(".gitkeep"))
    home = vault.read_note("Home.md")
    assert home.meta["type"] == "index" and home.meta["created"] == "2026-11-13"
    assert "{{date}}" not in (root / "Home.md").read_text()
    (root / "Home.md").write_text("mine")
    vault.ensure_vault()  # never overwrites
    assert (root / "Home.md").read_text() == "mine"


def test_frontmatter_round_trip(root):
    meta = {"type": "person", "source": "sms", "confidence": "inferred", "tags": ["family", "a b"],
            "custom": "Keeps: colons", "rating": 3}
    n = vault.write_note("People/Alex", "# Alex\n\nLikes [[Hiking]].\n", meta=meta)
    assert n.path == "People/Alex.md"
    again = vault.read_note("People/Alex.md")
    assert again.meta["tags"] == ["family", "a b"] and again.meta["custom"] == "Keeps: colons"
    assert again.meta["rating"] == 3 and again.meta["created"] == again.meta["updated"] == "2026-11-13"
    assert again.body.startswith("# Alex") and again.sha == n.sha
    text = (root / "People/Alex.md").read_text()
    assert text.startswith("---\ntype: person\ncreated: 2026-11-13\nupdated: 2026-11-13\nsource: sms\n")
    assert vault.parse_note_text("no frontmatter here") == ({}, "no frontmatter here")
    assert vault.parse_note_text("---\n: [broken\n---\nbody")[0] == {}


@pytest.mark.parametrize("bad", ["../outside.md", "/etc/passwd", "Profile/../../x.md", "~/x.md", "C:/x.md",
                                 ".obsidian/app.md", "Profile/.hidden.md", "notes.txt", "x.md\x00.txt", "", "..",
                                 "Profile\\..\\..\\x.md", "script.py"])
def test_path_attacks_rejected(root, bad):
    with pytest.raises(vault.VaultPathError):
        vault.write_note(bad, "pwned")
    with pytest.raises((vault.VaultPathError, vault.NoteNotFound)):
        vault.read_note(bad)
    assert not (root.parent / "outside.md").exists()


def test_symlink_escape_rejected(root, tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_text("top secret")
    os.symlink(secret, root / "Inbox" / "link.md")
    os.symlink(tmp_path, root / "escape")
    with pytest.raises(vault.VaultPathError):
        vault.read_note("Inbox/link.md")
    with pytest.raises(vault.VaultPathError):
        vault.write_note("escape/new.md", "x")
    with pytest.raises(vault.VaultPathError):
        vault.append_to_note("Inbox/link.md", "x")
    assert secret.read_text() == "top secret"
    assert "Inbox/link.md" not in vault.list_notes()
    with pytest.raises(vault.VaultPathError):
        vault.list_notes("../")


def test_human_edit_is_never_clobbered(root):
    n = vault.write_note("Projects/Garden.md", "# Garden\n\nPlan beds.\n")
    # the owner edits in Obsidian after the assistant read it
    p = root / "Projects/Garden.md"
    p.write_text(p.read_text().replace("Plan beds.", "Plan beds. Tomatoes along the fence."))
    out = vault.write_note("Projects/Garden.md", "# Garden\n\nAssistant rewrite.\n", base_sha=n.sha)
    text = p.read_text()
    assert "Tomatoes along the fence." in text and "Assistant rewrite." in text and "## Merged" in text
    with pytest.raises(vault.NoteConflict):
        vault.write_note("Projects/Garden.md", "again", base_sha=n.sha, on_conflict="raise")
    # writing without having read an existing note also merges instead of replacing
    vault.write_note("Projects/Garden.md", "blind write")
    assert "Tomatoes along the fence." in p.read_text()
    # a proper read-modify-write replaces
    cur = vault.read_note("Projects/Garden.md")
    vault.write_note(cur.path, "# Garden\n\nClean.\n", base_sha=cur.sha)
    assert vault.read_note("Projects/Garden.md").body.strip() == "# Garden\n\nClean."
    assert out.path == "Projects/Garden.md"


def test_append_under_heading_and_journal(root, clock):
    vault.append_to_note("Inbox/Inbox.md", "- first", heading="Facts")
    vault.append_to_note("Inbox/Inbox.md", "tail line")
    vault.append_to_note("Inbox/Inbox.md", "- second", heading="Facts")
    body = vault.read_note("Inbox/Inbox.md").body
    facts = body.split("## Facts", 1)[1]
    assert facts.index("- first") < facts.index("tail line") < facts.index("- second")  # Facts is the last section
    j = vault.append_journal("Logged 38 hours.")
    vault.append_journal("Suggested lunch.")
    assert j.path == "Journal/2026/2026-11-13.md"
    jn = vault.read_note(j.path)
    assert jn.meta["type"] == "journal" and "- 10:00 Logged 38 hours." in jn.body and "[[Home]]" in jn.body
    vault.append_journal("Old entry", day=date(2026, 11, 1))
    assert "- Old entry" in vault.read_note("Journal/2026/2026-11-01.md").body


def test_list_and_backlinks(root):
    vault.write_note("People/Alex.md", "# Alex")
    vault.write_note("Projects/Trip.md", "With [[Alex]] and [[People/Alex|him]].")
    vault.append_journal("Called [[Alex#Phone]].")
    assert "People/Alex.md" in vault.list_notes("People")
    links = vault.backlinks("People/Alex.md")
    assert "Projects/Trip.md" in links and "Journal/2026/2026-11-13.md" in links
    assert "People/Alex.md" not in links
    assert "Home.md" in vault.backlinks("About")


def test_remember_dedupes_and_files_with_provenance(root):
    r1 = vault.remember("Works at the warehouse on weekdays.", area="work", source="sms")
    assert r1.added and r1.path == "Profile/Work.md"
    r2 = vault.remember("works at the warehouse on weekdays", area="work", source="dashboard")
    assert not r2.added and r2.duplicate_of.startswith("Works at the warehouse")
    r3 = vault.remember("Allergic to peanuts", area="meals", source="sms", confidence="stated")
    assert r3.path == "Health/Meals.md"
    vault.remember("Probably prefers spicy food", area="meals", source="sms", confidence="inferred")
    body = vault.read_note("Health/Meals.md").body
    assert "- Allergic to peanuts — _stated, sms, 2026-11-13_" in body
    assert "_inferred, sms, 2026-11-13_" in body
    assert vault.facts_in(body) == ["Allergic to peanuts", "Probably prefers spicy food"]
    r4 = vault.remember("Sister lives in Denver", area="person:Jamie", source="sms")
    assert r4.path == "People/Jamie.md" and vault.read_note(r4.path).meta["type"] == "person"
    with pytest.raises(ValueError):
        vault.remember("x", area="nowhere", source="sms")
    with pytest.raises(ValueError):
        vault.remember("x", area="person:../../etc", source="sms")
    with pytest.raises(ValueError):
        vault.remember("x", area="work", source="sms", confidence="certain")
    with pytest.raises(ValueError):
        vault.remember("   ", area="work", source="sms")


def test_forget(root):
    vault.remember("Drives a blue Civic", area="about", source="sms")
    vault.remember("Has a dog called Max", area="about", source="sms")
    assert len(vault.forget("drives a blue civic")) == 1
    body = vault.read_note("Profile/About.md").body
    assert "Civic" not in body and "Max" in body
    assert vault.forget("never said this", area="about") == []


def test_search_and_incremental_index(root):
    vault.write_note("Projects/Garden.md", "# Garden\n\nTomatoes and basil along the fence.")
    hits = vault.search("tomatoes")
    assert hits and hits[0].path == "Projects/Garden.md" and "Tomatoes" in hits[0].snippet
    # human edit on disk -> picked up on the next search
    p = root / "Projects/Garden.md"
    p.write_text(p.read_text() + "\nAlso courgettes.\n")
    os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 5))
    assert vault.search("courgettes")[0].path == "Projects/Garden.md"
    stats = vault.sync_index()
    assert stats.added == stats.updated == stats.removed == 0 and stats.unchanged > 5
    # a new file dropped in by Obsidian, a deletion
    (root / "Inbox/Idea.md").write_text("Build a sauna by the lake.")
    st = vault.sync_index()
    assert st.added == 1 and "Inbox/Idea.md" in st.paths
    p.unlink()
    assert vault.sync_index().removed == 1
    assert vault.search("tomatoes") == []
    assert vault.search("sauna")[0].path == "Inbox/Idea.md"
    # weird queries never raise
    for q in ['"', "AND OR NOT", "*", "a", "", "foo:bar (baz"]:
        vault.search(q)
    full = vault.reindex()
    assert full.added == len(vault.list_notes())


def test_context_for_respects_budget(root, clock):
    vault.remember("Prefers short texts", area="preferences", source="sms")
    vault.remember("Saving for a motorbike", area="finance", source="sms")
    vault.append_journal("Asked about the motorbike fund.")
    ctx = vault.context_for("How is the motorbike fund?", budget_chars=2500)
    assert len(ctx) <= 2500
    assert '<note path="Profile/Preferences.md">' in ctx and "Finance/Finance.md" in ctx
    assert "Journal/2026/2026-11-13.md" in ctx
    small = vault.context_for("motorbike", budget_chars=300)
    assert len(small) <= 300
