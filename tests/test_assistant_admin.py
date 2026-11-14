"""assistant/admin.py: Sterling as administrator - read everything, change everything, never a secret."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone

import pytest

import config
import config_store
import db
from assistant import admin, ledger, registry, store, tools, vault
from assistant import settings as asettings
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")

SECRETS = {
    "ANTHROPIC_API_KEY": "sk-ant-api03-SEEDEDsecretVALUE1234567890",
    "TWILIO_AUTH_TOKEN": "twilioauthtoken0123456789abcdef",
    "TWILIO_ACCOUNT_SID": "AC" + "0123456789abcdef" * 2,  # built at runtime: a literal trips GitHub secret scanning,
    "DASHBOARD_TOKEN": "dash-token-seeded-123456",
    "ELEVENLABS_API_KEY": "eleven-seeded-key-abcdef123456",
    "YOUTUBE_API_KEY": "yt-api-key-seeded-987654321",
}
CSRF = "csrf-seeded-secret-abcdef0123456789"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPAGENT_CONFIG_FILE", str(tmp_path / "config.env"))
    config_store.reload()
    asettings.reset_assistant_settings()
    return tmp_path


@pytest.fixture
def staff_dir(tmp_path, monkeypatch):
    d = tmp_path / "agents"
    shutil.copytree(registry.AGENTS_DIR, d, ignore=shutil.ignore_patterns(".archive"))
    monkeypatch.setattr(registry, "AGENTS_DIR", d)
    return d


def _call(name, args=None, *, channel="dashboard"):
    tools.available([name])
    ctx = tools.ToolContext(channel=channel, agent="main")
    return tools.dispatch(name, args or {}, ctx, allowed=set(admin.TOOL_NAMES))


def _table_names():
    with db.transaction() as c:
        return {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                             "AND name NOT LIKE 'sqlite_%'")}


# --- map ---------------------------------------------------------------------------------------------

def test_app_map_covers_every_table_and_section(cfg):
    admin._ensure_tables()
    with db.transaction() as c:
        c.execute("CREATE TABLE brand_new_thing (id INTEGER PRIMARY KEY, label TEXT)")
    m = admin.app_map()
    mapped = {t["table"] for t in m["tables"]}
    assert _table_names() <= mapped and "brand_new_thing" in mapped        # generated, never stale
    holds = {t["table"]: t["holds"] for t in m["tables"]}
    for name in ("sources", "clips", "uploads", "as_messages", "as_income", "as_jobs", "kv", "events"):
        assert "no description" not in holds[name], name
    assert set(m) == set(admin.MAP_SECTIONS)
    assert m["config"]["DRY_RUN"]["value"] in ("true", "false")
    assert m["config"]["ANTHROPIC_API_KEY"] == {"secret": True, "set": False}
    assert "main" in m["staff"]["agents"] and m["vault"]["notes"] >= 0
    assert any("logs" in x for x in m["files"]["logs"])
    r = _call("app_map")
    assert r.ok and "as_messages" in r.content and "truncated" not in r.content
    r = _call("app_map", {"section": "config", "filter": "VOICE"})
    assert r.ok and "VOICE_TTS" in r.content and "DRY_RUN" not in r.content


# --- read --------------------------------------------------------------------------------------------

def test_app_query_is_read_only_single_statement_and_redacts(cfg):
    gid = store.add_goal("Save 5k", target=5000)
    db.kv_set("dashboard:csrf_secret", CSRF)
    db.kv_set("scout:topic_cursor", 3)
    with db.transaction() as c:
        c.execute("CREATE TABLE vendor_keys (id INTEGER PRIMARY KEY, vendor TEXT, api_key TEXT)")
        c.execute("INSERT INTO vendor_keys (vendor, api_key) VALUES ('acme', 'acme-very-secret-1234')")
    r = admin.app_query("SELECT id, title FROM as_goals WHERE id = ?", [gid])
    assert r["rows"] == [{"id": gid, "title": "Save 5k"}]
    kv = {row["key"]: row["value"] for row in admin.app_query("SELECT * FROM kv")["rows"]}
    assert kv["dashboard:csrf_secret"] == admin.REDACTED and kv["scout:topic_cursor"] == "3"
    sneaky = admin.app_query("SELECT hex(value) AS h, substr(value, 2) AS s FROM kv WHERE key LIKE '%csrf%'")
    assert CSRF.encode().hex().upper() not in json.dumps(sneaky) and CSRF[1:] not in json.dumps(sneaky)
    assert admin.app_query("SELECT vendor, api_key FROM vendor_keys")["rows"] == [{"vendor": "acme", "api_key": None}]
    for bad in ("DELETE FROM as_goals", "UPDATE as_goals SET title='x'", "INSERT INTO kv VALUES ('a','1','x')",
                "SELECT 1; DELETE FROM as_goals", "PRAGMA writable_schema=1", "ATTACH DATABASE 'x' AS y",
                "SELECT * FROM main.kv", "WITH x AS (SELECT 1) DELETE FROM as_goals", "DROP TABLE as_goals"):
        with pytest.raises(admin.AdminError):
            admin.app_query(bad)
    assert store.get_goal(gid) is not None
    big = admin.app_query("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i < 2000) "
                          "SELECT i FROM n", limit=10)
    assert big["row_count"] == 10 and big["truncated"]
    r = _call("app_query", {"sql": "DELETE FROM as_goals"})
    assert not r.ok and "SELECT" in r.content


def test_app_search_and_conversation_history(cfg, clock):
    clock.set(datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc))
    store.add_message("in", "sms", "Remind me what we decided about the blue van")
    store.add_message("out", "sms", "You chose the blue van, sir, for the Saturday shoots.")
    clock.set(datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc))
    store.add_message("in", "voice", "How did the shorts do today?")
    db.log_event("pick", "blue van clip picked")
    hist = admin.conversation_history(30, query="blue van")
    assert hist["matches"][0]["date"] == "2026-11-02" and {m["who"] for m in hist["matches"]} == {"Blake", "Sterling"}
    assert hist["conversations"][0]["channel"] == "sms"
    recent = admin.conversation_history(3)
    assert [c["channel"] for c in recent["conversations"]] == ["voice"]
    assert admin.conversation_history(0)["period"] == "all time"
    s = admin.app_search("blue van")
    assert s["found"]["messages"] == 2 and s["found"]["events"] == 1
    r = _call("conversation_history", {"days": 0, "query": "blue van"})
    assert r.ok and "2026-11-02" in r.content


# --- change ------------------------------------------------------------------------------------------

def test_config_set_live_restart_refusals_and_undo(cfg, monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    config_store.reload()
    assert config.get_settings().dry_run is True
    res = admin.config_set("dry_run", False)
    assert res["changed"] == {"DRY_RUN": "false"} and res["applies"] == "now"
    assert config.get_settings().dry_run is False                    # live
    assert admin.config_set("MAX_UPLOADS_PER_DAY", "12")["changed"]["MAX_UPLOADS_PER_DAY"] == "12"
    assert config.get_settings().max_uploads_per_day == 12
    assert admin.config_set("ASSISTANT_DAILY_BUDGET_USD", "$40")["changed"]["ASSISTANT_DAILY_BUDGET_USD"] == "40.00"
    assert asettings.get_assistant_settings().daily_budget_usd == 40
    assert "HEARTBEAT_MINUTES" in admin.config_set("HEARTBEAT_MINUTES", 15)["restart_needed"]
    assert "warning" in admin.config_set("TIMEZONE", "Europe/London")         # env var overrides the file
    for key in ("ANTHROPIC_API_KEY", "twilio_auth_token", "TWILIO_ACCOUNT_SID", "DASHBOARD_TOKEN",
                "ELEVENLABS_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "YOUTUBE_TOKEN_PATH", "YOUTUBE_CLIENT_SECRETS"):
        with pytest.raises(admin.AdminError):
            admin.config_set(key, "x")
    with pytest.raises(admin.AdminError, match="true or false"):
        admin.config_set("DRY_RUN", "maybe")
    with pytest.raises(admin.AdminError, match="negative"):
        admin.config_set("MAX_UPLOADS_PER_DAY", "-3")
    with pytest.raises(admin.AdminError):
        admin.config_set("QUIET_HOURS_START", "25:99")
    with pytest.raises(admin.AdminError, match="unknown"):
        admin.config_set("NOT_A_REAL_SETTING", "1")
    v = config_store.read_values()
    assert "ANTHROPIC_API_KEY" not in v and v["MAX_UPLOADS_PER_DAY"] == "12"
    assert admin.config_set("voice", "Lewis")["now"] == "Kokoro Lewis"            # friendly names too
    admin.app_undo(res["change_id"])
    assert "DRY_RUN" not in config_store.read_values() and config.get_settings().dry_run is True
    r = _call("config_set", {"key": "ANTHROPIC_API_KEY", "value": "sk-x"})
    assert not r.ok and "secret" in r.content


def test_pay_log_shows_in_ledger_summary_and_undo(cfg):
    before = ledger.summary(weeks=1)["month_to_date"]["job_gross"]
    r = _call("pay_log", {"amount": 812.5, "note": "paycheck"})
    assert r.ok, r.content
    out = json.loads(r.content)
    s = ledger.summary(weeks=1)
    assert s["month_to_date"]["job_gross"] == pytest.approx(before + 812.5)
    admin.pay_log(40, source="other", note="sold a lamp")
    assert ledger.summary(weeks=1)["month_to_date"]["other_income"] == pytest.approx(40)
    edited = admin.income_edit(out["income_id"], {"amount": 800})
    assert store.list_income(source="job")[-1].amount_usd == 800 and edited["change_id"]
    admin.app_undo(edited["change_id"])
    assert store.list_income(source="job")[-1].amount_usd == 812.5
    admin.app_undo(out["change_id"])
    assert ledger.summary(weeks=1)["month_to_date"]["job_gross"] == pytest.approx(before)
    with pytest.raises(admin.AdminError):
        admin.pay_log(-5)
    with pytest.raises(admin.AdminError):
        admin.pay_log(5, source="lottery")


def test_hours_log_job_set_and_income_delete(cfg):
    j = admin.job_set("Warehouse", rate=20, schedule="every other week", tax=20)
    assert j["job"]["hourly_rate"] == 20 and j["job"]["pay_schedule"] == "biweekly" and j["job"]["est_tax_rate"] == 0.2
    h = admin.hours_log(10)
    assert h["gross"] == 200
    h2 = admin.hours_log(12)
    admin.app_undo(h2["change_id"])
    assert store.list_work_log()[0].hours == 10 and store.list_income(source="job")[0].amount_usd == 200
    admin.app_undo(h["change_id"])
    assert store.list_work_log() == [] and store.list_income(source="job") == []
    renamed = admin.job_set("Warehouse", rename="Depot", active=True)
    assert renamed["job"]["name"] == "Depot"
    iid = admin.pay_log(100)["income_id"]
    d = admin.income_delete(iid)
    assert all(i.id != iid for i in store.list_income())
    admin.app_undo(d["change_id"])
    assert any(i.id == iid for i in store.list_income())


def test_app_update_delete_and_undo(cfg):
    gid = store.add_goal("Run 5k", target=5)
    r = _call("app_update", {"table": "as_goals", "id": gid, "fields": {"title": "Run 10k", "target": "10"}})
    assert r.ok, r.content
    cid = json.loads(r.content)["change_id"]
    g = store.get_goal(gid)
    assert g.title == "Run 10k" and g.target == 10.0
    assert _call("app_undo", {"change_id": cid}).ok
    assert store.get_goal(gid).title == "Run 5k"
    # delete with cascade: a job and its hours come back together
    job = ledger.set_job("Cafe", hourly_rate=15)
    ledger.log_hours(8, job="Cafe")
    d = admin.app_delete("as_jobs", job.id)
    assert d["linked_rows"] >= 1 and store.list_work_log() == []
    admin.app_undo(d["change_id"])
    assert store.get_job(job.id).name == "Cafe" and store.list_work_log()[0].hours == 8
    # state machines, refusals
    sid = db.add_source("https://example.gov/v1", "nasa", licence="us_gov_public_domain")
    with pytest.raises(admin.AdminError, match="force"):
        admin.app_update("sources", sid, {"status": "done"})
    admin.app_update("sources", sid, {"status": "done"}, force=True)
    assert db.get_source(sid).status == "done"
    for bad in (("as_vault_fts", 1, {"body": "x"}), ("as_goals", gid, {"nope": 1}), ("as_goals", gid, {"id": 9}),
                ("no_table", 1, {"a": 1}), ("as_goals", 99999, {"title": "x"})):
        with pytest.raises(admin.AdminError):
            admin.app_update(*bad)
    db.kv_set("dashboard:csrf_secret", CSRF)
    with pytest.raises(admin.AdminError, match="secret"):
        admin.app_update("kv", "dashboard:csrf_secret", {"value": "x"})
    with pytest.raises(admin.AdminError, match="secret"):
        admin.app_delete("kv", "dashboard:csrf_secret")
    assert [c["kind"] for c in admin.list_changes(3)][0] == "update"


def test_staff_add_rename_update_delete_restore(cfg, staff_dir):
    def valid():
        return registry.load_agents(staff_dir, skills=registry.load_skills(),
                                    known_tools=tools.known_tool_names())

    valid()
    a = admin.staff_add("Penny Pincher", role="Budget coach", description="Spending advice and budgets.",
                        prompt="You help the owner spend less.", tools=["ledger_summary", "memory_read"])
    assert a["added"] == "penny-pincher" and (staff_dir / "penny-pincher.md").is_file()
    spec = valid()["penny-pincher"]
    assert spec.extra["role"] == "Budget coach" and spec.extra["display_name"] == "Penny Pincher"
    r = admin.staff_rename("ledger", "Bookie")
    assert not (staff_dir / "ledger.md").exists() and (staff_dir / "bookie.md").is_file()
    assert "main.md" in r["references_updated"]
    agents = valid()
    assert "bookie" in agents and "ledger" not in agents and agents["bookie"].extra["role"] == "Bookkeeper"
    assert "- bookie:" in (staff_dir / "main.md").read_text() and "ledger_summary" in (staff_dir / "main.md").read_text()
    admin.staff_update("bookie", {"description": "The books.", "add_tools": ["pay_log"], "remove_tools": ["goals_update"]})
    b = valid()["bookie"]
    assert b.description == "The books." and "pay_log" in b.tools and "goals_update" not in b.tools
    with pytest.raises(admin.AdminError, match="put them back"):
        admin.staff_update("bookie", {"skills": ["no-such-skill"]})
    assert valid()["bookie"].skills == ("weekly-hours",)                              # rolled back
    with pytest.raises(admin.AdminError):
        admin.staff_update("bookie", {"tools": ["not_a_tool"]})
    d = admin.staff_delete("bookie")
    assert "bookie" not in valid() and list((staff_dir / ".archive").glob("*-bookie.md"))
    with pytest.raises(admin.AdminError):
        admin.staff_delete("main")
    with pytest.raises(admin.AdminError):
        admin.staff_rename("main", "boss")
    admin.staff_restore("bookie")
    assert "bookie" in valid() and not list((staff_dir / ".archive").glob("*-bookie.md"))
    admin.app_undo()                                       # undo the restore
    assert "bookie" not in valid()
    admin.app_undo(d["change_id"])                         # undo the delete
    admin.app_undo(r["change_id"])                         # ...and the rename restores ledger + main.md
    assert "ledger" in valid() and "- ledger:" in (staff_dir / "main.md").read_text()
    r = _call("staff_list")
    assert r.ok and "penny-pincher" in r.content


def test_memory_write_delete_and_heartbeat_protected(cfg):
    vault.ensure_vault()
    w = admin.memory_write("People/Sam.md", "# Sam\n- Brother, lives in Leeds")
    assert "Leeds" in vault.read_note("People/Sam.md").body
    admin.memory_write("People/Sam.md", "# Sam\n- Lives in York now")
    assert "York" in vault.read_note("People/Sam.md").body and "Leeds" not in vault.read_note("People/Sam.md").body
    admin.memory_write("People/Sam.md", "- Likes chess", mode="append", heading="Notes")
    d = admin.memory_delete("People/Sam.md")
    assert not vault.note_exists("People/Sam.md")
    admin.app_undo(d["change_id"])
    assert "chess" in vault.read_note("People/Sam.md").body
    with pytest.raises(admin.AdminError):
        admin.memory_write("Agent/Heartbeat.md", "hacked")
    with pytest.raises(admin.AdminError):
        admin.memory_delete("Agent/Heartbeat.md")
    assert w["change_id"]


# --- secrets ------------------------------------------------------------------------------------------

def test_no_tool_ever_returns_a_secret(cfg, monkeypatch, staff_dir):
    cfg_file = cfg / "config.env"
    cfg_file.write_text("".join(f"{k}={v}\n" for k, v in SECRETS.items()), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-env-seeded-token-555666777")
    config_store.reload()
    secrets = list(SECRETS.values()) + [CSRF, "oauth-env-seeded-token-555666777", "voice-local-key-seeded-42"]
    db.kv_set("dashboard:csrf_secret", CSRF)
    db.kv_set("voice:local_key", "voice-local-key-seeded-42")
    db.log_event("setup", "oops " + SECRETS["ANTHROPIC_API_KEY"], data={"leak": SECRETS["TWILIO_AUTH_TOKEN"]})
    store.add_message("in", "sms", "my dashboard password is " + SECRETS["DASHBOARD_TOKEN"])
    store.add_goal("token " + SECRETS["ELEVENLABS_API_KEY"])
    gid = store.add_goal("plain goal")
    calls = {
        "app_map": [{}, *({"section": s} for s in admin.MAP_SECTIONS)],
        "app_query": [{"sql": "SELECT * FROM kv"}, {"sql": "SELECT * FROM events"}, {"sql": "SELECT * FROM as_messages"},
                      {"sql": "SELECT key, upper(value) AS v, hex(value) AS h FROM kv"}, {"sql": "SELECT * FROM as_goals"}],
        "app_search": [{"text": "oops"}, {"text": "password"}, {"text": "token"}, {"text": "secret"}, {"text": "key"}],
        "conversation_history": [{"days": 0}, {"days": 0, "query": "password"}],
        "config_get": [{}, {"filter": "KEY"}, {"filter": "TOKEN"}],
        "config_set": [{"key": "ANTHROPIC_API_KEY", "value": "x"}, {"key": "DRY_RUN", "value": "true"}],
        "staff_list": [{}],
        "app_changes": [{}],
        "app_update": [{"table": "as_goals", "id": gid, "fields": {"title": "plain goal 2"}}],
        "pay_log": [{"amount": 10, "note": "x"}],
        "app_undo": [{}],
    }
    assert set(admin.TOOL_NAMES) - set(calls) <= {"hours_log", "job_set", "income_edit", "income_delete", "staff_add",
                                                  "staff_rename", "staff_update", "staff_delete", "staff_restore",
                                                  "memory_write", "memory_delete", "app_delete"}
    outputs = []
    for name, arglist in calls.items():
        for args in arglist:
            outputs.append(_call(name, args).content)
    for name in ("settings_get", "studio_status", "memory_search", "lessons_list", "goals_list"):
        tools.available([name])
        outputs.append(tools.dispatch(name, {"query": "x"} if name == "memory_search" else {},
                                      tools.ToolContext(), allowed={name}).content)
    blob = "\n".join(outputs)
    assert "oops" in blob and admin.REDACTED in blob            # the rows are visible, the values are not
    for s in secrets:
        assert s not in blob, s
        assert s.encode().hex().upper() not in blob
    m = admin.app_map("config")["config"]
    assert m["ANTHROPIC_API_KEY"] == {"secret": True, "set": True} and m["CLAUDE_CODE_OAUTH_TOKEN"]["set"] is True


def test_secret_definitions():
    for k in ("ANTHROPIC_API_KEY", "TWILIO_AUTH_TOKEN", "TWILIO_ACCOUNT_SID", "DASHBOARD_TOKEN", "ELEVENLABS_API_KEY",
              "CLAUDE_CODE_OAUTH_TOKEN", "some_password", "dashboard:csrf_secret", "voice:local_key", "refresh_token"):
        assert admin.is_secret_key(k), k
    for k in ("DRY_RUN", "input_tokens", "output_tokens", "ASSISTANT_MAX_TOKENS", "key", "ELEVENLABS_VOICE_ID",
              "YOUTUBE_TOKEN_PATH", "call_sid", "OWNER_PHONE", "sha256"):
        assert not admin.is_secret_key(k), k
    assert admin.redact("x sk-ant-abcdefghijk123 y") == f"x {admin.REDACTED} y"
    assert set(admin.TOOL_NAMES) <= set(tools.PLANNED_TOOLS)
    main = registry.load_agents(skills=registry.load_skills())["main"]
    assert set(admin.TOOL_NAMES) <= set(main.tools) and "app-admin" in main.skills
    assert registry.validate_all(tools.known_tool_names()) == []
