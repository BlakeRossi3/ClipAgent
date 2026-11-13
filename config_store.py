"""Read and edit ``config.env`` for the web setup wizard / settings page.

The file edited is ``config.get_settings().config_file`` (``$CLIPAGENT_DATA_DIR/config.env`` under
the Windows launcher, so app updates never touch it). Edits keep every comment, blank line and
unknown key in place; changed keys are rewritten on their own line, new keys are appended. The
write is atomic (temp file + ``os.replace``) and, on POSIX, mode 0600 because the file holds
secrets. After a write the cached settings are dropped so the change applies without a restart,
except for :data:`RESTART_KEYS` (read once when the scheduler starts).

Values are never logged; :func:`update` logs only the key names.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path

import config

KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
#: Keys whose new value only takes effect after "Restart" (the scheduler reads them at start).
RESTART_KEYS = frozenset({"TIMEZONE", "ASSISTANT_ENABLED", "LOG_LEVEL"})
HEADER = ("# ClipAgent settings, written by the setup wizard (http://127.0.0.1:8000/setup/).\n"
          "# You can edit this file by hand too; every option is explained in config.env.example.\n")
_lock = threading.Lock()


class ConfigStoreError(ValueError):
    """A key or value that cannot be written safely."""


def path() -> Path:
    return Path(config.get_settings().config_file)


def read_values(p: Path | None = None) -> dict[str, str]:
    """The key/value pairs currently in the file (env vars not included)."""
    return config._read_env_file(Path(p) if p else path())


def quote(value: str) -> str:
    """Render a value for a dotenv line (double-quoted when it needs to be)."""
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ConfigStoreError("values must be a single line")
    if value == "" or re.fullmatch(r"[A-Za-z0-9_.,:/@+\-%=~]*", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render(text: str, changes: Mapping[str, str | None]) -> str:
    """Apply ``changes`` to the file text (pure). ``None`` removes the key."""
    for k in changes:
        if not KEY_RE.match(k):
            raise ConfigStoreError(f"bad config key {k!r}")
    pending = dict(changes)
    out: list[str] = []
    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if m and m.group(1) in changes:
            key = m.group(1)
            if key in pending:
                new = pending.pop(key)
                if new is not None:
                    out.append(f"{key}={quote(new)}")
            continue  # a duplicate line of an edited key is dropped (the last one used to win)
        out.append(line)
    new_lines = [f"{k}={quote(v)}" for k, v in pending.items() if v is not None]
    if new_lines:
        if out and out[-1].strip():
            out.append("")
        out.extend(new_lines)
    return "\n".join(out) + "\n"


def _atomic_write(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=target.parent)
    try:
        if sys.platform != "win32":  # Windows: the file lives in the user's own profile instead
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def update(changes: Mapping[str, str | None], *, p: Path | None = None) -> list[str]:
    """Write ``changes`` and reload settings. Returns the keys among them that need a restart."""
    target = Path(p) if p else path()
    with _lock:
        old = target.read_text(encoding="utf-8") if target.is_file() else HEADER
        _atomic_write(target, render(old, changes))
    reload()
    try:
        import db

        db.log_event("setup", "settings saved", data={"keys": sorted(changes)})
    except Exception:  # pragma: no cover - a broken DB must not lose the saved settings
        pass
    return sorted(k for k in changes if k in RESTART_KEYS)


def reload() -> None:
    """Drop cached settings so the next read sees the file (assistant settings follow config)."""
    config.reset_settings()
    try:
        from assistant.settings import reset_assistant_settings

        reset_assistant_settings()
    except Exception:  # pragma: no cover
        pass
