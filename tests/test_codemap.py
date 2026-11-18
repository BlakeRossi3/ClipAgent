"""codemap: the generated page of where everything lives and who reads what (zero tokens)."""

from __future__ import annotations

import os
from pathlib import Path

import codemap


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "app"
    (root / "assistant").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "db.py").write_text('"""Database helpers."""\n'
                                'def recent_views(day):\n    return 0\n'
                                'def kv_get(k, d=None):\n    return d\n'
                                'MIGRATIONS = ["CREATE TABLE IF NOT EXISTS stats (day TEXT)"]\n', encoding="utf-8")
    (root / "home.py").write_text('"""The home page."""\nimport db\n'
                                  'def tile():\n    return db.recent_views(1) + db.kv_get("x:y:z", 0)\n'
                                  'SQL = "SELECT * FROM stats"\n', encoding="utf-8")
    (root / "assistant" / "__init__.py").write_text("", encoding="utf-8")
    (root / "assistant" / "status.py").write_text('"""Spoken status."""\nimport db\nfrom datetime import date\n'
                                                  'def line():\n    return db.recent_views(2)\n'
                                                  'KV = "x:y:{n}"\nQ = "SELECT day FROM stats"\n',
                                                  encoding="utf-8")
    (root / "main.py").write_text('"""CLI and jobs."""\nfrom fastapi import APIRouter\n'
                                  'router = APIRouter(prefix="/dashboard")\n'
                                  'def job_specs():\n    return [JobSpec("live_counts", f, "cron", {"minute": "*/10"}, '
                                  '"live counts"), JobSpec("plain", f, "interval", {"minutes": 5})]\n'
                                  '@router.get("/studio")\ndef studio():\n    pass\n', encoding="utf-8")
    (root / "tests" / "test_x.py").write_text("import db\ndb.recent_views(3)\n", encoding="utf-8")
    return root


def test_map_lists_every_reader_of_shared_data(tmp_path):
    md = codemap.build(_repo(tmp_path))
    assert "- `db.recent_views` - assistant.status, home" in md          # the "fix every reader" list
    assert "- `stats` - assistant.status, home" in md                  # SQL table, not `from datetime`
    assert "datetime" not in md.split("## Modules")[0]
    assert "db.kv_get" not in md                                           # plumbing is not data
    assert "- `x:y:*` - assistant.status, home" in md                      # kv key prefixes
    assert "`live_counts` - cron {'minute': '*/10'} - live counts" in md
    assert "`plain` - interval {'minutes': 5}" in md and "`/dashboard/studio`" in md
    assert "- `home` - The home page." in md and "tests" not in md.split("## Modules")[1]


def test_ensure_fresh_rebuilds_only_when_code_changed(tmp_path):
    root = _repo(tmp_path)
    out = codemap.ensure_fresh(root)
    first = out.stat().st_mtime
    assert not codemap.stale(root) and codemap.ensure_fresh(root).stat().st_mtime == first
    os.utime(out, (first - 60, first - 60))                                # a source file is now newer
    assert codemap.stale(root)
    codemap.ensure_fresh(root)
    assert not codemap.stale(root)


def test_real_app_map_is_small_and_names_the_view_readers(tmp_path):
    md = codemap.build()
    assert len(md) < 60_000                                                # cheap to read (~10k tokens)
    line = next(x for x in md.splitlines() if x.startswith("- `db.recent_views`"))
    assert "home" in line and "home_live" in line
    codemap.set_output(tmp_path / "m.md")
    assert codemap.write() == tmp_path / "m.md" and (tmp_path / "m.md").is_file()
