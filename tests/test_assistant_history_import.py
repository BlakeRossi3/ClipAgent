"""assistant.history_import: multi-zip export import, search, distillation, manifest + Downloads watcher."""

from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path

import pytest

import db
from assistant import history_import as hi
from assistant import runtime, tools, vault
from assistant import toolkit_settings as ts
from assistant_fixtures import assistant_env, fake_model  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env", "clean")


@pytest.fixture
def clean(monkeypatch):
    for k in list(os.environ):
        if k.startswith(ts.ENV_PREFIXES):
            monkeypatch.delenv(k, raising=False)
    ts.reset_toolkit_settings()
    hi._reset_migration_cache()
    yield
    ts.reset_toolkit_settings()


def conv(uuid, name, day, msgs):
    return {"uuid": uuid, "name": name, "created_at": f"{day}T10:00:00Z", "updated_at": f"{day}T11:00:00Z",
            "chat_messages": [{"sender": s, "text": t if i % 2 == 0 else "",
                               "content": [{"type": "text", "text": t}], "created_at": f"{day}T10:0{i}:00Z"}
                              for i, (s, t) in enumerate(msgs)]}


LONG = "I run a small YouTube Shorts channel about space and I work weekends at the hardware store. " * 3
CONVS = [
    conv("c1", "Planning the shorts channel", "2026-10-01", [("human", LONG), ("assistant", "Great plan.")]),
    conv("c2", "Hi", "2026-10-02", [("human", "hi"), ("assistant", "hello")]),
    conv("c3", "Dinner ideas", "2026-11-01", [("human", "My sister Dana is vegetarian and visits on Sundays. " * 8),
                                             ("assistant", "Try a lentil curry.")]),
]


def make_zip(path: Path, members: dict[str, object]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data if isinstance(data, (str, bytes)) else json.dumps(data))
    return path


def export(tmp_path) -> list[Path]:
    return [
        make_zip(tmp_path / "projects-000.zip", {"projects.json": [{"uuid": "p1", "name": "Shorts", "description": "d",
                                                                     "is_private": True, "docs": [{"filename": "a.md",
                                                                                                    "content": "x"}]}]}),
        make_zip(tmp_path / "conversations-001.zip", {"data/conversations.json": CONVS[2:]}),
        make_zip(tmp_path / "light_metadata-000.zip", {"conversations.json": [
            {"uuid": c["uuid"], "name": c["name"], "created_at": c["created_at"], "updated_at": c["updated_at"]}
            for c in CONVS] + [{"uuid": "c9", "name": "only metadata", "created_at": "2026-01-01T00:00:00Z"}]}),
        make_zip(tmp_path / "conversations-000.zip", {"conversations.json": CONVS[:2], "users.json": [
            {"uuid": "u", "full_name": "Owner", "email_address": "x@example.com"}]}),
        make_zip(tmp_path / "memories-000.zip", {"memories.json": {"conversations_memory": "Likes space content."},
                                                 "weird.json": {"foo": 1}}),
    ]


def test_import_many_zips_any_order_and_idempotent(tmp_path):
    res = hi.import_files(export(tmp_path))
    assert all(r["status"] == "imported" for r in res), res
    p = hi.progress()
    assert p["conversations"] == 3 and p["metadata_only"] == 1 and p["projects"] == 1 and p["memories"] == 1
    assert p["messages"] == 6 and p["pending"] == 3
    # light metadata imported after messages did not clobber them
    assert hi.conversation_text("c1").startswith("[Owner] I run a small")
    # unknown shapes are logged, not fatal
    mem = next(r for r in res if r["file"] == "memories-000.zip")
    assert any("weird.json" in u for u in mem["unknown"])
    assert any("unrecognised" in e.message for e in db.list_events(limit=50))
    assert "Likes space content" in vault.read_note("Inbox/Claude memory import.md").body
    # re-upload: skipped by hash
    again = hi.import_files(export(tmp_path)[:1])
    assert again[0]["status"] in ("already imported", "imported")
    assert hi.import_files([tmp_path / "conversations-000.zip"])[0]["status"] == "already imported"


def test_search(tmp_path):
    hi.import_files(export(tmp_path))
    hits = hi.search("vegetarian sister")
    assert hits and hits[0]["conversation"] == "Dinner ideas" and hits[0]["who"] == "owner" and hits[0]["date"]
    assert hi.search("") == [] and hi.search("zzzz-nothing") == []
    tools.load_extensions()
    r = tools.dispatch("history_search", {"query": "hardware store"}, tools.ToolContext(), allowed={"history_search"})
    assert r.ok and "Planning the shorts channel" in r.content


def test_unsafe_and_bad_archives(tmp_path):
    bad = make_zip(tmp_path / "evil.zip", {"../../x.json": []})
    assert hi.import_files([bad])[0]["status"] == "error"
    (tmp_path / "notzip.zip").write_bytes(b"PK\x03\x04garbage")
    assert "error" == hi.import_files([tmp_path / "notzip.zip"])[0]["status"]
    man = tmp_path / "manifest.json"
    man.write_text(json.dumps({"data_files": [{"export_url": "https://claude.ai/export/x", "filename": "a.zip"}]}))
    r = hi.import_files([man])[0]
    assert r["status"] == "error" and "manifest" in r["error"]


def test_jsonl_and_import_bytes(tmp_path):
    lines = "\n".join(json.dumps({"conversation_uuid": "j1", "sender": "human", "text": f"line {i}"}) for i in range(3))
    res = hi.import_bytes([("conversations.jsonl", lines.encode()), ("../../sneaky.json", json.dumps(CONVS[:1]).encode())])
    assert [r["status"] for r in res] == ["imported", "imported"] and res[1]["file"] == "sneaky.json"
    assert hi.progress()["conversations"] == 2
    assert not list((hi.config.get_settings().data_dir / "assistant" / "history" / "incoming").iterdir())


def test_distil_newest_first_with_provenance_and_resume(tmp_path, fake_model):
    hi.import_files(export(tmp_path))
    fake_model.queue_for("archivist",
                         json.dumps({"facts": [{"fact": "Has a sister, Dana, who is vegetarian", "area": "person:Dana",
                                                "confidence": "stated"},
                                               {"fact": "Dana visits on Sundays", "area": "nonsense-area",
                                                "confidence": "inferred"}]}))
    r = hi.distil_pending(limit=1)
    assert r.processed == 1 and r.facts_added == 2 and r.skipped == 1 and r.remaining == 1
    note = vault.read_note("People/Dana.md").body
    assert "vegetarian" in note and 'claude chat "Dinner ideas" 2026-11-01' in note
    assert "Dana visits on Sundays" in vault.read_note("Inbox/Inbox.md").body  # unknown area -> inbox
    task = fake_model.requests[-1].messages[0]["content"]
    assert "Dinner ideas" in task and "untrusted" in task
    # next run: c1 gets the default fake reply (not JSON) -> error, retried later
    r2 = hi.distil_pending()
    assert r2.errors == 1 and r2.remaining == 1
    fake_model.queue_for("archivist", '```json\n{"facts": [{"fact": "Works weekends at a hardware store", "area": "work"}]}\n```')
    r3 = hi.distil_pending()
    assert r3.processed == 1 and r3.remaining == 0
    assert hi.nightly() is None
    assert hi.progress()["pct"] == 100


def test_distil_stops_on_budget(tmp_path, fake_model, monkeypatch):
    hi.import_files(export(tmp_path))
    monkeypatch.setattr(hi, "_turn_room", lambda: "reserve")
    r = hi.distil_pending()
    assert r.stopped == "reserve" and r.processed == 0 and r.skipped == 1 and r.remaining == 2  # trivial: free


MANIFEST = {
    "instructions": "Download each file within 24 hours.",
    "created_at": "2026-11-13T12:00:00Z", "total_files": 3,
    "data_files": [
        {"batch_index": 0, "export_url": "https://claude.ai/export/abc/light_metadata-000.zip?sig=1",
         "category": "light_metadata", "part": 0, "filename": "light_metadata-000.zip"},
        {"batch_index": 0, "export_url": "https://evil.example/export/x", "category": "projects", "part": 0,
         "filename": "projects-000.zip"},
        {"batch_index": 1, "export_url": "https://claude.ai/export/abc/conversations-000.zip?sig=2",
         "category": "conversations", "part": 0, "filename": "conversations-000.zip"},
    ],
}


def test_parse_manifest_full_and_truncated(clock):
    m = hi.parse_manifest(json.dumps(MANIFEST))
    assert [e.filename for e in m.entries] == ["light_metadata-000.zip", "projects-000.zip", "conversations-000.zip"]
    assert [e.url_ok for e in m.entries] == [True, False, True] and not m.truncated and not m.expired
    text = json.dumps(MANIFEST, indent=1)
    cut = text[: text.index("conversations-000.zip?sig=2")]
    t = hi.parse_manifest(cut)
    assert t.truncated and len(t.entries) == 2 and t.total_files == 3 and t.missing_count == 1
    with pytest.raises(hi.HistoryImportError):
        hi.parse_manifest('{"instructions": "x", "data_fi')
    clock.advance(hours=30)
    assert hi.parse_manifest(json.dumps(MANIFEST)).expired


@pytest.mark.parametrize("url,ok", [
    ("https://claude.ai/export/abc/x.zip", True),
    ("http://claude.ai/export/abc/x.zip", False),
    ("https://claude.ai.evil.com/export/x", False),
    ("https://evil.com/claude.ai/export/x", False),
    ("https://claude.ai/settings", False),
    ("https://user:pw@claude.ai/export/x", False),
    ("https://claude.ai:8443/export/x", False),
    ("javascript:alert(1)", False),
])
def test_validate_export_url(url, ok):
    assert (hi.validate_export_url(url) == "") is ok


def test_scan_downloads_partial_then_complete(tmp_path):
    exp = ["conversations-000.zip", "projects-000.zip"]
    (tmp_path / "conversations-000.zip.crdownload").write_bytes(b"part")
    sizes: dict = {}
    s1 = hi.scan_downloads(exp, tmp_path, sizes=sizes)
    assert s1["conversations-000.zip"]["state"] == "partial" and s1["projects-000.zip"]["state"] == "missing"
    (tmp_path / "conversations-000.zip.crdownload").unlink()
    make_zip(tmp_path / "conversations-000 (1).zip", {"conversations.json": CONVS[:1]})
    s2 = hi.scan_downloads(exp, tmp_path, sizes=sizes)
    assert s2["conversations-000.zip"]["state"] == "stabilising"
    s3 = hi.scan_downloads(exp, tmp_path, sizes=sizes)
    assert s3["conversations-000.zip"]["state"] == "complete"
    assert s3["conversations-000.zip"]["path"].endswith("conversations-000 (1).zip")


def test_watch_downloads_imports_as_files_arrive(tmp_path, fake_model):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    exp = ["conversations-000.zip", "projects-000.zip"]
    t = {"now": 1000.0, "ticks": 0}

    def sleep(s):
        t["now"] += s
        t["ticks"] += 1
        if t["ticks"] == 1:
            (folder / "conversations-000.zip.crdownload").write_bytes(b"x")
        if t["ticks"] == 2:
            (folder / "conversations-000.zip.crdownload").unlink()
            make_zip(folder / "conversations-000.zip", {"conversations.json": CONVS})
    fake_model.queue_for("archivist", '{"facts": []}', '{"facts": []}')
    state = hi.watch_downloads(exp, folder=folder, since=0, timeout_sec=30, poll_sec=5, sleep=sleep,
                               clock=lambda: t["now"])
    assert state["files"]["conversations-000.zip"]["state"] == "imported"
    assert state["files"]["projects-000.zip"]["state"] == "missing"
    assert "expired" in state["files"]["projects-000.zip"]["detail"] and state["status"] == "incomplete"
    assert state["distil"]["processed"] == 2
    assert hi.progress()["watch"]["status"] == "incomplete"


def test_import_from_manifest_windows_opens_links_and_never_logs_urls(monkeypatch, clock):
    opened, spawned = [], []
    monkeypatch.setattr(hi, "open_url", lambda u: opened.append(u))
    monkeypatch.setattr(hi, "spawn_watcher", lambda files, since: spawned.append(files) or True)
    monkeypatch.setattr(hi.time, "sleep", lambda s: None)
    r = hi.import_from_manifest(json.dumps(MANIFEST), open_links=True)
    assert opened == [MANIFEST["data_files"][0]["export_url"], MANIFEST["data_files"][2]["export_url"]]
    assert spawned == [["light_metadata-000.zip", "conversations-000.zip"]] and r["watching"]
    assert r["rejected"][0]["filename"] == "projects-000.zip"
    logged = json.dumps([e.to_dict() for e in db.list_events(limit=50)])
    assert "sig=" not in logged and "claude.ai/export" not in logged


def test_import_from_manifest_elsewhere_lists_links_and_tool_hides_them(monkeypatch, clock):
    monkeypatch.setattr(hi, "open_url", lambda u: pytest.fail("must not open"))
    r = hi.import_from_manifest(json.dumps(MANIFEST), open_links=False)
    assert [x["filename"] for x in r["links"]] == ["light_metadata-000.zip", "conversations-000.zip"]
    tools.load_extensions()
    ctx = tools.ToolContext()
    monkeypatch.setattr(hi.os, "name", "posix")
    res = tools.dispatch("history_import_from_manifest", {"manifest": json.dumps(MANIFEST)}, ctx,
                         allowed={"history_import_from_manifest"})
    assert res.ok and "sig=" not in res.content and "light_metadata-000.zip" in res.content


def test_expired_manifest_does_not_open(monkeypatch, clock):
    clock.advance(hours=48)
    monkeypatch.setattr(hi, "open_url", lambda u: pytest.fail("must not open"))
    r = hi.import_from_manifest(json.dumps(MANIFEST), open_links=True)
    assert r["expired"] and not r["opened"] and any("new export" in n for n in r["notes"])
