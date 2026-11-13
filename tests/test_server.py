from __future__ import annotations

import sys

from fastapi.testclient import TestClient

import db
import server


def test_app_routes_healthz_and_lifespan_migrates(settings):
    assert db.schema_version() == 0
    with TestClient(server.create_app()) as client:
        body = client.get("/healthz").json()
        assert body == {"status": "ok", "schema_version": db.SCHEMA_VERSION, "dashboard": True}
        assert client.get("/", follow_redirects=False).headers["location"] == "/dashboard/"
        # notify's webhooks are mounted (DRY_RUN without a Twilio token accepts unsigned posts)
        assert client.post("/twilio/sms", data={"From": "+15550000000", "Body": "STATUS"}).status_code == 200
        assert client.post("/twilio/status", data={"CallSid": "CAx"}).status_code == 200
        assert client.get("/dashboard/").status_code != 404


def test_app_starts_without_a_broken_dashboard(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "dashboard", None)          # import dashboard -> ImportError
    app = server.create_app()
    assert "dashboard unavailable" in caplog.text
    with TestClient(app) as client:
        assert client.get("/healthz").json()["dashboard"] is False
        assert client.get("/dashboard/").status_code == 404
        assert client.get("/", follow_redirects=False).headers["location"] == "/healthz"
