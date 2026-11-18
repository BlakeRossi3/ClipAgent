from pathlib import Path

from assistant import screenshot


def test_build_command_uses_throwaway_profile_and_size():
    cmd = screenshot.build_command("chrome.exe", "http://x/dashboard/", Path("o.png"), "prof", "800x600")
    assert "--headless=new" in cmd and "--user-data-dir=prof" in cmd
    assert "--window-size=800,600" in cmd and cmd[-1] == "http://x/dashboard/"
    assert any(a.startswith("--screenshot=") for a in cmd)
