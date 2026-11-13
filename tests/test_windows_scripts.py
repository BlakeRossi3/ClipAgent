"""Static checks of the Windows installer/launcher (PowerShell cannot run in CI here)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WIN = Path(__file__).resolve().parent.parent / "windows"
FILES = ["ClipAgent-Setup.bat", "setup.ps1", "ClipAgent.ps1", "ClipAgent.bat"]


def _text(name: str) -> str:
    return (WIN / name).read_bytes().decode("ascii")   # raises if not plain ASCII


@pytest.mark.parametrize("name", FILES)
def test_files_exist_and_are_ascii(name):
    assert _text(name).strip()


@pytest.mark.parametrize("name", ["ClipAgent-Setup.bat", "ClipAgent.bat"])
def test_batch_files_use_crlf_and_bypass_policy(name):
    raw = (WIN / name).read_bytes()
    assert b"\r\n" in raw and raw.count(b"\n") == raw.count(b"\r\n")     # cmd.exe needs CRLF
    assert b"-ExecutionPolicy Bypass" in raw and b"-NoProfile" in raw


@pytest.mark.parametrize("name", ["setup.ps1", "ClipAgent.ps1"])
def test_powershell_is_strict_and_alias_free(name):
    text = _text(name)
    assert "Set-StrictMode -Version Latest" in text and "$ErrorActionPreference = 'Stop'" in text
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for alias in (r"\bcurl\b", r"\bwget\b", r"\biwr\b", r"\bgci\b", r"\bls\b", r"\bcd\b", r"\bcat\b", r"\becho\b",
                  r"\b%\s*\{", r"\?\s*\{", r"\bsls\b", r"\bsleep\b", r"\bmkdir\b", r"\brm\b"):
        assert not re.search(alias, code), (name, alias)
    assert "Invoke-Expression" not in code and "iex " not in code
    assert code.count("-LiteralPath") >= 3            # paths with spaces / brackets in the user name


def test_setup_installs_prereqs_clones_and_starts():
    t = _text("setup.ps1")
    for needle in ["'Python.Python.3.13'", "'Git.Git'", "'Gyan.FFmpeg'", "--accept-package-agreements",
                   "--accept-source-agreements", "--silent", "Update-SessionPath", "'clone', '--branch'",
                   "'pull', '--ff-only'", "'-m', 'venv'", "'pip', 'install'", "requirements.txt", "'main.py', 'init'",
                   "CLIPAGENT_DATA_DIR", "CLIPAGENT_STRATEGY_PATH", "GetFolderPath('Programs')",
                   "GetFolderPath('Desktop')", "ClipAgent (background).lnk", "WScript.Shell", "Start-Transcript",
                   "App Installer", "$env:LOCALAPPDATA", "claude/clever-allen-zi43bi"]:
        assert needle in t, needle


def test_launcher_runs_schedule_on_loopback_and_opens_browser():
    t = _text("ClipAgent.ps1")
    for needle in ["main.py schedule --host 127.0.0.1", "/healthz", "Start-Process -FilePath \"$Url/\"",
                   "-WindowStyle Minimized", "'pull', '--ff-only'", "DASHBOARD_TRUST_LOOPBACK", "CLIPAGENT_DATA_DIR",
                   "[switch]$NoBrowser", "[switch]$NoUpdate", "logs", "Show-Problem", "PYTHONUTF8"]:
        assert needle in t, needle


def test_setup_bat_downloads_the_installer_for_this_branch():
    t = _text("ClipAgent-Setup.bat")
    assert ("https://raw.githubusercontent.com/BlakeRossi3/ClipAgent/refs/heads/%CLIPAGENT_BRANCH%/windows/setup.ps1"
            in t)
    assert 'set "CLIPAGENT_BRANCH=claude/clever-allen-zi43bi"' in t
    assert "Tls12" in t and "%~dp0setup.ps1" in t
    # cmd.exe would end the ( ... ) block at a ")" inside the PowerShell command
    cmd_line = next(line for line in t.splitlines() if "-Command" in line)
    assert "(" not in cmd_line and ")" not in cmd_line


def test_setup_and_launcher_agree_on_layout():
    s, launcher = _text("setup.ps1"), _text("ClipAgent.ps1")
    assert "Join-Path -Path $InstallRoot -ChildPath 'app'" in s and "Join-Path -Path $InstallRoot -ChildPath 'data'" in s
    assert "$App = Split-Path -Parent $PSScriptRoot" in launcher
    assert "Join-Path -Path $Root -ChildPath 'data'" in launcher
    import setup_web
    assert setup_web.BACKGROUND_LNK.name == "ClipAgent (background).lnk"
