"""Desktop modes: one click (or one text) to set the owner's Windows PC up for gaming, work, winding down.

Modes live in an editable vault note, ``Agent/Desktop Modes.md``, as a fenced ``yaml`` block::

    modes:
      - name: Gaming
        description: Steam + Discord, high performance
        steps:
          - close: ms-teams
          - launch: Steam
          - power_plan: high_performance

Steps come from a **fixed vocabulary** - there is no way to run an arbitrary command from a mode
file. Each step becomes an argv list (never a shell string); values reach PowerShell through an
environment variable, never by string interpolation, so a mode file cannot inject code.

=============  ==========================================================================
step           meaning
=============  ==========================================================================
launch         app by Start-menu name ("Steam"), exe path, ``shell:AppsFolder\\<AppID>`` or a
               URI such as ``steam://open/main``; optional ``args`` (list of strings)
close          process name ("Discord", "ms-teams", "OUTLOOK"); asks the window to close,
               ``force: true`` also ends it if it is still running 5 s later
open_folder    a folder path (``~`` and ``%VARS%`` expanded)
open_url       an http(s) link in the default browser
power_plan     ``balanced`` | ``high_performance`` | ``power_saver`` | ``ultimate`` | a plan GUID
volume         master volume 0-100
mute           true / false
notifications  ``off`` | ``on`` - Windows' "Get notifications from apps" switch (Do Not Disturb-ish)
wait           seconds (0-120)
=============  ==========================================================================

Any step may add ``optional: true`` (a failure is reported but not counted). Steps run in order
and a failed step never stops the rest. Armoury Crate profiles (Silent / Performance / Turbo) have
no command-line interface; see skills/desktop-modes/SKILL.md for what to do instead.

Windows only. Elsewhere (or with ``DESKTOP_MODES_DRY_RUN=true`` / ``dry_run=True``) a run returns
the plan without executing anything. Tools ``desktop_modes_list`` / ``desktop_mode_run``; CLI
``python -m assistant.desktop_modes list | show <name> | run <name> [--dry-run] | init``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import db

MODULE = "assistant.desktop_modes"
NOTE_PATH = "Agent/Desktop Modes.md"
STEP_TIMEOUT_SEC = 60
MAX_MODES = 20
MAX_STEPS = 40
ARG_ENV = "CLIPAGENT_STEP_ARG"
ARGS_ENV = "CLIPAGENT_STEP_ARGS"

POWER_PLANS = {
    "balanced": "381b4222-f694-41f0-9685-ff5bb260df2e",
    "high_performance": "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",
    "power_saver": "a1841308-3542-4f4b-a7b5-ba5ce2b7a09d",
    "ultimate": "e9a42b02-d5df-448d-aa00-03f14749eb61",
}
ACTIONS = ("launch", "close", "open_folder", "open_url", "power_plan", "volume", "mute", "notifications", "wait")
PROTECTED_PROCESSES = frozenset({
    "explorer", "svchost", "csrss", "winlogon", "wininit", "lsass", "services", "smss", "system", "dwm",
    "python", "pythonw", "py", "powershell", "pwsh", "cmd", "conhost", "clipagent", "claude", "taskmgr",
    "fontdrvhost", "sihost", "ctfmon", "runtimebroker", "searchhost", "startmenuexperiencehost",
})
_PROC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")
_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_URI_RE = re.compile(r"^[a-z][a-z0-9+.-]{1,30}:", re.I)
_BAD_CHARS = re.compile(r"[\x00-\x1f\"`|<>]")
_BLOCKED_SCHEMES = {"file", "javascript", "data", "vbscript", "ms-msdt", "search-ms", "ms-officecmd", "powershell", "cmd"}

PS = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"]

# PowerShell bodies are constants; the only variable input is $env:CLIPAGENT_STEP_ARG(S).
PS_LAUNCH = r"""
$n = $env:CLIPAGENT_STEP_ARG
$a = @(); if ($env:CLIPAGENT_STEP_ARGS) { $a = @(ConvertFrom-Json $env:CLIPAGENT_STEP_ARGS) }
if (Test-Path -LiteralPath $n) {
  if ($a.Count) { Start-Process -FilePath $n -ArgumentList $a } else { Start-Process -FilePath $n }
} elseif ($n -match '^[A-Za-z][A-Za-z0-9+.-]+:') { Start-Process -FilePath $n }
else {
  $apps = Get-StartApps
  $app = $apps | Where-Object { $_.Name -eq $n } | Select-Object -First 1
  if (-not $app) { $app = $apps | Where-Object { $_.Name -like ('*' + [WildcardPattern]::Escape($n) + '*') } | Select-Object -First 1 }
  if (-not $app) { Write-Output "not installed or not in the Start menu: $n"; exit 3 }
  Start-Process -FilePath ('shell:AppsFolder\' + $app.AppID)
}
""".strip()

PS_CLOSE = r"""
$n = $env:CLIPAGENT_STEP_ARG
$p = Get-Process -Name $n -ErrorAction SilentlyContinue
if (-not $p) { Write-Output "not running"; exit 0 }
$p | ForEach-Object { [void]$_.CloseMainWindow() }
if ($env:CLIPAGENT_STEP_ARGS -eq 'force') {
  Start-Sleep -Seconds 5
  Get-Process -Name $n -ErrorAction SilentlyContinue | Stop-Process -ErrorAction SilentlyContinue
}
""".strip()

PS_OPEN = r"Start-Process -FilePath $env:CLIPAGENT_STEP_ARG"

# Master volume through the Core Audio API (IAudioEndpointVolume); no third-party tool needed.
PS_VOLUME = r"""
Add-Type -TypeDefinition @'
using System.Runtime.InteropServices;
[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioEndpointVolume { int f(); int g(); int h(); int i();
  int SetMasterVolumeLevelScalar(float fLevel, System.Guid pguidEventContext); int j(); int k(); int l(); int m(); int n(); int o();
  int SetMute([MarshalAs(UnmanagedType.Bool)] bool bMute, System.Guid pguidEventContext); }
[Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice { int Activate(ref System.Guid id, int clsCtx, int activationParams, out IAudioEndpointVolume aev); }
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator { int f(); int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice endpoint); }
[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumeratorComObject { }
public class ClipAgentAudio {
  static IAudioEndpointVolume Vol() {
    var enumerator = new MMDeviceEnumeratorComObject() as IMMDeviceEnumerator;
    IMMDevice dev = null; Marshal.ThrowExceptionForHR(enumerator.GetDefaultAudioEndpoint(0, 1, out dev));
    IAudioEndpointVolume epv = null; var epvid = typeof(IAudioEndpointVolume).GUID;
    Marshal.ThrowExceptionForHR(dev.Activate(ref epvid, 23, 0, out epv)); return epv; }
  public static void SetVolume(float v) { Marshal.ThrowExceptionForHR(Vol().SetMasterVolumeLevelScalar(v, System.Guid.Empty)); }
  public static void SetMute(bool m) { Marshal.ThrowExceptionForHR(Vol().SetMute(m, System.Guid.Empty)); }
}
'@
if ($env:CLIPAGENT_STEP_ARGS -eq 'mute') { [ClipAgentAudio]::SetMute([bool]::Parse($env:CLIPAGENT_STEP_ARG)) }
else { [ClipAgentAudio]::SetMute($false); [ClipAgentAudio]::SetVolume([float]([int]$env:CLIPAGENT_STEP_ARG) / 100) }
""".strip()

NOTIFY_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\PushNotifications"

DEFAULT_NOTE = """# Desktop modes

{name} can set your PC up for what you are about to do. Edit the block below (Obsidian or the
dashboard's Desktop modes page), then say "gaming mode" or press the button. Steps run in order;
only these step types exist: launch, close, open_folder, open_url, power_plan, volume, mute,
notifications, wait. Add `optional: true` to a step that may not apply (an app you might not have).

```yaml
modes:
  - name: Gaming
    description: Close work apps, open Steam and Discord, full power, notifications off.
    steps:
      - close: ms-teams
        optional: true
      - close: olk          # new Outlook
        optional: true
      - close: OUTLOOK      # classic Outlook
        optional: true
      - power_plan: high_performance
        optional: true      # many laptops only have Balanced; set Armoury Crate to Turbo by hand
      - launch: Steam
      - launch: Discord
        optional: true
      - notifications: "off"
      - volume: 60
  - name: Work
    description: Balanced power, browser on your work sites, Outlook and Teams.
    steps:
      - close: steam
        optional: true
      - power_plan: balanced
      - notifications: "on"
      - open_url: https://outlook.office.com/mail/   # replace with your work sites
      - launch: Outlook
        optional: true
      - launch: Microsoft Teams
        optional: true
      - volume: 35
  - name: Wind down
    description: Close the noisy apps, quiet the PC.
    steps:
      - close: steam
        optional: true
      - close: Discord
        optional: true
      - close: ms-teams
        optional: true
      - power_plan: power_saver
        optional: true
      - notifications: "off"
      - volume: 20
```
"""


class ModeError(ValueError):
    """A mode file or request is invalid (message is safe to show)."""


@dataclass
class Step:
    action: str
    value: Any
    args: list[str] = field(default_factory=list)
    force: bool = False
    optional: bool = False

    def describe(self) -> str:
        v = self.value
        if self.action == "power_plan":
            return f"power plan: {v}"
        if self.action == "wait":
            return f"wait {v}s"
        if self.action == "close":
            return f"close {v}" + (" (force)" if self.force else "")
        return f"{self.action.replace('_', ' ')}: {v}"


@dataclass
class Mode:
    name: str
    description: str
    steps: list[Step]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "steps": [s.describe() for s in self.steps]}


@dataclass
class StepResult:
    step: str
    argv: list[str]
    ok: bool
    skipped: bool = False
    optional: bool = False
    detail: str = ""


@dataclass
class ModeRun:
    mode: str
    dry_run: bool
    results: list[StepResult]
    ok: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for r in d["results"]:
            r["argv"] = [a if len(a) < 120 else a[:60] + "…" for a in r["argv"]]  # the long PS bodies
        return d


# ---------------------------------------------------------------------------
# parsing + validation (pure)
# ---------------------------------------------------------------------------

def extract_yaml(text: str) -> str:
    """The first ```yaml fenced block in a note (or the whole text if it already is YAML)."""
    m = re.search(r"```ya?ml[^\n]*\n(.*?)```", text, re.S | re.I)
    if m:
        return m.group(1)
    if re.match(r"\s*modes\s*:", text):
        return text
    raise ModeError("No ```yaml block with `modes:` found in the Desktop Modes note.")


def _clean_text(v: Any, what: str, max_len: int = 260) -> str:
    s = str(v if v is not None else "").strip()
    if not s:
        raise ModeError(f"{what} is empty")
    if len(s) > max_len or _BAD_CHARS.search(s):
        raise ModeError(f"{what} has characters or a length that is not allowed: {s[:60]!r}")
    return s


def parse_step(raw: Any, where: str) -> Step:
    if not isinstance(raw, dict):
        raise ModeError(f"{where}: each step must be like `- launch: Steam`")
    keys = [k for k in raw if k in ACTIONS]
    extra = set(raw) - set(ACTIONS) - {"args", "force", "optional"}
    if len(keys) != 1:
        raise ModeError(f"{where}: one action per step, one of {', '.join(ACTIONS)} (got {sorted(raw)})")
    if extra:
        raise ModeError(f"{where}: unknown key(s) {sorted(extra)}; only {', '.join(ACTIONS)}, args, force, optional")
    action = keys[0]
    v = raw[action]
    optional = bool(raw.get("optional", False))
    args: list[str] = []
    if action == "launch":
        v = _clean_text(v, f"{where} launch")
        scheme = v.split(":", 1)[0].lower() if _URI_RE.match(v) and not re.match(r"^[A-Za-z]:[\\/]", v) else ""
        if scheme in _BLOCKED_SCHEMES:
            raise ModeError(f"{where}: launching {scheme}: links is not allowed")
        if v.lower().endswith((".bat", ".cmd", ".ps1", ".vbs", ".js", ".wsf", ".hta", ".msi", ".scr", ".reg")):
            raise ModeError(f"{where}: scripts and installers cannot be launched from a mode; use the app itself")
        raw_args = raw.get("args") or []
        if not isinstance(raw_args, list) or len(raw_args) > 10:
            raise ModeError(f"{where}: args must be a short list")
        args = [_clean_text(a, f"{where} argument", 200) for a in raw_args]
    elif action == "close":
        v = _clean_text(v, f"{where} close")
        v = re.sub(r"\.exe$", "", v, flags=re.I)
        if not _PROC_RE.match(v):
            raise ModeError(f"{where}: {v!r} is not a process name (like Discord or ms-teams)")
        if v.lower() in PROTECTED_PROCESSES:
            raise ModeError(f"{where}: closing {v} is not allowed")
    elif action == "open_folder":
        v = _clean_text(v, f"{where} open_folder")
        if _URI_RE.match(v) and not re.match(r"^[A-Za-z]:[\\/]", v):
            raise ModeError(f"{where}: open_folder takes a folder path, not a link")
    elif action == "open_url":
        v = _clean_text(v, f"{where} open_url", 2000)
        p = urlparse(v)
        if p.scheme not in ("http", "https") or not p.hostname:
            raise ModeError(f"{where}: open_url needs an http(s) link")
    elif action == "power_plan":
        v = _clean_text(v, f"{where} power_plan").lower().replace(" ", "_").replace("-", "_") \
            if not _GUID_RE.match(str(v).strip()) else str(v).strip().lower()
        if v not in POWER_PLANS and not _GUID_RE.match(v):
            raise ModeError(f"{where}: power_plan must be one of {', '.join(POWER_PLANS)} or a plan GUID")
    elif action == "volume":
        try:
            v = int(v)
        except (TypeError, ValueError):
            raise ModeError(f"{where}: volume must be a number 0-100") from None
        if not 0 <= v <= 100:
            raise ModeError(f"{where}: volume must be 0-100")
    elif action == "mute":
        if not isinstance(v, bool):
            raise ModeError(f"{where}: mute must be true or false")
    elif action == "notifications":
        v = {True: "on", False: "off"}.get(v, str(v).strip().lower()) if isinstance(v, bool) else str(v).strip().lower()
        if v not in ("on", "off"):
            raise ModeError(f"{where}: notifications must be on or off")
    elif action == "wait":
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ModeError(f"{where}: wait takes seconds") from None
        if not 0 <= v <= 120:
            raise ModeError(f"{where}: wait must be 0-120 seconds")
    return Step(action, v, args, bool(raw.get("force", False)) if action == "close" else False, optional)


def parse_modes(text: str) -> list[Mode]:
    """Parse + validate the note (or bare YAML). Raises :class:`ModeError` listing every problem."""
    import yaml

    try:
        data = yaml.safe_load(extract_yaml(text))
    except yaml.YAMLError as exc:
        raise ModeError(f"The YAML does not parse: {str(exc).splitlines()[0]}") from None
    if not isinstance(data, dict) or not isinstance(data.get("modes"), list):
        raise ModeError("The YAML must have a top-level `modes:` list.")
    if len(data["modes"]) > MAX_MODES:
        raise ModeError(f"At most {MAX_MODES} modes.")
    modes: list[Mode] = []
    errors: list[str] = []
    seen: set[str] = set()
    for i, m in enumerate(data["modes"], 1):
        if not isinstance(m, dict) or not str(m.get("name") or "").strip():
            errors.append(f"mode {i}: needs a name")
            continue
        name = str(m["name"]).strip()[:40]
        if name.lower() in seen:
            errors.append(f"mode {name}: duplicate name")
        seen.add(name.lower())
        raw_steps = m.get("steps") or []
        if not isinstance(raw_steps, list) or not raw_steps:
            errors.append(f"mode {name}: needs a list of steps")
            continue
        if len(raw_steps) > MAX_STEPS:
            errors.append(f"mode {name}: at most {MAX_STEPS} steps")
            continue
        steps = []
        for j, s in enumerate(raw_steps, 1):
            try:
                steps.append(parse_step(s, f"{name} step {j}"))
            except ModeError as exc:
                errors.append(str(exc))
        modes.append(Mode(name, str(m.get("description") or "").strip()[:200], steps))
    if errors:
        raise ModeError("; ".join(errors[:12]))
    return modes


# ---------------------------------------------------------------------------
# argv building (pure)
# ---------------------------------------------------------------------------

def _expand_path(p: str) -> str:
    return os.path.expandvars(os.path.expanduser(p))


def build_command(step: Step) -> tuple[list[str], dict[str, str]] | None:
    """(argv, extra env) for one step, or None for steps run in-process (wait)."""
    a = step.action
    if a == "launch":
        val = _expand_path(step.value) if not _URI_RE.match(step.value) or re.match(r"^[A-Za-z]:[\\/]", step.value) \
            else step.value
        return PS + [PS_LAUNCH], {ARG_ENV: val, ARGS_ENV: json.dumps(step.args) if step.args else ""}
    if a == "close":
        return PS + [PS_CLOSE], {ARG_ENV: step.value, ARGS_ENV: "force" if step.force else ""}
    if a == "open_folder":
        return ["explorer.exe", _expand_path(step.value)], {}
    if a == "open_url":
        return PS + [PS_OPEN], {ARG_ENV: step.value}
    if a == "power_plan":
        return ["powercfg.exe", "/setactive", POWER_PLANS.get(step.value, step.value)], {}
    if a == "volume":
        return PS + [PS_VOLUME], {ARG_ENV: str(int(step.value)), ARGS_ENV: ""}
    if a == "mute":
        return PS + [PS_VOLUME], {ARG_ENV: "true" if step.value else "false", ARGS_ENV: "mute"}
    if a == "notifications":
        return ["reg.exe", "add", NOTIFY_KEY, "/v", "ToastEnabled", "/t", "REG_DWORD", "/d",
                "1" if step.value == "on" else "0", "/f"], {}
    return None


# ---------------------------------------------------------------------------
# note I/O
# ---------------------------------------------------------------------------

def ensure_note() -> str:
    """Create ``Agent/Desktop Modes.md`` with the default modes when missing. Returns its path."""
    from assistant import vault
    from assistant.settings import get_assistant_settings

    vault.ensure_vault()
    if not vault.note_exists(NOTE_PATH):
        vault.write_note(NOTE_PATH, DEFAULT_NOTE.replace("{name}", get_assistant_settings().name),
                         source="desktop_modes", note_type="agent")
    return NOTE_PATH


def read_note_text() -> tuple[str, str]:
    """(body, sha) of the modes note."""
    from assistant import vault

    ensure_note()
    n = vault.read_note(NOTE_PATH)
    return n.body, n.sha


def load_modes() -> list[Mode]:
    return parse_modes(read_note_text()[0])


def save_yaml(yaml_text: str, *, base_sha: str | None = None) -> list[Mode]:
    """Validate ``yaml_text`` and write it back into the note's yaml block (rest of the note kept)."""
    from assistant import vault

    if len(yaml_text) > 50_000:
        raise ModeError("That is too long.")
    modes = parse_modes(yaml_text if re.search(r"```ya?ml", yaml_text) else f"```yaml\n{yaml_text}\n```")
    body, sha = read_note_text()
    block = f"```yaml\n{extract_yaml(yaml_text).strip()}\n```" if re.search(r"```ya?ml", yaml_text) \
        else f"```yaml\n{yaml_text.strip()}\n```"
    new = re.sub(r"```ya?ml[^\n]*\n.*?```", lambda _m: block, body, count=1, flags=re.S | re.I) \
        if re.search(r"```ya?ml", body, re.I) else body.rstrip() + "\n\n" + block + "\n"
    vault.write_note(NOTE_PATH, new, base_sha=base_sha or sha, on_conflict="raise", source="dashboard",
                     note_type="agent")
    db.log_event("assistant", "desktop modes saved", data={"modes": [m.name for m in modes]})
    return modes


def find_mode(name: str, modes: list[Mode] | None = None) -> Mode:
    modes = modes if modes is not None else load_modes()
    want = re.sub(r"\s*mode$", "", (name or "").strip().lower())
    for m in modes:
        if m.name.lower() == want:
            return m
    close = [m for m in modes if want and (want in m.name.lower() or m.name.lower() in want)]
    if len(close) == 1:
        return close[0]
    raise ModeError(f"No mode called {name!r}. Modes: {', '.join(m.name for m in modes) or 'none'}.")


# ---------------------------------------------------------------------------
# running
# ---------------------------------------------------------------------------

Runner = Callable[[list[str], dict[str, str]], tuple[int, str]]


def _subprocess_runner(argv: list[str], extra_env: dict[str, str]) -> tuple[int, str]:
    env = {**os.environ, **extra_env}
    kw: dict[str, Any] = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        p = subprocess.run(argv, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=STEP_TIMEOUT_SEC, stdin=subprocess.DEVNULL, **kw)
    except FileNotFoundError:
        return 127, f"{argv[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    out = ((p.stdout or "") + "\n" + (p.stderr or "")).strip()
    return p.returncode, out[-400:]


_runner: Runner | None = None
_sleep: Callable[[float], None] = time.sleep


def set_runner(fn: Runner | None, sleep: Callable[[float], None] | None = None) -> None:
    """Tests: replace subprocess execution (and sleeping)."""
    global _runner, _sleep
    _runner = fn
    _sleep = sleep or time.sleep


def is_windows() -> bool:
    return os.name == "nt"


def _hint(step: Step, code: int, out: str) -> str:
    if step.action == "power_plan" and code != 0:
        return ("that power plan is not on this PC (many laptops only have Balanced; run `powercfg /list`, "
                "or use Armoury Crate's Turbo/Performance mode)")
    if step.action == "launch" and code == 3:
        return out or "app not found"
    return out or f"exit code {code}"


def run_mode(name: str, *, dry_run: bool | None = None) -> ModeRun:
    from assistant.toolkit_settings import get_toolkit_settings

    mode = find_mode(name)
    reason = ""
    if dry_run is None:
        dry_run = get_toolkit_settings().desktop_modes_dry_run or (not is_windows() and _runner is None)
        if not is_windows() and _runner is None:
            reason = "not Windows: plan only"
    results: list[StepResult] = []
    for st in mode.steps:
        cmd = build_command(st)
        argv = cmd[0] if cmd else []
        if dry_run:
            results.append(StepResult(st.describe(), argv, True, skipped=True, optional=st.optional, detail="planned"))
            continue
        if st.action == "wait":
            _sleep(float(st.value))
            results.append(StepResult(st.describe(), [], True, optional=st.optional))
            continue
        assert cmd is not None
        code, out = (_runner or _subprocess_runner)(cmd[0], cmd[1])
        ok = code == 0
        results.append(StepResult(st.describe(), argv, ok, optional=st.optional,
                                  detail="" if ok else _hint(st, code, out)))
    ok = all(r.ok or r.optional for r in results)
    run = ModeRun(mode.name, dry_run, results, ok, reason)
    failed = [r.step for r in results if not r.ok]
    db.log_event("assistant", f"desktop mode {mode.name} " + ("planned" if dry_run else "ran" if ok else "ran with problems"),
                 level="info" if ok else "warning", data={"failed": failed})
    return run


# ---------------------------------------------------------------------------
# tools + CLI
# ---------------------------------------------------------------------------

def _tool_list(ctx: Any, a: dict[str, Any]) -> Any:
    from assistant.tools import ToolError

    try:
        return {"note": NOTE_PATH, "windows": is_windows(), "modes": [m.to_dict() for m in load_modes()]}
    except ModeError as exc:
        raise ToolError(f"the Desktop Modes note is invalid: {exc}") from None


def _tool_run(ctx: Any, a: dict[str, Any]) -> Any:
    from assistant.tools import ToolError

    try:
        r = run_mode(a["name"], dry_run=True if a.get("dry_run") else None)
    except ModeError as exc:
        raise ToolError(str(exc)) from None
    d = r.to_dict()
    for x in d["results"]:
        x.pop("argv", None)
    return d


def register_tools(register: Callable[..., None]) -> None:
    from assistant.tools import Tool, _obj

    register(Tool("desktop_modes_list",
                  "List the owner's desktop modes (Gaming, Work, Wind down...) and their steps, from the vault note "
                  "Agent/Desktop Modes.md.", _obj({}), _tool_list, owner=MODULE))
    register(Tool("desktop_mode_run",
                  "Set the owner's Windows PC up for a mode he named (e.g. 'gaming mode'): runs that mode's fixed steps "
                  "(open/close apps, power plan, volume, notifications). Only run a mode he asked for. dry_run=true "
                  "shows the plan without doing anything.",
                  _obj({"name": {"type": "string"}, "dry_run": {"type": "boolean"}}, ["name"]), _tool_run,
                  owner=MODULE, side_effects=True))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m assistant.desktop_modes", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("init", help="create the Desktop Modes note with the default modes if missing")
    sh = sub.add_parser("show")
    sh.add_argument("name")
    r = sub.add_parser("run")
    r.add_argument("name")
    r.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "init":
            out: Any = {"note": ensure_note()}
        elif args.cmd == "list":
            out = [m.to_dict() for m in load_modes()]
        elif args.cmd == "show":
            out = find_mode(args.name).to_dict()
        else:
            out = run_mode(args.name, dry_run=True if args.dry_run else None).to_dict()
    except ModeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if not isinstance(out, dict) or out.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
