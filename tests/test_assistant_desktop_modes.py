"""assistant.desktop_modes: parsing, validation, argv building, dry-run, injected runner."""

from __future__ import annotations

import pytest

from assistant import desktop_modes as dm
from assistant import toolkit_settings as ts
from assistant import tools, vault
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env", "clean")


@pytest.fixture
def clean():
    ts.reset_toolkit_settings()
    yield
    dm.set_runner(None)
    ts.reset_toolkit_settings()


def test_default_note_parses_and_has_the_three_modes():
    modes = dm.load_modes()
    assert [m.name for m in modes] == ["Gaming", "Work", "Wind down"]
    g = dm.find_mode("gaming mode")
    acts = [s.action for s in g.steps]
    assert "launch" in acts and "close" in acts and "power_plan" in acts
    assert vault.note_exists(dm.NOTE_PATH)


@pytest.mark.parametrize("step,err", [
    ({"shell": "rm -rf /"}, "one action per step"),
    ({"launch": "evil.bat"}, "scripts"),
    ({"launch": "file:///C:/x.exe"}, "not allowed"),
    ({"launch": "Steam\"; Remove-Item"}, "not allowed"),
    ({"close": "explorer"}, "not allowed"),
    ({"close": "a;b"}, "process name"),
    ({"open_url": "javascript:alert(1)"}, "http"),
    ({"power_plan": "turbo"}, "power_plan"),
    ({"volume": 150}, "0-100"),
    ({"wait": 999}, "0-120"),
    ({"notifications": "maybe"}, "on or off"),
    ({"launch": "Steam", "run_as": "admin"}, "unknown key"),
])
def test_validation_refuses(step, err):
    with pytest.raises(dm.ModeError) as ei:
        dm.parse_step(step, "x")
    assert err in str(ei.value)


def test_parse_modes_reports_all_errors():
    text = "```yaml\nmodes:\n  - name: A\n    steps:\n      - volume: 500\n  - name: A\n    steps: []\n```"
    with pytest.raises(dm.ModeError) as ei:
        dm.parse_modes(text)
    assert "0-100" in str(ei.value) and ("duplicate" in str(ei.value) or "needs a list" in str(ei.value))


def test_argv_building_never_interpolates_values():
    s = dm.parse_step({"launch": "Steam", "args": ["-silent"]}, "x")
    argv, env = dm.build_command(s)
    assert argv[0] == "powershell.exe" and "Steam" not in " ".join(argv)
    assert env[dm.ARG_ENV] == "Steam" and env[dm.ARGS_ENV] == '["-silent"]'
    argv, env = dm.build_command(dm.parse_step({"close": "Discord.exe", "force": True}, "x"))
    assert env == {dm.ARG_ENV: "Discord", dm.ARGS_ENV: "force"}
    argv, _ = dm.build_command(dm.parse_step({"power_plan": "high performance"}, "x"))
    assert argv == ["powercfg.exe", "/setactive", dm.POWER_PLANS["high_performance"]]
    argv, _ = dm.build_command(dm.parse_step({"notifications": "off"}, "x"))
    assert argv[:2] == ["reg.exe", "add"] and argv[argv.index("/d") + 1] == "0"
    argv, env = dm.build_command(dm.parse_step({"volume": 40}, "x"))
    assert env[dm.ARG_ENV] == "40" and "SetMasterVolumeLevelScalar" in argv[-1]
    argv, _ = dm.build_command(dm.parse_step({"open_folder": "C:/Games"}, "x"))
    assert argv == ["explorer.exe", "C:/Games"]
    assert dm.build_command(dm.parse_step({"wait": 2}, "x")) is None


def test_dry_run_off_windows_plans_only(monkeypatch):
    monkeypatch.setattr(dm, "is_windows", lambda: False)
    r = dm.run_mode("Work")
    assert r.dry_run and r.ok and all(x.skipped for x in r.results) and "plan only" in r.reason


def test_runner_executes_in_order_and_optional_failures_do_not_fail(monkeypatch):
    seen = []

    def runner(argv, env):
        seen.append((argv[0], env.get(dm.ARG_ENV)))
        if argv[0] == "powercfg.exe":
            return 1, "Invalid Parameters"
        return 0, ""
    dm.set_runner(runner, sleep=lambda s: None)
    monkeypatch.setattr(dm, "is_windows", lambda: True)
    r = dm.run_mode("Gaming")
    assert not r.dry_run and r.ok  # power plan failure is optional in the default
    pp = next(x for x in r.results if x.step.startswith("power plan"))
    assert not pp.ok and "Balanced" in pp.detail
    assert ("powershell.exe", "Steam") in seen
    assert [a for a, _ in seen].index("powercfg.exe") < [v for _, v in seen].index("Steam")


def test_save_yaml_validates_and_keeps_note_prose():
    body, sha = dm.read_note_text()
    with pytest.raises(dm.ModeError):
        dm.save_yaml("modes:\n  - name: X\n    steps:\n      - shell: dir\n")
    modes = dm.save_yaml("modes:\n  - name: Focus\n    steps:\n      - notifications: 'off'\n      - wait: 1\n")
    assert [m.name for m in modes] == ["Focus"]
    new = vault.read_note(dm.NOTE_PATH).body
    assert "# Desktop modes" in new and "Focus" in new and "Gaming" not in new


def test_tools(monkeypatch):
    monkeypatch.setattr(dm, "is_windows", lambda: False)
    tools.load_extensions()
    ctx = tools.ToolContext()
    res = tools.dispatch("desktop_modes_list", {}, ctx, allowed={"desktop_modes_list"})
    assert res.ok and "Gaming" in res.content
    res = tools.dispatch("desktop_mode_run", {"name": "wind down"}, ctx, allowed={"desktop_mode_run"})
    assert res.ok and '"dry_run": true' in res.content
    res = tools.dispatch("desktop_mode_run", {"name": "party"}, ctx, allowed={"desktop_mode_run"})
    assert not res.ok and "No mode" in res.content
