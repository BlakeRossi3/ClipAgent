"""home.py staff pictures: the owner's own avatar per staff member (upload, validation, reset, serving)
and that every place showing a portrait then shows his picture."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

import config
import home
import server
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
WEBP = b"RIFF\x20\x00\x00\x00WEBPVP8 " + b"\x00" * 40


@pytest.fixture
def client() -> TestClient:
    return TestClient(server.create_app(), base_url="http://testserver")


def _csrf(client: TestClient) -> str:
    m = re.search(r'name="csrf" value="([0-9a-f]+)"', client.get("/dashboard/staff").text)
    assert m, "no csrf field on the staff page"
    return m.group(1)


def _upload(client, name, data, *, csrf, filename="me.png", json=True):
    headers = {"Accept": "application/json"} if json else {}
    return client.post(f"/dashboard/agents/{name}/avatar", data={"csrf": csrf},
                       files={"image": (filename, data, "image/png")}, headers=headers, follow_redirects=False)


def test_upload_shows_everywhere_and_reset_restores_the_drawing(client):
    page = client.get("/dashboard/staff").text
    assert "Change picture" in page and "Reset to drawing" not in page
    r = _upload(client, "studio", PNG, csrf=_csrf(client))
    assert r.status_code == 200 and r.json()["ok"], r.text
    path = home.avatar_path("studio")
    assert path is not None and path.parent == config.get_settings().data_dir / "avatars" and path.suffix == ".png"
    assert path.read_bytes() == PNG
    # served from the data dir with a safe content type
    got = client.get("/dashboard/avatars/studio")
    assert got.status_code == 200 and got.content == PNG
    assert got.headers["content-type"] == "image/png" and got.headers["x-content-type-options"] == "nosniff"
    # every portrait of that agent is now his picture (staff page, home roster, top-bar menu)
    assert 'src="/dashboard/avatars/studio?v=' in home.sprite_svg("studio", size=40)
    staff = client.get("/dashboard/staff").text
    assert 'src="/dashboard/avatars/studio?v=' in staff and "Reset to drawing" in staff
    assert 'src="/dashboard/avatars/studio?v=' in client.get("/dashboard/").text
    # a new picture of another kind replaces the old one
    assert _upload(client, "studio", WEBP, csrf=_csrf(client), filename="x.webp").json()["ok"]
    assert home.avatar_path("studio").suffix == ".webp" and not (path.parent / "studio.png").exists()
    # reset -> the drawing again (redirect back to the staff page without JS)
    r = client.post("/dashboard/agents/studio/avatar/reset", data={"csrf": _csrf(client)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/dashboard/staff?msg=avatar_reset")
    assert home.avatar_path("studio") is None and 'viewBox="0 0 64 64"' in home.sprite_svg("studio")
    assert client.get("/dashboard/avatars/studio").status_code == 404


def test_rejects_non_images_and_oversize(client):
    csrf = _csrf(client)
    r = _upload(client, "ledger", b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
                csrf=csrf, filename="evil.png")
    assert r.status_code == 400 and not r.json()["ok"]
    assert _upload(client, "ledger", b"", csrf=csrf).status_code == 400
    big = PNG + b"\x00" * (home.AVATAR_MAX_BYTES + 10)
    assert _upload(client, "ledger", big, csrf=csrf).status_code in (400, 413)
    assert home.avatar_path("ledger") is None
    # without JS: a redirect back with a warning
    r = _upload(client, "ledger", b"GIF00", csrf=csrf, json=False)
    assert r.status_code == 303 and "msg=avatar_bad" in r.headers["location"]


def test_csrf_auth_and_path_safety(client, monkeypatch):
    assert _upload(client, "studio", PNG, csrf="0" * 64).status_code == 403
    csrf = _csrf(client)
    for bad in ("..", "..%2F..%2Fetc", "nobody-here", "Studio", "a.b"):
        r = _upload(client, bad, PNG, csrf=csrf)
        assert r.status_code in (400, 404, 405), bad
    assert not list((config.get_settings().data_dir / "avatars").glob("*")) if \
        (config.get_settings().data_dir / "avatars").exists() else True
    for bad in ("../config", "..", "studio.png", "%2e%2e", "..%2F..%2Fconfig.env"):
        got = client.get(f"/dashboard/avatars/{bad}")
        assert got.status_code == 404 or not got.headers["content-type"].startswith("image/"), bad
    with pytest.raises(ValueError):
        home.save_avatar("../x", PNG)
    assert home.avatar_path("../../etc/passwd") is None and home.reset_avatar("../x") is False
    # the routes are owner-only
    monkeypatch.setenv("DASHBOARD_TOKEN", "a-test-token-value")
    config.reset_settings()
    assert client.get("/dashboard/avatars/studio").status_code == 401
    assert client.post("/dashboard/agents/studio/avatar/reset", data={"csrf": csrf}).status_code == 401
