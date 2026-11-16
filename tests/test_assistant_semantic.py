"""assistant/semantic.py: memory search by meaning (fake embedder; no model, no network)."""

from __future__ import annotations

import hashlib
import math
import re

import pytest

from assistant import semantic, store, vault
from assistant import settings as asettings
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")

SYNONYMS = {"budget": "money", "savings": "money", "saving": "money", "spending": "money", "money": "money",
            "dog": "pet", "puppy": "pet", "pet": "pet"}
STOP = {"the", "a", "an", "and", "of", "to", "my", "i", "that", "about", "said", "thing", "each", "into", "is", "on"}


class FakeEmbedder:
    """Hashed bag of words; synonyms share a heavily weighted concept dimension."""

    name = "fake:bow"
    dim = 64

    def __init__(self) -> None:
        self.texts: list[str] = []

    def embed(self, texts):
        out = []
        for t in texts:
            self.texts.append(t)
            v = [0.0] * self.dim
            for w in re.findall(r"[a-z]+", t.lower()):
                if w in STOP:
                    continue
                key, weight = (SYNONYMS[w], 5.0) if w in SYNONYMS else (w, 1.0)
                v[int(hashlib.md5(key.encode()).hexdigest(), 16) % self.dim] += weight
            out.append(v)
        return out


@pytest.fixture
def emb():
    e = FakeEmbedder()
    semantic.set_embedder(e)
    semantic.set_spawner(lambda fn: fn())
    yield e
    semantic.reset()


@pytest.fixture
def notes(assistant_env):
    vault.ensure_vault()
    vault.write_note("Finance/Savings.md", "# Plan\n\nPut 200 dollars into the savings account every month.")
    vault.write_note("Hobbies/Kayak.md", "# Weekends\n\nPaddling the kayak on the river with friends.")
    vault.write_note("People/Rex.md", "Rex is the puppy. Walks twice daily.")
    return assistant_env


def test_meaning_match_without_keyword_overlap(emb, notes):
    query = "that thing I said about my budget"
    assert all(h.path != "Finance/Savings.md" for h in vault.keyword_search(query))
    hits = semantic.search(query, 5)
    assert hits and hits[0].path == "Finance/Savings.md"
    assert "Finance/Savings.md" in [h.path for h in vault.search(query, 5)]   # fused in (keywords alone miss it)
    assert "Finance/Savings.md" in vault.context_for(query)
    # a query with no meaning overlap finds nothing semantic above the floor
    assert all(h.path != "Finance/Savings.md" for h in semantic.search("river paddling", 5))


def test_incremental_sync_only_reembeds_changed(emb, notes):
    first = semantic.sync()
    assert "Finance/Savings.md" in first.embedded and first.chunks >= 3
    n_notes, n_chunks = store.vector_counts()
    assert n_notes == len(vault.list_notes()) and n_chunks >= n_notes - 1
    calls = len(emb.texts)
    again = semantic.sync()
    assert again.embedded == [] and len(emb.texts) == calls and again.unchanged == n_notes
    note = vault.read_note("Hobbies/Kayak.md")
    vault.write_note("Hobbies/Kayak.md", note.body + "\n\nAlso canoe.", base_sha=note.sha)
    third = semantic.sync()
    assert third.embedded == ["Hobbies/Kayak.md"]
    vault.delete_note("People/Rex.md")
    fourth = semantic.sync()
    assert fourth.removed == 1 and fourth.embedded == []
    assert "People/Rex.md" not in {h.path for h in semantic.search("my dog", 5)}


def test_model_change_reembeds_everything(emb, notes):
    semantic.sync()
    other = FakeEmbedder()
    other.name = "fake:v2"
    semantic.set_embedder(other)
    assert len(semantic.sync().embedded) == len(vault.list_notes())


def test_hybrid_keeps_keyword_hits(emb, notes):
    kw = vault.keyword_search("kayak river", 20)
    fused = vault.search("kayak river", 20)
    assert kw and fused[0].path == kw[0].path == "Hobbies/Kayak.md"
    assert {h.path for h in kw} <= {h.path for h in fused}
    # a keyword hit keeps its FTS snippet
    assert fused[0].snippet == kw[0].snippet


def test_fuse_rrf_order():
    class H:
        def __init__(self, p):
            self.path = p

    out = semantic.fuse([H("a"), H("b")], [semantic.SemanticHit("b", "", "", 0.9),
                                            semantic.SemanticHit("c", "", "", 0.8)], 10)
    assert [p for p, _ in out] == ["b", "a", "c"]


def test_off_falls_back_to_keywords(emb, notes, monkeypatch):
    monkeypatch.setenv("ASSISTANT_SEMANTIC_MEMORY", "off")
    asettings.reset_assistant_settings()
    assert not semantic.enabled()
    q = "that thing I said about my budget"
    assert vault.search(q) == vault.keyword_search(q)
    assert semantic.search(q) == []
    assert semantic.sync().skipped
    assert "keywords only" in semantic.status_line() and "off" in semantic.status_line()


def test_unavailable_package_degrades(notes):
    assert not semantic.enabled()  # conftest: model2vec counts as not installed
    assert vault.search("kayak") == vault.keyword_search("kayak")
    line = semantic.status_line()
    assert line.startswith("Memory search: keywords only") and "model2vec" in line


def test_cold_start_does_not_block_and_load_failure_degrades(notes, monkeypatch):
    monkeypatch.setattr(semantic, "_package_installed", lambda: True)
    queued = []
    semantic.set_spawner(queued.append)
    fake = FakeEmbedder()
    monkeypatch.setattr(semantic, "_loader", lambda: fake)
    q = "my budget"
    assert semantic.search(q) == [] and len(queued) == 1      # returns at once, loads in the background
    assert vault.search("kayak") == vault.keyword_search("kayak")
    queued.pop()()                                             # the background load + full sync
    assert store.vector_counts()[0] == len(vault.list_notes())
    assert semantic.search(q)[0].path == "Finance/Savings.md"
    assert "keywords + meaning" in semantic.status_line()

    semantic.reset()
    monkeypatch.setattr(semantic, "_package_installed", lambda: True)
    semantic.set_spawner(lambda fn: fn())

    def broken():
        raise OSError("offline")

    monkeypatch.setattr(semantic, "_loader", broken)
    assert semantic.search(q) == []
    assert "failed to load" in semantic.status_line()
    assert vault.search("kayak") == vault.keyword_search("kayak")
    assert semantic.warm_async() is False                      # backs off instead of retrying every search


def test_chunking_by_heading_and_size():
    body = "intro line\n\n# One\n\npara a\n\npara b\n\n## Two\n\n" + ("word " * 400)
    chunks = semantic.chunk_note(body)
    assert chunks[0] == ("", "intro line")
    assert chunks[1] == ("One", "para a\n\npara b")
    assert all(h == "Two" for h, _ in chunks[2:]) and len(chunks) > 3
    assert all(len(t) <= semantic.CHUNK_MAX for _, t in chunks)


def test_vectors_are_normalised_float32():
    v = semantic._unpack(semantic._pack([3.0, 4.0]))
    assert len(v) == 2 and math.isclose(v[0], 0.6, rel_tol=1e-6) and math.isclose(v[1], 0.8, rel_tol=1e-6)


def test_settings_and_job(monkeypatch):
    for raw, want in (("on", "on"), ("OFF", "off"), ("", "auto"), ("maybe", "auto")):
        assert asettings.load_assistant_settings({"ASSISTANT_SEMANTIC_MEMORY": raw}).semantic_memory == want
    import main

    monkeypatch.setenv("ASSISTANT_ENABLED", "true")
    asettings.reset_assistant_settings()
    assert any(j.id == "assistant_semantic_sync" for j in main.assistant_job_specs())
