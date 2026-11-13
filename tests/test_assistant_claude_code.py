"""Claude Code full-power runs: argv, permissions, sessions, images, usage, stop switch (fake runner only)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

import db
import notify
from assistant import claude_code, runtime, store, tools, usage
from assistant import settings as asettings
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")


def _result(text="Done, sir.", session="sess-1", **extra):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": text,
                       "session_id": session, "num_turns": 2, "total_cost_usd": 0.01,
                       "usage": {"input_tokens": 1000, "output_tokens": 50}, **extra})


def _rate(util=0.8, kind="five_hour", status="allowed_warning", resets=None):
    resets = resets or int((db.utcnow() + timedelta(hours=2)).timestamp())
    return json.dumps({"type": "rate_limit_event", "uuid": "u", "session_id": "s",
                       "rate_limit_info": {"status": status, "resetsAt": resets, "rateLimitType": kind,
                                           "utilization": util}})


class Runner:
    """Fake ``claude -p``: records each call (argv, stdin, generated files) and replays outputs."""

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, argv, *, input, env, cwd, timeout):
        files = {}
        for flag in ("--settings", "--mcp-config", "--agents"):
            if flag in argv:
                files[flag] = json.loads(Path(argv[argv.index(flag) + 1]).read_text(encoding="utf-8"))
        self.calls.append({"argv": argv, "input": input, "files": files, "cwd": cwd, "timeout": timeout})
        out = self.outputs.pop(0) if self.outputs else _result()
        if callable(out):
            out = out(argv)
        if isinstance(out, claude_code.Completed):
            return out
        return claude_code.Completed(0, out + "\n", "")


@pytest.fixture
def cc(monkeypatch, clock):
    monkeypatch.setenv("ASSISTANT_ENABLED", "true")
    monkeypatch.setenv("ASSISTANT_BACKEND", "claude_code")
    monkeypatch.setenv("FAKE_ASSISTANT", "false")
    asettings.reset_assistant_settings()
    monkeypatch.setattr(claude_code, "find_claude", lambda: Path("claude"))
    r = Runner()
    claude_code.set_runner(r)
    yield r
    claude_code.set_runner(None)
    tools.set_url_opener(None)


def test_permission_rules_deny_secrets_and_destructive_commands():
    perms = claude_code.permission_settings()
    p = perms["permissions"]
    assert p["defaultMode"] == "dontAsk" and perms["env"]["CLAUDE_CODE_USE_POWERSHELL_TOOL"] == "1"
    for rule in ("Read(//**/config.env)", "Edit(//**/config.env)", "Read(//**/*token*.json)",
                 "Read(//**/client_secret*.json)", f"Read({claude_code.rule_path(claude_code.home_dir())}/**)",
                 "Bash(rm -rf *)", "Bash(git push *)", "PowerShell(Remove-Item * -Recurse*)",
                 "PowerShell(git push *)"):
        assert rule in p["deny"], rule
    app = claude_code.rule_path(claude_code.REPO_ROOT)
    assert f"Edit({app}/**)" in p["allow"] and "Bash" in p["allow"] and "Read" in p["allow"]
    assert "Edit" not in p["allow"]          # edits only inside the working folders
    assert claude_code.TOOL_PREFIX + "*" in p["allow"]
    assert claude_code.rule_path(r"C:\Users\b\Desktop") == "//c/Users/b/Desktop"


def test_full_argv_shape():
    argv = claude_code.build_full_argv("claude", model="opus", system="persona", mcp_config="m.json",
                                       settings_path="s.json", agents_path="a.json", add_dirs=["/d", "/u"],
                                       resume="abc", effort="medium", max_turns=40, chrome=False)
    assert argv[:2] == ["claude", "-p"] and "--tools" not in argv and "--system-prompt" not in argv
    joined = " ".join(argv)
    for part in ("--output-format stream-json --verbose", "--append-system-prompt persona",
                 "--permission-mode dontAsk", "--settings s.json", "--setting-sources user", "--agents a.json",
                 "--add-dir /d --add-dir /u", "--resume abc", "--no-chrome", "--max-turns 40"):
        assert part in joined, part
    assert "--chrome" in claude_code.build_full_argv("c", model="m", system="s", mcp_config=None, settings_path="s",
                                                     agents_path=None, add_dirs=[], resume=None, effort=None,
                                                     max_turns=2, chrome=True)


def test_dashboard_turn_full_powers_and_session_resume_per_channel(cc):
    cc.outputs = [_result(session="d1"), _result(session="d1"), _result(session="s1")]
    r1 = runtime.run_turn("hello", channel="dashboard")
    assert r1.status == "ok" and r1.text.startswith("Done")
    first = cc.calls[0]
    assert "--resume" not in first["argv"] and first["cwd"] == str(claude_code.REPO_ROOT)
    assert "--add-dir" in first["argv"] and first["timeout"] == asettings.get_assistant_settings().cc_task_timeout_sec
    allowed = first["files"]["--mcp-config"]["mcpServers"]["clipagent"]["env"]["CLIPAGENT_MCP_ALLOWED"].split(",")
    assert {"memory_search", "open_url", "usage_status"} <= set(allowed) and "delegate" not in allowed
    assert "researcher" in first["files"]["--agents"] and "main" not in first["files"]["--agents"]
    assert first["files"]["--settings"]["permissions"]["defaultMode"] == "dontAsk"
    system = first["argv"][first["argv"].index("--append-system-prompt") + 1]
    assert "signed-in browser" in system and "never extract" in system.lower() and "CLAUDE.md" in system
    assert (claude_code.home_dir() / "skills" / "weekly-hours" / "SKILL.md").is_file()

    runtime.run_turn("and again", channel="dashboard")
    assert cc.calls[1]["argv"][cc.calls[1]["argv"].index("--resume") + 1] == "d1"
    runtime.run_turn("by text", channel="sms", deliver=True)
    assert "--resume" not in cc.calls[2]["argv"]
    assert claude_code.session_state("dashboard")["turns"] == 2


def test_session_rolls_over_daily_with_journal_summary(cc, clock):
    cc.outputs = [_result(session="old"), _result(session="new")]
    runtime.run_turn("remember the blue folder", channel="dashboard")
    clock.advance(days=1)
    runtime.run_turn("morning", channel="dashboard")
    assert "--resume" not in cc.calls[1]["argv"]
    assert claude_code.session_state("dashboard")["id"] == "new"
    from assistant import vault

    hits = vault.search("blue folder")
    assert hits and any("Journal" in h.path for h in hits)


def test_unknown_session_starts_fresh(cc):
    db.kv_set(claude_code.KV_SESSION.format(channel="dashboard"),
              {"id": "gone", "day": db.local_today().isoformat(), "turns": 1, "tokens": 10, "started": db.now_iso()})
    cc.outputs = [claude_code.Completed(1, "", "No conversation found with session ID: gone"), _result(session="n")]
    res = runtime.run_turn("hi", channel="dashboard")
    assert res.status == "ok" and "--resume" in cc.calls[0]["argv"] and "--resume" not in cc.calls[1]["argv"]


def test_images_are_passed_as_paths_for_the_read_tool(cc):
    img = claude_code.uploads_dir() / ("a" * 32 + ".png")
    img.write_bytes(b"\x89PNG\r\n\x1a\nxx")
    runtime.run_turn("what is this?", channel="dashboard", attachments=[str(img)])
    assert str(img) in cc.calls[0]["input"] and "Read tool" in cc.calls[0]["input"]
    assert store.list_messages(channel="dashboard", direction="in")[0].meta == {"attachments": [img.name]}


def test_stop_switch_blocks_every_run(cc):
    runtime.set_stopped(True, via="test")
    res = runtime.run_turn("hi", channel="dashboard")
    assert res.status == "stopped" and not cc.calls and "START STERLING" in res.text
    assert notify.is_pipeline_command("stop sterling") and notify.is_pipeline_command("UNDO")
    assert "Started" in notify.handle_sms_command("START STERLING") and not runtime.is_stopped()
    assert "Stopped" in notify.handle_sms_command("stop  sterling") and runtime.is_stopped()
    assert "nothing to undo" in notify.handle_sms_command("UNDO").lower() or "not a git" in notify.handle_sms_command("UNDO")


def test_open_url_tool(cc):
    opened = []
    tools.set_url_opener(lambda u: opened.append(u) or True)
    ctx = tools.ToolContext(agent="main")
    ok = tools.dispatch("open_url", {"url": "https://www.youtube.com/watch?v=x"}, ctx, allowed={"open_url"})
    assert ok.ok and opened == ["https://www.youtube.com/watch?v=x"]
    bad = tools.dispatch("open_url", {"url": "javascript:alert(1)"}, ctx, allowed={"open_url"})
    assert not bad.ok and len(opened) == 1


def test_rate_limit_events_feed_usage_status_and_deferral(cc):
    cc.outputs = [_rate(0.85) + "\n" + _rate(0.4, kind="seven_day", status="allowed") + "\n" + _result()]
    runtime.run_turn("hi", channel="dashboard")
    u = usage.usage_status()
    assert u["known"] and u["windows"]["five_hour"]["left_pct"] == 15 and u["windows"]["seven_day"]["left_pct"] == 60
    assert u["runs_today"] == 1 and u["windows"]["five_hour"]["resets_text"]
    assert usage.constrained()[0] is True
    did = usage.should_defer("reel", "Deep-dive the latest reel.")
    assert did and store.get_deferred(did).after_reset
    tools.load_extensions()
    res = tools.dispatch("usage_status", {}, tools.ToolContext(agent="main"), allowed={"usage_status"})
    assert res.ok and "five_hour" in res.content


def test_plan_limit_queues_owner_request_and_runs_it_after_reset(cc, clock):
    cc.outputs = [claude_code.Completed(1, "", "You've hit your session limit · resets 4pm")]
    res = runtime.run_turn("summarise my week", channel="dashboard")
    assert "resets" in res.text and "I'll do this then" in res.text
    q = store.list_deferred("queued")
    assert len(q) == 1 and q[0].kind == "owner_request" and q[0].channel == "dashboard"
    assert usage.run_due() == 0                      # still limited
    clock.advance(hours=7)                           # past 4pm New York
    cc.outputs = [_result("Your week: fine.", session="x")]
    assert usage.run_due() == 1
    assert store.get_deferred(q[0].id).status == "done"
    assert "summarise my week" in cc.calls[-1]["input"]
    assert store.list_messages(channel="dashboard", direction="out")[0].body == "Your week: fine."


def test_schedule_prompt_tool(cc, clock):
    tools.load_extensions()
    ctx = tools.ToolContext(agent="main")
    res = tools.dispatch("schedule_prompt", {"text": "Check the reel stats", "at": "2026-11-13 18:30"}, ctx,
                         allowed={"schedule_prompt"})
    assert res.ok
    d = store.list_deferred("queued")[0]
    assert not d.after_reset and d.run_after == "2026-11-13T23:30:00Z"
    assert usage.due() == []
    clock.advance(hours=9)
    assert [x.id for x in usage.due()] == [d.id]
    past = tools.dispatch("schedule_prompt", {"text": "x", "at": "2020-01-01 10:00"}, ctx, allowed={"schedule_prompt"})
    assert not past.ok
