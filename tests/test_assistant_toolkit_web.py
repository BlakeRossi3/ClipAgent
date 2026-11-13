"""assistant.toolkit_web: the Desktop modes / Claude history / Twilio check pages render and act."""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
import dashboard
from assistant import desktop_modes as dm
from assistant import history_import as hi
from assistant import toolkit_web
from assistant import toolkit_settings as ts
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")
H = {"Authorization": "Bearer tok-123456"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "tok-123456")
    config.reset_settings()
    ts.reset_toolkit_settings()
    hi._reset_migration_cache()
    app = FastAPI()
    app.include_router(dashboard.router)
    app.include_router(toolkit_web.router)
    yield TestClient(app, base_url="http://testserver")
    dm.set_runner(None)


def test_pages_render_and_need_auth(client):
    for path in ("/dashboard/modes", "/dashboard/history", "/dashboard/twilio-check"):
        assert client.get(path).status_code == 401
        r = client.get(path, headers=H)
        assert r.status_code == 200, path
    assert "Gaming" in client.get("/dashboard/modes", headers=H).text


def test_modes_run_and_save(client, monkeypatch):
    monkeypatch.setattr(dm, "is_windows", lambda: False)
    r = client.post("/dashboard/modes/run", data={"name": "Gaming"}, headers=H)
    assert r.status_code == 200 and "plan only" in r.text
    r = client.post("/dashboard/modes/save", data={"yaml": "modes:\n  - name: X\n    steps:\n      - shell: dir\n"}, headers=H)
    assert r.status_code == 400 and "Not saved" in r.text
    r = client.post("/dashboard/modes/save", data={"yaml": "modes:\n  - name: Focus\n    steps:\n      - wait: 1\n"}, headers=H)
    assert r.status_code == 200 and "Focus" in r.text


def test_history_upload_and_manifest(client, monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("conversations.json", json.dumps([{"uuid": "u1", "name": "Trip", "created_at": "2026-01-01T00:00:00Z",
                                                      "chat_messages": [{"sender": "human", "text": "hello"}]}]))
    r = client.post("/dashboard/history/upload", files=[("files", ("conversations-000.zip", buf.getvalue()))], headers=H)
    assert r.status_code == 200 and "imported" in r.text and hi.progress()["conversations"] == 1
    monkeypatch.setattr(hi.os, "name", "posix")
    man = {"total_files": 1, "created_at": None, "data_files": [{"filename": "projects-000.zip",
                                                                 "export_url": "https://claude.ai/export/x/p.zip"}]}
    r = client.post("/dashboard/history/manifest", data={"manifest": json.dumps(man)}, headers=H)
    assert r.status_code == 200 and "projects-000.zip" in r.text and "https://claude.ai/export/x/p.zip" in r.text
    assert client.post("/dashboard/history/manifest", data={"manifest": "nope"}, headers=H).status_code == 400
    assert client.get("/dashboard/history/status.json", headers=H).json()["conversations"] == 1


def test_twilio_check_runs_offline(client):
    r = client.post("/dashboard/twilio-check/run", headers=H)
    assert r.status_code == 200 and "Twilio settings missing" in r.text
