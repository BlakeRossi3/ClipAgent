"""assistant/hud.py: maps (Nominatim faked), the SSRF-guarded image proxy, panel kinds, the ui_* tools,
and the Jarvis page endpoints that serve them (/voice/api/img, /diagnostics, /timing)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import db
from assistant import events, hud, panels, tools
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
JS = (Path(__file__).resolve().parent.parent / "static" / "jarvis.js").read_text(encoding="utf-8")


class FakeNet:
    """Stands in for the internet: url -> Fetched (or a callable). Records every request."""

    def __init__(self, routes=None, dns=None):
        self.routes = dict(routes or {})
        self.dns = dict(dns or {})
        self.calls: list[tuple[str, dict]] = []

    def http(self, url, headers, timeout, max_bytes):
        self.calls.append((url, dict(headers)))
        for prefix, res in self.routes.items():
            if url.startswith(prefix):
                return res(url) if callable(res) else res
        return hud.Fetched(404, {"content-type": "text/plain"}, b"nope")

    def resolve(self, host):
        return self.dns.get(host, ["93.184.216.34"])

    def install(self):
        hud.set_http(self.http)
        hud.set_resolver(self.resolve)
        return self


def nominatim(rows):
    return hud.Fetched(200, {"content-type": "application/json"}, json.dumps(rows).encode())


def call(name, **args):
    tools.load_extensions()
    ctx = tools.ToolContext(channel="voice", agent="main")
    return tools.dispatch(name, args, ctx, allowed={name})


def last_panel():
    return [e for e in events.BUS.since(0, 1000) if e["type"] == "panel"][-1]["panel"]


# ---------------------------------------------------------------------------
# show_map + geocoding
# ---------------------------------------------------------------------------

def test_show_map_geocodes_with_a_proper_user_agent_caches_and_publishes(monkeypatch):
    net = FakeNet({hud.NOMINATIM_URL: nominatim([{"lat": "51.5007", "lon": "-0.1246", "addresstype": "city",
                                                    "display_name": "Big Ben, London, UK"}])}).install()
    sleeps = []
    monkeypatch.setattr(hud.time, "sleep", sleeps.append)
    res = call("show_map", query="Big Ben")
    assert res.ok, res.content
    out = json.loads(res.content)
    assert out["on_screen"] and out["lat"] == 51.5007 and out["zoom"] == 12
    url, headers = net.calls[0]
    assert headers["User-Agent"] == "ClipAgent/1.0 (personal assistant)" and "q=big+ben" in url
    p = last_panel()
    assert p["kind"] == "map" and p["map"]["markers"][0]["label"].startswith("Big Ben") and p["hold"] == "sticky"
    # cached in kv: the second ask makes no request at all
    assert call("show_map", query="  big   BEN ").ok and len(net.calls) == 1
    # a different place within the second waits out Nominatim's one-request-a-second limit
    assert call("show_map", query="Tower Bridge").ok and len(net.calls) == 2 and sleeps and sleeps[0] <= 1.0


def test_show_map_coordinates_markers_and_refusals():
    net = FakeNet({hud.NOMINATIM_URL: nominatim([])}).install()
    assert call("show_map", lat="40.7128", lon=-74.006, zoom=11, markers=[{"lat": 40.75, "lon": -73.99, "label": "Office"}]).ok
    p = last_panel()
    assert p["map"]["zoom"] == 11 and p["map"]["markers"] == [{"lat": 40.75, "lon": -73.99, "label": "Office"}]
    assert not net.calls                                  # coordinates need no geocoder
    r = call("show_map", query="Nowhereville Zzz")
    assert not r.ok and "No place called" in r.content
    assert not call("show_map", lat=95, lon=0).ok and not call("show_map").ok
    assert db.kv_get("hud:geo:nowhereville zzz") == {}    # misses are cached too


# ---------------------------------------------------------------------------
# the image proxy: allowlist + SSRF gate
# ---------------------------------------------------------------------------

TILE = "https://tile.openstreetmap.org/12/1205/1539.png"


def test_tiles_and_allowlisted_hosts_pass_and_are_cached():
    net = FakeNet({"https://tile.openstreetmap.org/": hud.Fetched(200, {"content-type": "image/png"}, PNG)}).install()
    assert hud.fetch_remote(TILE) == ("image/png", PNG)
    assert hud.fetch_remote(TILE) == ("image/png", PNG) and len(net.calls) == 1        # cached
    assert net.calls[0][1]["User-Agent"].startswith("ClipAgent/1.0")
    for bad in ("https://tile.openstreetmap.org/../../etc/passwd", "https://tile.openstreetmap.org/12/1/2.png?x=1",
                "https://tile.openstreetmap.org/admin"):
        with pytest.raises(hud.ProxyError):
            hud.fetch_remote(bad)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/x.png", "http://127.1/x.png", "http://localhost/x.png", "http://printer.local/x.png",
    "file:///etc/passwd", "ftp://example.com/x.png", "gopher://example.com/", "data:image/png;base64,AAAA",
    "http://10.0.0.5/x.png", "http://192.168.1.1/x.png", "http://172.16.0.1/x.png", "http://169.254.169.254/latest",
    "http://100.64.0.1/x.png", "http://[::1]/x.png", "http://[::ffff:127.0.0.1]/x.png", "http://[fe80::1]/x.png",
    "http://0.0.0.0/x.png", "http://2130706433/x.png", "http://0x7f.1/x.png", "http://user:pw@i.ytimg.com/vi/x.jpg",
    "http://i.ytimg.com:8080/vi/x.jpg", "http://8.8.8.8.nip.io.127/x.png", "javascript:alert(1)", "",
])
def test_ssrf_attempts_are_refused(url):
    net = FakeNet().install()
    hud.allow_url(url)              # even if the model put it in a panel
    with pytest.raises(hud.ProxyError):
        hud.fetch_remote(url)
    assert not net.calls


def test_dns_answers_and_redirects_are_checked_on_every_hop():
    net = FakeNet(dns={"i.ytimg.com": ["10.1.2.3"]}).install()
    with pytest.raises(hud.ProxyError) as e:
        hud.fetch_remote("https://i.ytimg.com/vi/abc/hq.jpg")       # an allowed name resolving to the LAN
    assert e.value.status == 403 and not net.calls
    net = FakeNet({"https://upload.wikimedia.org/a.jpg": hud.Fetched(302, {"location": "http://127.0.0.1:8000/x"}),
                   "https://upload.wikimedia.org/b.jpg": hud.Fetched(301, {"location": "file:///etc/passwd"}),
                   "https://upload.wikimedia.org/c.jpg": hud.Fetched(302, {"location": "https://cdn.example.org/c.jpg"}),
                   "https://cdn.example.org/c.jpg": hud.Fetched(200, {"content-type": "image/jpeg"}, b"jpg"),
                   "https://upload.wikimedia.org/loop.jpg": hud.Fetched(302, {"location": "/loop.jpg"})},
                  dns={"evil-rebind.example.org": ["127.0.0.1"]}).install()
    for u in ("https://upload.wikimedia.org/a.jpg", "https://upload.wikimedia.org/b.jpg"):
        with pytest.raises(hud.ProxyError):
            hud.fetch_remote(u)
    assert hud.fetch_remote("https://upload.wikimedia.org/c.jpg") == ("image/jpeg", b"jpg")   # public hop is fine
    with pytest.raises(hud.ProxyError, match="redirect"):
        hud.fetch_remote("https://upload.wikimedia.org/loop.jpg")
    net.routes["https://upload.wikimedia.org/d.jpg"] = hud.Fetched(302, {"location": "https://evil-rebind.example.org/"})
    with pytest.raises(hud.ProxyError):
        hud.fetch_remote("https://upload.wikimedia.org/d.jpg")


def test_content_type_and_size_are_enforced_and_panel_urls_are_allowed():
    FakeNet({"https://i.ytimg.com/html": hud.Fetched(200, {"content-type": "text/html"}, b"<script>"),
             "https://i.ytimg.com/svg": hud.Fetched(200, {"content-type": "image/svg+xml"}, b"<svg/>"),
             "https://i.ytimg.com/big": hud.Fetched(200, {"content-type": "image/png"}, b"x" * (hud.MAX_IMAGE_BYTES + 1)),
             "https://news.example.com/pic.jpg": hud.Fetched(200, {"content-type": "image/jpeg"}, b"jpeg")}).install()
    for u, status in (("https://i.ytimg.com/html", 415), ("https://i.ytimg.com/svg", 415), ("https://i.ytimg.com/big", 413)):
        with pytest.raises(hud.ProxyError) as e:
            hud.fetch_remote(u)
        assert e.value.status == status
    with pytest.raises(hud.ProxyError):
        hud.fetch_remote("https://news.example.com/pic.jpg")       # not allowed until a panel shows it
    panels.make_panel({"title": "News", "html": '<img class="hud-img" src="https://news.example.com/pic.jpg">'})
    assert hud.fetch_remote("https://news.example.com/pic.jpg") == ("image/jpeg", b"jpeg")


# ---------------------------------------------------------------------------
# panel kinds (sanitised on both sides)
# ---------------------------------------------------------------------------

def test_media_panels():
    for u in ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3", "https://youtu.be/dQw4w9WgXcQ",
              "https://www.youtube.com/shorts/dQw4w9WgXcQ", "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"):
        assert hud.youtube_id(u) == "dQw4w9WgXcQ", u
    for u in ("https://www.youtube.com/watch?v=<script>", "https://evil.com/watch?v=dQw4w9WgXcQ", "javascript:x"):
        assert hud.youtube_id(u) is None, u
    assert call("show_media", url="https://youtu.be/dQw4w9WgXcQ", caption="Rick").ok
    p = last_panel()
    assert p["kind"] == "youtube" and p["video_id"] == "dQw4w9WgXcQ" and "url" not in p
    assert call("show_media", url="https://upload.wikimedia.org/a/b/Cat.jpg").ok
    p = last_panel()
    assert p["kind"] == "image" and p["src"] == "/voice/api/img?u=https%3A%2F%2Fupload.wikimedia.org%2Fa%2Fb%2FCat.jpg"
    assert call("show_media", url="https://cdn.example.org/clip.mp4").ok and last_panel()["src"].startswith("/voice/api/media?u=")
    for bad in ({"url": "http://127.0.0.1/x.png"}, {"url": "file:///etc/passwd"}, {"url": "ftp://x.org/a.png"},
                {"url": "https://example.org/x", "kind": "youtube"}, {"clip_id": 999}):
        r = call("show_media", **bad)
        assert not r.ok, bad


def test_data_and_list_panels_are_trimmed_and_markup_panels_keep_their_kind():
    assert call("show_data", title="This week", pairs=[{"label": "Hours", "value": 38}, ["Pay", "$912"]] * 20).ok
    p = last_panel()
    assert p["kind"] == "data" and len(p["rows"]) == 16 and p["rows"][0] == ["Hours", "38"]
    assert call("show_data", title="Todo", items=["one", {"label": "two", "sub": "s", "tag": "T", "x": 1}]).ok
    p = last_panel()
    assert p["kind"] == "list" and p["items"][1] == {"label": "two", "sub": "s", "tag": "T"}
    assert not call("show_data", title="x").ok
    assert panels.display({"title": "Note", "html": "<b>hi</b>", "size": "blade"}) == "On screen."
    p = last_panel()
    assert p["kind"] == "markup" and p["size"] == "blade"


def test_browser_renderer_validates_every_kind():
    """static/jarvis.js renders kinds with its own code and re-checks each value (no model markup for them)."""
    assert "YT_ID = /^[A-Za-z0-9_-]{11}$/" in JS and "youtube-nocookie.com/embed/" in JS
    assert re.search(r"LOCAL_MEDIA = /\^\\/voice\\/api\\/\(img\|media\)", JS)
    assert "proxyImg('https://tile.openstreetmap.org/'" in JS and "el.src = /^https?:/i.test(s) ? proxyImg(s) : s" in JS
    assert "innerHTML" not in JS.replace("d.innerHTML", "")   # only the debugging helper reads innerHTML


# ---------------------------------------------------------------------------
# ui_* tools
# ---------------------------------------------------------------------------

def test_ui_tools_persist_and_emit_events():
    after = events.BUS.seq
    assert call("ui_theme", accent="crimson").ok
    assert call("ui_ring", style="radar", color="#0f0", scale="1.3", spin=0).ok
    assert call("ui_rails", show="hide").ok
    st = hud.ui_state()
    assert st == {"accent": "#dc143c", "ring": {"style": "radar", "color": "#00ff00", "scale": 1.3, "spin": 0.0},
                  "rails": False}
    assert db.kv_get(hud.KV_UI)["accent"] == "#dc143c"
    ui = [e for e in events.BUS.since(after, 100) if e["type"] == "ui"]
    assert len(ui) == 3 and ui[-1]["ui"]["rails"] is False
    assert call("ui_effect", effect="glitch").ok and events.BUS.since(0, 1000)[-1]["effect"] == "glitch"
    assert db.kv_get(hud.KV_UI)["ring"]["style"] == "radar"            # effects are not persisted
    for bad in (("ui_theme", {"accent": "url(javascript:x)"}), ("ui_theme", {"accent": "#12"}),
                ("ui_ring", {"style": "spiral"}), ("ui_ring", {"scale": 9}), ("ui_effect", {"effect": "explode"})):
        assert not call(bad[0], **bad[1]).ok, bad
    assert call("ui_theme", accent="auto").ok and hud.ui_state()["accent"] is None
    assert call("ui_reset").ok and hud.ui_state() == hud.DEFAULT_UI and db.kv_get(hud.KV_UI) == hud.DEFAULT_UI


# ---------------------------------------------------------------------------
# the endpoints
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    import server

    return TestClient(server.create_app(), base_url="http://127.0.0.1:8000")


def _csrf(c):
    return re.search(r'name="csrf" value="([0-9a-f]+)"', c.get("/dashboard/").text).group(1)


def test_img_endpoint_proxies_and_refuses(client):
    FakeNet({"https://tile.openstreetmap.org/": hud.Fetched(200, {"content-type": "image/png"}, PNG)}).install()
    r = client.get("/voice/api/img", params={"u": TILE})
    assert r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff" and "sandbox" in r.headers["content-security-policy"]
    for u, code in (("http://127.0.0.1:8000/healthz", 403), ("file:///etc/passwd", 400), ("https://evil.example.com/a.png", 403)):
        assert client.get("/voice/api/img", params={"u": u}).status_code == code, u


def test_home_page_csp_allows_only_the_youtube_frame_and_proxied_images(client):
    csp = client.get("/dashboard/").headers["content-security-policy"]
    assert "frame-src https://www.youtube-nocookie.com" in csp and "img-src 'self' data: blob:" in csp
    assert "https:" not in csp.split("img-src")[1].split(";")[0]
    other = client.get("/dashboard/events").headers["content-security-policy"]
    assert "frame-src" not in other


def test_activity_diagnostics_and_timing(client):
    hud.ui_theme("teal")
    a = client.get("/voice/api/activity").json()
    assert a["ui"]["accent"] == "#14b8a6" and a["voice"]["stt"] == "auto"
    d = client.get("/voice/api/diagnostics").json()
    assert {"brain", "voice", "stt", "timings", "mcp_servers", "laptop"} <= set(d) and d["stt"] == "auto"
    assert client.post("/voice/api/timing", data={"first_word_ms": "900"}).status_code == 403   # CSRF
    after = events.BUS.seq
    r = client.post("/voice/api/timing", data={"csrf": _csrf(client), "turn": "t1", "first_word_ms": "812",
                                               "stt": "browser", "ask_ms": "40"})
    assert r.json()["ok"]
    e = [x for x in events.BUS.since(after, 100) if x["type"] == "timing"][-1]
    assert e["timing"]["speech_to_first_word_ms"] == 812 and e["timing"]["stt"] == "browser"
    assert client.get("/voice/api/diagnostics").json()["timings"][-1]["turn"] == "t1"


def test_rendered_short_is_served_only_from_renders_dir(client, make_clip, tmp_path):
    import config

    rd = config.get_settings().renders_dir
    rd.mkdir(parents=True, exist_ok=True)
    good = rd / "7.mp4"
    good.write_bytes(b"\0\0\0\x18ftypmp42")
    cid, bad = make_clip(status="rendered"), make_clip(status="rendered")
    db.update_clip(cid, render_path=str(good))
    db.update_clip(bad, render_path=str(tmp_path / "secret.mp4"))
    (tmp_path / "secret.mp4").write_bytes(b"x")
    assert client.get(f"/voice/api/clip/{cid}").status_code == 200
    assert client.get(f"/voice/api/clip/{bad}").status_code == 404
    assert call("show_media", clip_id=cid).ok and last_panel()["src"] == f"/voice/api/clip/{cid}"
