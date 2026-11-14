"""Sterling as the administrator of ClipAgent: read everything, change everything - except secrets.

Blake's rule: Sterling can see and change every bit of the app (settings, pay, staff, memory, the
pipeline's rows, past conversations) and the ONLY thing he never sees is a secret value (API keys,
auth tokens, passwords, OAuth token files, the dashboard token, the Claude Code login). He may see
whether a secret is set.

Pieces (all registered as tools through :func:`register_tools`, the extension pattern):

* **Map** - :func:`app_map` is generated from the live DB, settings and files, so it never goes
  stale: every table (columns, row counts, what it holds), the vault layout, every config key with
  its current non-secret value, the staff (``agents/*.md``) and skills, log locations, scheduler jobs.
* **Read** - :func:`app_query` (one read-only SELECT on the app DB: ``mode=ro`` + ``query_only`` + a
  SQLite authorizer; secret columns read as NULL and secret ``kv`` values as ``[redacted]``),
  :func:`app_search` (messages on every channel, imported Claude history, vault, events,
  notifications, decisions, clips, sources...) and :func:`conversation_history`.
* **Change** - typed operations, each logged as an ``admin`` event with an undo record
  (:func:`app_undo`): :func:`config_set`, the ledger (:func:`pay_log`, :func:`hours_log`,
  :func:`job_set`, income edit/delete), the staff (:func:`staff_add` / rename / update / delete /
  restore), vault notes (:func:`memory_write` / :func:`memory_delete`; the Heartbeat checklist stays
  Blake's) and generic rows (:func:`app_update` / :func:`app_delete`).
* **Secrets** - :data:`SECRET_KEYS` + :data:`SECRET_RE` are the single definition used everywhere:
  config reads/writes, DB redaction, query results, the map, and :func:`redact` (which
  ``assistant.tools.dispatch`` applies to every tool result as a last line of defence).

Undo records live in ``kv`` (``admin:change:<n>``); nothing here needs a schema migration.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

import config
import db

MODULE = "admin"
REDACTED = "[redacted]"

# ---------------------------------------------------------------------------
# secrets: the single definition
# ---------------------------------------------------------------------------

#: Config keys whose VALUES are secret (never shown, never set by a tool).
SECRET_KEYS = frozenset({
    "ANTHROPIC_API_KEY", "TWILIO_AUTH_TOKEN", "TWILIO_ACCOUNT_SID", "DASHBOARD_TOKEN", "ELEVENLABS_API_KEY",
    "YOUTUBE_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
})
#: Anything else whose name looks like a secret (config keys, DB columns, kv keys). Case-insensitive.
SECRET_RE = re.compile(r"(api_?key|auth_?token|oauth|secret|password|passwd|private_?key|access_?token|"
                       r"refresh_?token|(^|[_:])token$|credential|csrf|local_key)", re.I)
#: Where credentials live on disk: the path is visible, but tools never change it or read the file.
CREDENTIAL_PATH_KEYS = frozenset({"YOUTUBE_CLIENT_SECRETS", "YOUTUBE_TOKEN_PATH"})
#: Well-known secret formats, redacted wherever they appear.
SECRET_VALUE_RE = re.compile(
    r"(sk-ant-[A-Za-z0-9_\-]{8,}|sk_[A-Za-z0-9]{24,}|AIza[0-9A-Za-z_\-]{30,}|ya29\.[0-9A-Za-z_\-.]{10,}|"
    r"1//[0-9A-Za-z_\-]{20,}|AC[0-9a-f]{32}|SK[0-9a-f]{32}|(?i:bearer)\s+[A-Za-z0-9_\-.=]{16,})")
MIN_SECRET_LEN = 8


def is_secret_key(name: str) -> bool:
    """True for a config key, column or kv key whose value must never be shown or set."""
    n = str(name or "").strip()
    if n.upper() in CREDENTIAL_PATH_KEYS:
        return False
    return n.upper() in SECRET_KEYS or bool(SECRET_RE.search(n))


def _kv_is_secret(key: Any) -> int:
    k = str(key or "")
    return 1 if (SECRET_RE.search(k) or k.startswith("setup:oauth:")) else 0


# A cheap SQL pre-filter for _kv_is_secret (kv also holds large non-secret state).
_KV_SECRET_LIKE = ("%secret%", "%token%", "%oauth%", "%password%", "%passwd%", "%credential%", "%csrf%", "%key%")


def secret_values() -> set[str]:
    """Every secret value currently known to the app (config, env, kv), for literal redaction."""
    vals: set[str] = set()

    def add(v: Any) -> None:
        if isinstance(v, str) and len(v.strip()) >= MIN_SECRET_LEN:
            vals.add(v.strip())

    try:
        s = config.get_settings()
        for attr in ("anthropic_api_key", "twilio_auth_token", "twilio_account_sid", "dashboard_token",
                     "youtube_api_key"):
            add(getattr(s, attr, ""))
        for k, v in config._read_env_file(Path(s.config_file)).items():
            if is_secret_key(k):
                add(v)
    except Exception:  # unreadable config must not break redaction of the rest
        pass
    for k, v in os.environ.items():
        if k.upper() in SECRET_KEYS or (SECRET_RE.search(k) and not k.startswith("CLIPAGENT_MCP")):
            add(v)
    try:
        with db.transaction() as c:
            likes = " OR ".join("key LIKE ?" for _ in _KV_SECRET_LIKE)
            for row in c.execute(f"SELECT key, value FROM kv WHERE {likes}", _KV_SECRET_LIKE):
                if _kv_is_secret(row["key"]):
                    try:
                        v = json.loads(row["value"]) if row["value"] is not None else None
                    except ValueError:
                        v = row["value"]
                    add(v if isinstance(v, str) else (json.dumps(v) if v is not None else None))
                    add(row["value"])
    except Exception:
        pass
    return vals


def redact(text: str, values: set[str] | None = None) -> str:
    """Replace every known secret value and well-known secret format in ``text``."""
    if not text:
        return text
    for v in sorted(values if values is not None else secret_values(), key=len, reverse=True):
        if v in text:
            text = text.replace(v, REDACTED)
    return SECRET_VALUE_RE.sub(REDACTED, text)


def redact_obj(obj: Any, values: set[str] | None = None) -> Any:
    """``redact`` applied through a JSON-able structure (dict keys that look secret are masked too)."""
    vals = secret_values() if values is None else values
    if isinstance(obj, str):
        return redact(obj, vals)
    if isinstance(obj, dict):
        return {k: (REDACTED if (isinstance(k, str) and is_secret_key(k) and isinstance(v, (str, int, float))
                                 and not isinstance(v, bool) and v != "")
                    else redact_obj(v, vals)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(x, vals) for x in obj]
    return obj


class AdminError(ValueError):
    """Refused or failed; the message is shown to the model."""


# ---------------------------------------------------------------------------
# change log + undo (kv: admin:change:<n>)
# ---------------------------------------------------------------------------

KV_SEQ = "admin:change_seq"
KV_CHANGE = "admin:change:{id}"


def _record(kind: str, summary: str, undo: dict[str, Any], *, via: str = "assistant") -> int:
    cid = db.kv_incr(KV_SEQ)
    db.kv_set(KV_CHANGE.format(id=cid), {"id": cid, "ts": db.now_iso(), "kind": kind, "summary": summary[:300],
                                         "via": via, "undo": undo, "undone": False})
    db.log_event(MODULE, summary[:300], data={"change_id": cid, "kind": kind, "via": via})
    return cid


def get_change(change_id: int) -> dict[str, Any] | None:
    v = db.kv_get(KV_CHANGE.format(id=int(change_id)))
    return v if isinstance(v, dict) else None


def list_changes(limit: int = 20) -> list[dict[str, Any]]:
    last = int(db.kv_get(KV_SEQ, 0) or 0)
    out = []
    for cid in range(last, max(0, last - 200), -1):
        ch = get_change(cid)
        if ch:
            out.append({k: ch[k] for k in ("id", "ts", "kind", "summary", "undone")})
        if len(out) >= limit:
            break
    return out


def app_undo(change_id: int | None = None) -> dict[str, Any]:
    """Revert one recorded change (default: the newest one not yet undone)."""
    if change_id is None:
        cand = [c for c in list_changes(200) if not c["undone"]]
        if not cand:
            raise AdminError("nothing to undo")
        change_id = cand[0]["id"]
    ch = get_change(int(change_id))
    if ch is None:
        raise AdminError(f"no change #{change_id}")
    if ch.get("undone"):
        raise AdminError(f"change #{change_id} was already undone")
    u = ch["undo"]
    kind = u.get("type")
    if kind == "rows":
        _restore_rows(u["rows"])
    elif kind == "config":
        import config_store

        config_store.update(u["before"])
        _reload_live(list(u["before"]))
    elif kind == "files":
        _restore_files(u["files"])
        if u.get("vault"):
            from assistant import vault

            vault.sync_index()
    else:
        raise AdminError(f"change #{change_id} cannot be undone")
    ch["undone"] = True
    db.kv_set(KV_CHANGE.format(id=int(change_id)), ch)
    db.log_event(MODULE, f"undid change #{change_id}: {ch['summary']}"[:300], data={"change_id": int(change_id)})
    return {"undone": int(change_id), "summary": ch["summary"]}


def _snapshot_files(paths: list[Path]) -> dict[str, str | None]:
    return {str(p): (p.read_text(encoding="utf-8") if p.is_file() else None) for p in paths}


def _restore_files(files: dict[str, str | None]) -> None:
    for p, text in files.items():
        path = Path(p)
        if text is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# DB schema introspection
# ---------------------------------------------------------------------------

TABLE_INFO: dict[str, str] = {
    "sources": "Shorts pipeline: candidate source videos found by scout (URL, channel, licence, status "
               "candidate->fetched->transcribed->picked->done/rejected/failed).",
    "clips": "Shorts pipeline: clips cut from sources (times, title, description, hashtags, hook, credit, status "
             "picked->rendered->scheduled->uploaded/failed/skipped).",
    "uploads": "YouTube uploads of clips (video id, privacy, publish time, status, quota, dry_run, lifetime views).",
    "stats": "Daily YouTube Analytics per video (views, watch minutes, likes, revenue...).",
    "channel_stats": "Daily channel totals (subscribers, total views, video count).",
    "retention": "Audience retention curve per video.",
    "decisions": "Approval requests to Blake (pending/approved/rejected/expired) and how he answered.",
    "llm_usage": "Every model call: model, purpose, tokens, cost (pipeline and assistant.* rows).",
    "strategy_versions": "Every saved strategy.json version with the weekly review report.",
    "notifications": "Every call/SMS/WhatsApp sent or queued to Blake (body, status).",
    "events": "The activity log shown on the dashboard (module, level, message, data).",
    "kv": "Small persistent flags and state (pause switch, cursors, counters, undo records). Secret entries "
          "read as [redacted].",
    "schema_version": "Pipeline DB migrations applied.",
    "as_messages": "Every message between Blake and Sterling on every channel (sms, whatsapp, dashboard chat, "
                   "voice, call, system): direction in/out, body, kind, agent, cost.",
    "as_checkins": "Scheduled check-in questions to Blake and his answers.",
    "as_jobs": "Blake's jobs: name, hourly rate, pay schedule, tax set-aside.",
    "as_work_log": "Hours worked per job per week (week_start = local Monday).",
    "as_work_days": "Hours worked per job per day (optional detail; the week's total is their sum).",
    "as_income": "Money in: job pay (from hours or logged pay), reels, other.",
    "as_meals": "Meal suggestions and his feedback.",
    "as_goals": "Blake's goals (title, metric, target, current, due, status).",
    "as_agent_runs": "Audit log of every agent run (agent, task, model, cost, status, tool calls).",
    "as_lessons": "Lessons about serving Blake better (proposed/adopted/retired).",
    "as_vault_index": "Index of the memory vault notes (path, title, sha) - managed by the vault.",
    "as_vault_fts": "Full-text index of the memory vault notes - managed by the vault.",
    "as_deferred": "Prompts queued to run later (after the Claude plan window resets, or at a set time).",
    "as_import_files": "Claude/ChatGPT export files imported (name, size, summary).",
    "as_import_conversations": "Conversations imported from his claude.ai export (name, dates, distil status).",
    "as_import_messages": "Each message of the imported conversations (sender human/assistant, text, time).",
    "as_import_fts": "Full-text index of imported conversation messages.",
    "as_import_projects": "claude.ai projects from his export.",
    "as_import_extras": "Other items from his export (memories, user profile...).",
}
#: Tables that the generic editor never touches (indexes and bookkeeping managed by their modules).
MANAGED_TABLES = frozenset({"schema_version", "as_vault_index", "as_vault_fts", "as_import_fts"})
_FTS_SHADOW = re.compile(r"_(config|content|data|docsize|idx)$")


def _ensure_tables() -> None:
    from assistant import store

    with store.transaction():
        pass
    try:
        from assistant import history_import

        with history_import._tx():
            pass
    except Exception:  # optional module
        pass


def _tables(c: sqlite3.Connection) -> list[tuple[str, str]]:
    return [(r["name"], r["sql"] or "") for r in
            c.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                      "ORDER BY name")]


def _is_shadow(name: str, all_names: set[str]) -> bool:
    m = _FTS_SHADOW.search(name)
    return bool(m) and name[: m.start()] in all_names


def _columns(c: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    return [{"name": r["name"], "type": r["type"] or "", "notnull": bool(r["notnull"]), "pk": int(r["pk"])}
            for r in c.execute(f'PRAGMA table_info("{table}")')]


def describe_tables() -> list[dict[str, Any]]:
    """Every table in the app DB with its columns, row count and plain-words purpose."""
    _ensure_tables()
    with db.transaction() as c:
        tabs = _tables(c)
        names = {n for n, _ in tabs}
        out = []
        for name, sql in tabs:
            shadow = _is_shadow(name, names)
            cols = _columns(c, name)
            try:
                n = int(c.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0])
            except sqlite3.DatabaseError:
                n = None
            base = name[: _FTS_SHADOW.search(name).start()] if shadow else name
            out.append({
                "table": name, "rows": n,
                "holds": TABLE_INFO.get(name) or (f"internal storage of the {base} full-text index" if shadow
                                                  else "(no description yet - look at its columns)"),
                "columns": [col["name"] + (" (secret: reads as NULL)" if is_secret_key(col["name"]) else "")
                            for col in cols],
                "editable": not (shadow or name in MANAGED_TABLES or "VIRTUAL TABLE" in sql.upper()),
            })
    return out


# ---------------------------------------------------------------------------
# config keys
# ---------------------------------------------------------------------------

_EX_LINE = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]{2,})=(.*)$")
_LOADER_KEY = re.compile(r"""(?:\bs|env\.get|env\[|_float\(env\.get|_bool\(env\.get)\(?\s*["']([A-Z][A-Z0-9_]{2,})["']""")
_SETTINGS_SOURCES = ("config.py", "assistant/settings.py", "assistant/toolkit_settings.py", "voice/settings.py")
#: Choices validated before writing (the loaders silently fall back to a default otherwise).
CHOICES: dict[str, tuple[str, ...]] = {
    "VOICE_TTS": ("kokoro", "sapi", "elevenlabs"),
    "VOICE_WEB_TTS": ("browser", "kokoro", "elevenlabs"),
    "VOICE_STT": ("auto", "browser", "server"),
    "VOICE_WAKE_WORD": ("both", "hey_jarvis", "name"),
    "VOICE_BARGE_IN": ("auto", "wake_only", "off"),
    "MESSAGING_CHANNEL": ("auto", "sms", "whatsapp"),
    "ASSISTANT_BACKEND": ("api", "claude_code"),
    "SCOUT_CC_BACKEND": ("ytdlp", "data_api"),
    "YOUTUBE_DEFAULT_PRIVACY": ("private", "unlisted", "public"),
    "LOG_LEVEL": ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
    "HAIKU_EFFORT": ("low", "medium", "high", "xhigh", "max"),
    "SONNET_EFFORT": ("low", "medium", "high", "xhigh", "max"),
    "ASSISTANT_EFFORT": ("low", "medium", "high", "xhigh", "max"),
    "SUBAGENT_EFFORT": ("low", "medium", "high", "xhigh", "max"),
    "ASSISTANT_VOICE_EFFORT": ("low", "medium", "high", "xhigh", "max"),
}
#: Read once at startup: a new value applies after Restart.
RESTART_KEYS = frozenset({
    "TIMEZONE", "ASSISTANT_ENABLED", "LOG_LEVEL", "HEARTBEAT_MINUTES", "VOICE_ENABLED", "VOICE_INPUT_DEVICE",
    "VOICE_OUTPUT_DEVICE", "VOICE_HOTKEY_TALK", "VOICE_HOTKEY_MUTE", "VOICE_TRAY", "CLIPAGENT_DATA_DIR",
    "CLIPAGENT_DB_PATH", "CLIPAGENT_MEDIA_DIR", "CLIPAGENT_STRATEGY_PATH", "CLIPAGENT_CONFIG_FILE",
    "ASSISTANT_BACKEND", "VOICE_STT_MODEL", "VOICE_NAME_MODEL", "WHISPER_MODEL", "WHISPER_DEVICE",
})


def config_keys() -> dict[str, dict[str, Any]]:
    """Every known config key -> {default, type, help}. From config.env.example + the settings loaders."""
    root = config.REPO_ROOT
    out: dict[str, dict[str, Any]] = {}
    ex = root / "config.env.example"
    if ex.is_file():
        comment: list[str] = []
        for line in ex.read_text(encoding="utf-8").splitlines():
            m = _EX_LINE.match(line)
            if m:
                key, default = m.group(1), m.group(2).strip().strip('"')
                out.setdefault(key, {"default": default, "help": " ".join(comment)[-240:]})
                comment = []
            elif line.strip().startswith("#"):
                comment.append(line.strip().lstrip("#").strip())
            else:
                comment = []
    for rel in _SETTINGS_SOURCES:
        p = root / rel
        if p.is_file():
            for key in _LOADER_KEY.findall(p.read_text(encoding="utf-8")):
                if not key.startswith("CLIPAGENT_MCP"):
                    out.setdefault(key, {"default": "", "help": ""})
    for key, info in out.items():
        d = info["default"]
        if key in CHOICES:
            info["type"] = "choice"
            info["choices"] = list(CHOICES[key])
        elif d.lower() in ("true", "false"):
            info["type"] = "bool"
        elif re.fullmatch(r"-?\d+(\.\d+)?", d):
            info["type"] = "number"
        elif re.fullmatch(r"\d{1,2}:\d{2}", d):
            info["type"] = "time"
        else:
            info["type"] = "text"
    return dict(sorted(out.items()))


def config_values(filter_text: str = "") -> dict[str, Any]:
    """Current value of every key (secrets: only whether set) and where it comes from."""
    import config_store

    fileval = config_store.read_values()
    f = filter_text.strip().upper()
    out: dict[str, Any] = {}
    for key, info in config_keys().items():
        if f and f not in key and f.lower() not in info.get("help", "").lower():
            continue
        if key in os.environ:
            raw, src = os.environ[key], "environment (overrides config.env)"
        elif key in fileval:
            raw, src = fileval[key], "config.env"
        else:
            raw, src = None, "default"
        if is_secret_key(key):
            out[key] = {"secret": True, "set": bool((raw or "").strip())}
            continue
        entry: dict[str, Any] = {"value": raw if raw is not None else info["default"], "from": src,
                                 "type": info["type"]}
        if info.get("choices"):
            entry["choices"] = info["choices"]
        if key in CREDENTIAL_PATH_KEYS:
            entry["note"] = "credential file location: visible, not changeable by tools"
        if key in RESTART_KEYS:
            entry["restart_needed_to_change"] = True
        out[key] = entry
    return out


def _resolve_key(key: str) -> str:
    k = str(key or "").strip()
    up = re.sub(r"[\s\-]+", "_", k).upper()
    return up


def _validate_value(key: str, value: Any, info: dict[str, Any] | None) -> str:
    sval = "" if value is None else (("true" if value else "false") if isinstance(value, bool) else str(value))
    sval = sval.strip()
    if "\n" in sval or "\r" in sval:
        raise AdminError("a setting value must be a single line")
    typ = (info or {}).get("type", "text")
    if sval == "":
        return ""  # empty = back to the default
    if typ == "bool":
        try:
            return "true" if config._bool(sval, False) else "false"
        except ValueError:
            raise AdminError(f"{key} is true or false") from None
    if typ == "number":
        try:
            x = float(sval.replace("$", "").replace(",", ""))
        except ValueError:
            raise AdminError(f"{key} needs a number") from None
        if x < 0:
            raise AdminError(f"{key} cannot be negative")
        d = (info or {}).get("default", "")
        return str(int(x)) if (x == int(x) and "." not in d) else f"{x:g}"
    if typ == "time":
        try:
            return config._hhmm(sval.replace(".", ":"))
        except ValueError:
            raise AdminError(f"{key} is a 24-hour HH:MM time") from None
    if typ == "choice":
        choices = (info or {}).get("choices") or []
        match = [c for c in choices if c.lower() == sval.lower()]
        if not match:
            raise AdminError(f"{key} is one of: {', '.join(choices)}")
        return match[0]
    return sval


def _reload_live(keys: list[str]) -> None:
    """Drop every settings cache; rebuild the voice if a voice key changed; tell the page."""
    import config_store

    config_store.reload()
    try:
        from assistant.toolkit_settings import reset_toolkit_settings

        reset_toolkit_settings()
    except Exception:
        pass
    try:
        from voice import settings as vsettings

        vsettings.reset_voice_settings()
        if any(k.startswith(("VOICE_", "ELEVENLABS_")) for k in keys):
            from voice import web

            web.reload_tts()
            from assistant import events

            events.publish("settings", voice=vsettings.get_voice_settings().public())
    except Exception:  # the voice package is optional
        pass


def config_set(key: str, value: Any, *, via: str = "assistant") -> dict[str, Any]:
    """Change any non-secret setting live (config.env, comment-preserving). Raises AdminError."""
    import config_store

    from assistant import live_settings

    raw = str(key or "").strip()
    if raw.lower() in live_settings.SETTINGS:  # friendly names: voice, quiet_hours, ...
        try:
            changes, said = live_settings.changes_for(raw.lower(), value)
        except live_settings.SettingRefused as exc:
            raise AdminError(str(exc)) from None
    else:
        k = _resolve_key(raw)
        if not config_store.KEY_RE.match(k):
            raise AdminError(f"{raw!r} is not a setting name")
        if is_secret_key(k):
            raise AdminError(f"{k} is a secret: I can see whether it is set but never read or change it. "
                             "He changes it himself in Settings.")
        if k in CREDENTIAL_PATH_KEYS:
            raise AdminError(f"{k} is where a credential file lives; he changes it in Settings.")
        known = config_keys()
        if k not in known:
            close = [x for x in known if raw.upper().replace(" ", "_") in x][:8]
            raise AdminError(f"unknown setting {k!r}" + (f"; did you mean {', '.join(close)}?" if close else
                                                          "; app_map section 'config' lists them all"))
        changes, said = {k: _validate_value(k, value, known[k])}, None
    for k in changes:
        if is_secret_key(k) and k != "ELEVENLABS_VOICE_ID":
            raise AdminError("Refused: that would change a secret.")
    before_vals = config_store.read_values()
    before = {k: before_vals.get(k) for k in changes}
    restart = config_store.update({k: (v if v != "" else None) for k, v in changes.items()})
    try:  # trial-load every settings object; a value the loaders reject is put back
        config.get_settings()
        from assistant.settings import get_assistant_settings

        get_assistant_settings()
        from assistant.toolkit_settings import load_toolkit_settings

        load_toolkit_settings()
        try:
            from voice.settings import load_voice_settings
        except ImportError:  # pragma: no cover
            load_voice_settings = None  # type: ignore[assignment]
        if load_voice_settings is not None:
            load_voice_settings()
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        config_store.update(before)
        _reload_live(list(changes))
        raise AdminError(f"refused: {exc}") from None
    _reload_live(list(changes))
    restart = sorted(set(restart) | {k for k in changes if k in RESTART_KEYS})
    overridden = sorted(k for k in changes if k in os.environ)
    cid = _record("config", f"setting changed: {', '.join(sorted(changes))}", {"type": "config", "before": before},
                  via=via)
    out: dict[str, Any] = {"ok": True, "changed": {k: (v if v != "" else "(default)") for k, v in changes.items()},
                           "applies": "after a restart" if restart else "now", "change_id": cid}
    if said:
        out["now"] = said
    if restart:
        out["restart_needed"] = restart
    if overridden:
        out["warning"] = (f"{', '.join(overridden)} is also set as an environment variable, which overrides "
                          "config.env until it is removed")
    return out


# ---------------------------------------------------------------------------
# app map
# ---------------------------------------------------------------------------

def _vault_layout() -> dict[str, Any]:
    from assistant import vault

    try:
        vault.ensure_vault()
        notes = vault.list_notes()
    except Exception as exc:
        return {"error": type(exc).__name__}
    folders = Counter(n.split("/", 1)[0] if "/" in n else "(top level)" for n in notes)
    return {"root": str(vault.vault_root()), "notes": len(notes), "folders": dict(sorted(folders.items())),
            "protected": sorted(vault.PROTECTED_NOTES),
            "tools": "memory_search/read/list/remember/forget, memory_write, memory_delete, journal_append"}


def staff_files() -> dict[str, Any]:
    from assistant import registry

    skills = registry.load_skills(strict=False)
    agents = registry.load_agents(skills=skills, strict=False)
    return {"agents_dir": str(registry.AGENTS_DIR), "skills_dir": str(registry.SKILLS_DIR),
            "agents": {n: {"file": a.path.name, "description": a.description, "role": a.extra.get("role")}
                       for n, a in sorted(agents.items())},
            "archived": [p.name for p in sorted(_archive_dir().glob("*.md"))] if _archive_dir().is_dir() else [],
            "skills": {n: s.description[:120] for n, s in sorted(skills.items())}}


def _jobs() -> list[dict[str, Any]]:
    try:
        import main

        return [{"id": j.id, "when": j.args, "trigger": j.trigger, "does": j.description} for j in main.job_specs()]
    except Exception as exc:
        return [{"error": f"could not list jobs: {type(exc).__name__}"}]


def _files() -> dict[str, Any]:
    s = config.get_settings()
    return {
        "app_dir": str(config.REPO_ROOT), "data_dir": str(s.data_dir), "database": str(s.db_path),
        "config_file": str(s.config_file), "strategy": str(s.strategy_path),
        "downloads": str(s.downloads_dir), "transcripts": str(s.transcripts_dir), "renders": str(s.renders_dir),
        "logs": [str(s.data_dir / "logs") + " (launcher-<date>.log, clipagent-<date>.log, voice.log)",
                 "the events table (app_query: SELECT * FROM events ORDER BY id DESC LIMIT 20)"],
        "templates": str(config.REPO_ROOT / "templates"), "static": str(config.REPO_ROOT / "static"),
        "excluded_secrets": ["config.env secret values", "YouTube OAuth token file / client secret file",
                             str(s.data_dir / "claude-code") + " (Claude Code login)"],
    }


MAP_SECTIONS = ("tables", "config", "vault", "staff", "jobs", "files")


def app_map(section: str | None = None, *, filter_text: str = "") -> dict[str, Any]:
    """The live map of the app. ``section`` = one of :data:`MAP_SECTIONS` (None = all)."""
    builders: dict[str, Callable[[], Any]] = {
        "tables": describe_tables, "config": lambda: config_values(filter_text), "vault": _vault_layout,
        "staff": staff_files, "jobs": _jobs, "files": _files,
    }
    if section:
        if section not in builders:
            raise AdminError(f"section is one of {', '.join(MAP_SECTIONS)}")
        return redact_obj({section: builders[section]()})
    return redact_obj({k: fn() for k, fn in builders.items()})


def app_map_compact() -> dict[str, Any]:
    """Small overview for the tool (fits one result): table -> rows + purpose, and what else exists."""
    tabs = describe_tables()
    names = {t["table"] for t in tabs}
    return {
        "tables": {t["table"]: f"{t['rows']} rows - {t['holds']}" for t in tabs
                   if not _is_shadow(t["table"], names)},
        "other_sections": {"config": f"{len(config_keys())} settings (app_map section='config', filter='VOICE')",
                           "vault": "memory notes (section='vault')", "staff": "agents/*.md + skills (section='staff')",
                           "jobs": "scheduler jobs (section='jobs')", "files": "paths, logs (section='files')"},
        "how": "Read rows with app_query (one SELECT). Search everything with app_search. Past chats: "
               "conversation_history. Change: config_set, pay_log, hours_log, job_set, income_edit/delete, staff_*, "
               "memory_write/delete, app_update/app_delete; undo any with app_undo.",
    }


# ---------------------------------------------------------------------------
# read-only SQL
# ---------------------------------------------------------------------------

QUERY_MAX_ROWS = 500
QUERY_TIMEOUT_SEC = 5.0
CELL_MAX = 4000
_READ_PRAGMAS = frozenset({"data_version", "table_info", "table_xinfo", "index_list", "index_info",
                           "foreign_key_list", "index_xinfo"})
_ALLOWED_ACTIONS = frozenset({sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                              sqlite3.SQLITE_RECURSIVE})


def _authorizer(action: int, arg1: str | None, arg2: str | None, dbname: str | None, source: str | None) -> int:
    if action == sqlite3.SQLITE_READ:
        if arg1 == "kv" and dbname == "main" and source is None:
            return sqlite3.SQLITE_DENY  # read kv through the redacting view (plain "kv")
        if arg2 and is_secret_key(arg2) and not (arg1 == "kv" and arg2 == "key"):
            return sqlite3.SQLITE_IGNORE  # secret-looking column -> NULL
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA:
        return sqlite3.SQLITE_OK if (arg1 or "").lower() in _READ_PRAGMAS else sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def _ro_connection() -> sqlite3.Connection:
    _ensure_tables()
    path = Path(config.get_settings().db_path).resolve()
    try:
        c = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
        c.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
    except sqlite3.Error:
        c = sqlite3.connect(str(path), timeout=5)
    c.row_factory = sqlite3.Row
    c.create_function("_admin_secret_kv", 1, _kv_is_secret, deterministic=True)
    c.execute(f"CREATE TEMP VIEW kv AS SELECT key, CASE WHEN _admin_secret_kv(key) THEN '{REDACTED}' "
              "ELSE value END AS value, updated_at FROM main.kv")
    c.execute("PRAGMA query_only = ON")
    return c


def _strip_sql(sql: str) -> str:
    s = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    s = re.sub(r"--[^\n]*", " ", s)
    return s.strip().rstrip(";").strip()


def app_query(sql: str, params: list[Any] | dict[str, Any] | None = None, *, limit: int = 100) -> dict[str, Any]:
    """Run ONE read-only SELECT/WITH on the app DB. Secret columns read as NULL, secret kv values redacted."""
    stmt = _strip_sql(str(sql or ""))
    if not stmt:
        raise AdminError("empty query")
    if not re.match(r"^(select|with)\b", stmt, re.I):
        raise AdminError("only one read-only SELECT (or WITH ... SELECT) statement; use app_update/app_delete or "
                         "the typed tools to change data")
    limit = max(1, min(int(limit or 100), QUERY_MAX_ROWS))
    c = _ro_connection()
    deadline = time.monotonic() + QUERY_TIMEOUT_SEC
    c.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 2000)
    c.set_authorizer(_authorizer)
    try:
        try:
            cur = c.execute(stmt, params if params is not None else ())
            rows = cur.fetchmany(limit + 1)
        except sqlite3.ProgrammingError as exc:
            raise AdminError(f"refused: {exc}") from None
        except sqlite3.DatabaseError as exc:
            msg = str(exc)
            if "interrupted" in msg:
                msg = f"query took longer than {QUERY_TIMEOUT_SEC:g}s; narrow it"
            elif "not authorized" in msg or "prohibited" in msg:
                msg = f"refused: {msg} (read-only SELECTs only; use plain 'kv', not main.kv)"
            raise AdminError(msg) from None
        cols = [d[0] for d in (cur.description or [])]
    finally:
        c.close()
    vals = secret_values()
    out_rows = []
    for r in rows[:limit]:
        row = {}
        for k in cols:
            v = r[k]
            if isinstance(v, bytes):
                v = f"<{len(v)} bytes>"
            if is_secret_key(k) and not k == "key" and v is not None:
                v = REDACTED
            if isinstance(v, str):
                v = redact(v, vals)
                if len(v) > CELL_MAX:
                    v = v[:CELL_MAX] + "…"
            row[k] = v
        out_rows.append(row)
    return {"columns": cols, "rows": out_rows, "row_count": len(out_rows), "truncated": len(rows) > limit}


# ---------------------------------------------------------------------------
# search + past conversations
# ---------------------------------------------------------------------------

def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _excerpt(text: str | None, n: int = 200) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


_SEARCH_SPECS: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    # (label, table, searched columns, select list)
    ("messages", "as_messages", ("body",), "id, ts, channel, direction, kind, body AS text"),
    ("events", "events", ("message", "data"), "id, ts, module, level, message AS text"),
    ("notifications", "notifications", ("body",), "id, ts, kind, channel, status, body AS text"),
    ("decisions", "decisions", ("summary", "resolution_note"), "id, requested_at AS ts, kind, status, summary AS text"),
    ("clips", "clips", ("title", "description", "hook_text"), "id, created_at AS ts, status, title AS text"),
    ("sources", "sources", ("title", "channel", "url", "topic"), "id, discovered_at AS ts, status, title AS text"),
    ("goals", "as_goals", ("title", "metric"), "id, created_at AS ts, status, title AS text"),
    ("lessons", "as_lessons", ("lesson", "evidence", "area"), "id, ts, status, lesson AS text"),
    ("income", "as_income", ("note", "source"), "id, created_at AS ts, day, amount_usd, note AS text"),
    ("jobs", "as_jobs", ("name",), "id, created_at AS ts, hourly_rate, name AS text"),
    ("meals", "as_meals", ("suggestion", "feedback"), "id, created_at AS ts, day, slot, suggestion AS text"),
    ("checkins", "as_checkins", ("question", "answer_text"), "id, scheduled_for AS ts, kind, status, "
                                                               "question || COALESCE(' -> ' || answer_text, '') AS text"),
    ("agent_runs", "as_agent_runs", ("task", "summary"), "id, ts, agent, status, task AS text"),
    ("deferred", "as_deferred", ("prompt", "result"), "id, created_at AS ts, status, prompt AS text"),
)


def app_search(text: str, *, limit: int = 8) -> dict[str, Any]:
    """Search every corner of the app for ``text``. Returns hits grouped by where they were found."""
    q = " ".join(str(text or "").split())
    if not q:
        raise AdminError("what should I search for?")
    limit = max(1, min(int(limit or 8), 50))
    _ensure_tables()
    out: dict[str, Any] = {}
    with db.transaction() as c:
        for label, table, cols, select in _SEARCH_SPECS:
            where = " OR ".join(f"{col} LIKE ? ESCAPE '\\'" for col in cols)
            try:
                rows = c.execute(f"SELECT {select} FROM {table} WHERE {where} ORDER BY 2 DESC LIMIT ?",
                                 (*[_like(q)] * len(cols), limit)).fetchall()
            except sqlite3.DatabaseError:
                continue
            if rows:
                out[label] = [{**{k: r[k] for k in r.keys() if k != "text"}, "text": _excerpt(r["text"], 240)}
                              for r in rows]
        try:
            rows = c.execute("SELECT key, value FROM kv WHERE key LIKE ? ESCAPE '\\' LIMIT ?",
                             (_like(q), limit)).fetchall()
            hits = [{"key": r["key"], "value": REDACTED if _kv_is_secret(r["key"]) else _excerpt(r["value"], 160)}
                    for r in rows]
            if hits:
                out["kv"] = hits
        except sqlite3.DatabaseError:
            pass
    try:
        from assistant import vault

        vault.ensure_vault()
        hits = vault.search(q, limit=limit)
        if hits:
            out["vault"] = [{"path": h.path, "title": h.title, "snippet": h.snippet} for h in hits]
    except Exception:
        pass
    try:
        from assistant import history_import

        hits = history_import.search(q, limit)
        if hits:
            out["claude_history"] = hits
    except Exception:
        pass
    return redact_obj({"query": q, "found": {k: len(v) for k, v in out.items()}, "results": out}
                      if out else {"query": q, "found": {}, "results": "no matches anywhere in the app"})


def conversation_history(days: int | None = 7, *, query: str | None = None, channel: str | None = None,
                         limit: int = 20) -> dict[str, Any]:
    """Past conversations with dates: per local day and channel, who said what (plus imported Claude chats)."""
    from assistant import store

    _ensure_tables()
    q = " ".join(str(query or "").split())
    days = None if days in (None, 0) else max(1, int(days))
    since = db.to_iso(db.utcnow() - timedelta(days=days)) if days else None
    where, params = ["1=1"], []
    if since:
        where.append("ts >= ?")
        params.append(since)
    if channel:
        where.append("channel = ?")
        params.append(channel)
    if q:
        where.append("body LIKE ? ESCAPE '\\'")
        params.append(_like(q))
    with store.transaction() as c:
        rows = c.execute(f"SELECT id, ts, channel, direction, kind, body FROM as_messages WHERE {' AND '.join(where)} "
                         "ORDER BY ts DESC, id DESC LIMIT 2000", params).fetchall()
    days_map: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        day = db.local_date_of(r["ts"]).isoformat()
        d = days_map.setdefault((day, r["channel"]), {"date": day, "channel": r["channel"], "messages": 0,
                                                      "from_owner": 0, "first": r["ts"], "last": r["ts"],
                                                      "owner_said": [], "sterling_said": []})
        d["messages"] += 1
        d["first"] = min(d["first"], r["ts"])
        d["last"] = max(d["last"], r["ts"])
        if r["direction"] == "in":
            d["from_owner"] += 1
            d["owner_said"].append(_excerpt(r["body"], 160))
        else:
            d["sterling_said"].append(_excerpt(r["body"], 160))
    convs = sorted(days_map.values(), key=lambda d: d["last"], reverse=True)[: max(1, int(limit))]
    for d in convs:
        d["owner_said"] = list(reversed(d["owner_said"]))[:6]
        d["sterling_said"] = list(reversed(d["sterling_said"]))[:4]
        d["first"] = db.parse_iso(d["first"]).astimezone(db.local_tz()).strftime("%H:%M")
        d["last"] = db.parse_iso(d["last"]).astimezone(db.local_tz()).strftime("%H:%M")
    out: dict[str, Any] = {"period": f"last {days} days" if days else "all time", "query": q or None,
                           "conversations": convs}
    if q:
        out["matches"] = [{"date": db.local_date_of(r["ts"]).isoformat(),
                           "time": db.parse_iso(r["ts"]).astimezone(db.local_tz()).strftime("%H:%M"),
                           "channel": r["channel"], "who": "Blake" if r["direction"] == "in" else "Sterling",
                           "text": _excerpt(r["body"], 300), "message_id": r["id"]} for r in rows[:limit]]
    try:
        from assistant import history_import

        if q:
            hits = history_import.search(q, limit)
        else:
            with history_import._tx() as c:
                cutoff = (db.local_today() - timedelta(days=days)).isoformat() if days else "0000"
                hits = [{"conversation": r["name"] or "untitled", "date": (r["created_at"] or "")[:10],
                         "messages": r["message_count"]} for r in
                        c.execute("SELECT name, created_at, message_count FROM as_import_conversations "
                                  "WHERE COALESCE(updated_at, created_at, '') >= ? ORDER BY created_at DESC LIMIT ?",
                                  (cutoff, limit)).fetchall()]
        if hits:
            out["claude_history"] = hits
    except Exception:
        pass
    if q:
        try:
            from assistant import vault

            vault.ensure_vault()
            j = [{"path": h.path, "snippet": h.snippet} for h in vault.search(q, limit=limit)
                 if h.path.startswith("Journal/")]
            if j:
                out["journal"] = j
        except Exception:
            pass
    if not convs and not out.get("claude_history") and not out.get("journal"):
        out["note"] = "nothing found; widen days (0 = all time) or change the query"
    return redact_obj(out)


# ---------------------------------------------------------------------------
# generic row edits (with undo)
# ---------------------------------------------------------------------------

def _table_meta(c: sqlite3.Connection, table: str) -> tuple[list[dict[str, Any]], str]:
    names = {n for n, _ in _tables(c)}
    if table not in names:
        raise AdminError(f"no table {table!r}; app_map lists them")
    sql = dict(_tables(c))[table]
    if _is_shadow(table, names) or table in MANAGED_TABLES or "VIRTUAL TABLE" in sql.upper():
        raise AdminError(f"{table} is managed automatically; use the memory/history tools instead")
    cols = _columns(c, table)
    pks = [col["name"] for col in cols if col["pk"]]
    pk = pks[0] if len(pks) == 1 else "rowid"
    return cols, pk


def _row(c: sqlite3.Connection, table: str, pk: str, key: Any) -> dict[str, Any] | None:
    r = c.execute(f'SELECT * FROM "{table}" WHERE "{pk}" = ?', (key,)).fetchone()
    return dict(r) if r is not None else None


def _dependents(c: sqlite3.Connection, table: str, key: Any) -> list[dict[str, Any]]:
    """Rows in other tables that point at ``table`` (cascade / set-null victims), for the undo snapshot."""
    out = []
    for other, _ in _tables(c):
        try:
            fks = c.execute(f'PRAGMA foreign_key_list("{other}")').fetchall()
        except sqlite3.DatabaseError:
            continue
        for fk in fks:
            if fk["table"] != table:
                continue
            ocols, opk = _columns(c, other), None
            pks = [x["name"] for x in ocols if x["pk"]]
            opk = pks[0] if len(pks) == 1 else "rowid"
            for r in c.execute(f'SELECT * FROM "{other}" WHERE "{fk["from"]}" = ?', (key,)).fetchall():
                out.append({"table": other, "pk": opk, "key": r[opk] if opk != "rowid" else None, "before": dict(r)})
    return out


def _restore_rows(rows: list[dict[str, Any]]) -> None:
    with db.transaction() as c:
        for item in rows:
            table, pk, key, before = item["table"], item["pk"], item["key"], item.get("before")
            if before is None:
                c.execute(f'DELETE FROM "{table}" WHERE "{pk}" = ?', (key,))
            elif c.execute(f'SELECT 1 FROM "{table}" WHERE "{pk}" = ?', (key,)).fetchone():
                sets = ", ".join(f'"{k}" = ?' for k in before if k != pk)
                c.execute(f'UPDATE "{table}" SET {sets} WHERE "{pk}" = ?',
                          (*[v for k, v in before.items() if k != pk], key))
            else:
                cols = ", ".join(f'"{k}"' for k in before)
                c.execute(f'INSERT INTO "{table}" ({cols}) VALUES ({", ".join("?" for _ in before)})',
                          tuple(before.values()))


def _coerce(col: dict[str, Any], v: Any) -> Any:
    if v is None:
        return None
    t = col["type"].upper()
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    if isinstance(v, bool):
        return int(v)
    try:
        if "INT" in t:
            return int(v) if not isinstance(v, str) or re.fullmatch(r"-?\d+", v.strip()) else int(float(v))
        if "REAL" in t:
            return float(str(v).replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        raise AdminError(f"{col['name']} needs a number, not {v!r}") from None
    return v


def _key_for(table: str, key: Any) -> Any:
    return int(key) if isinstance(key, str) and re.fullmatch(r"\d+", key.strip()) and table != "kv" else key


def app_update(table: str, key: Any, fields: dict[str, Any], *, force: bool = False, via: str = "assistant",
               summary: str | None = None) -> dict[str, Any]:
    """Change columns of one row (by primary key). Status columns of sources/clips go through their state
    machines (``force`` = manual repair). Returns the change id for app_undo."""
    if not isinstance(fields, dict) or not fields:
        raise AdminError("fields must be an object of column -> new value")
    key = _key_for(table, key)
    _ensure_tables()
    with db.transaction() as c:
        cols, pk = _table_meta(c, table)
        colmap = {col["name"]: col for col in cols}
        bad = [k for k in fields if k not in colmap]
        if bad:
            raise AdminError(f"{table} has no column(s) {bad}; columns: {', '.join(colmap)}")
        if pk in fields:
            raise AdminError(f"the key column {pk} cannot be changed")
        secret = [k for k in fields if is_secret_key(k)]
        if secret:
            raise AdminError(f"{secret} hold secrets and are never changed by tools")
        if table == "kv" and _kv_is_secret(key):
            raise AdminError("that kv entry is a secret")
        before = _row(c, table, pk, key)
        if before is None:
            raise AdminError(f"no {table} row with {pk} = {key!r}")
    vals = {k: _coerce(colmap[k], v) for k, v in fields.items()}
    if table == "kv" and "value" in vals and not isinstance(fields["value"], str):
        vals["value"] = json.dumps(fields["value"])
    elif table == "kv" and "value" in vals:
        try:
            json.loads(vals["value"])
        except ValueError:
            vals["value"] = json.dumps(vals["value"])
    status = vals.pop("status", None) if table in ("sources", "clips") and "status" in vals else None
    reason = vals.pop("status_reason", None) if status is not None else None
    try:
        if status is not None:
            setter = db.set_source_status if table == "sources" else db.set_clip_status
            setter(int(key), status, reason, force=force)
        if vals:
            if "updated_at" in colmap and "updated_at" not in vals:
                vals["updated_at"] = db.now_iso()
            with db.transaction() as c:
                sets = ", ".join(f'"{k}" = ?' for k in vals)
                c.execute(f'UPDATE "{table}" SET {sets} WHERE "{pk}" = ?', (*vals.values(), key))
    except db.InvalidTransition as exc:
        raise AdminError(f"{exc} (pass force=true for a manual repair)") from None
    except sqlite3.IntegrityError as exc:
        raise AdminError(f"refused by the database: {exc}") from None
    with db.transaction() as c:
        after = _row(c, table, pk, key)
    changed = {k: {"from": before.get(k), "to": (after or {}).get(k)} for k in fields}
    cid = _record("update", summary or f"{table} #{key}: changed {', '.join(fields)}",
                  {"type": "rows", "rows": [{"table": table, "pk": pk, "key": key, "before": before}]}, via=via)
    return redact_obj({"ok": True, "table": table, "key": key, "changed": changed, "change_id": cid})


def app_delete(table: str, key: Any, *, via: str = "assistant", summary: str | None = None) -> dict[str, Any]:
    """Delete one row (and whatever cascades from it); the snapshot lets app_undo put it all back."""
    key = _key_for(table, key)
    _ensure_tables()
    with db.transaction() as c:
        _cols, pk = _table_meta(c, table)
        if table == "kv" and _kv_is_secret(key):
            raise AdminError("that kv entry is a secret")
        before = _row(c, table, pk, key)
        if before is None:
            raise AdminError(f"no {table} row with {pk} = {key!r}")
        deps = _dependents(c, table, key)
        try:
            c.execute(f'DELETE FROM "{table}" WHERE "{pk}" = ?', (key,))
        except sqlite3.IntegrityError as exc:
            raise AdminError(f"refused by the database: {exc}") from None
    snap = [{"table": table, "pk": pk, "key": key, "before": before}] + [d for d in deps if d["key"] is not None]
    cid = _record("delete", summary or f"{table} #{key} deleted" + (f" (+{len(deps)} linked rows)" if deps else ""),
                  {"type": "rows", "rows": snap}, via=via)
    return redact_obj({"ok": True, "deleted": {"table": table, "key": key}, "linked_rows": len(deps),
                       "change_id": cid})


# ---------------------------------------------------------------------------
# ledger: pay, hours, jobs, income
# ---------------------------------------------------------------------------

def pay_log(amount: Any, *, day: str | None = None, source: str = "job", note: str | None = None,
            via: str = "assistant") -> dict[str, Any]:
    """Record money he received (a paycheck, a tip, a side job...) as an as_income row."""
    from assistant import ledger, store

    try:
        amt = float(str(amount).replace("$", "").replace(",", "").strip())
    except ValueError:
        raise AdminError(f"not an amount: {amount!r}") from None
    if amt < 0:
        raise AdminError("an amount cannot be negative")
    src = (source or "job").strip().lower()
    src = {"work": "job", "pay": "job", "paycheck": "job", "salary": "job", "shorts": "reels",
           "youtube": "reels"}.get(src, src)
    if src not in store.INCOME_SOURCES:
        raise AdminError(f"source is one of {', '.join(store.INCOME_SOURCES)}")
    try:
        d = date.fromisoformat(day) if day else db.local_today()
    except ValueError:
        raise AdminError("date looks like YYYY-MM-DD") from None
    ref = f"pay:{db.kv_incr('admin:pay_seq')}"
    iid = store.add_income(amt, source=src, day=d, note=note, ref=ref)
    if iid is None:  # pragma: no cover - refs are unique
        raise AdminError("could not record that payment")
    cid = _record("insert", f"pay logged: {ledger.fmt_usd(amt, whole=False)} ({src}) on {d.isoformat()}",
                  {"type": "rows", "rows": [{"table": "as_income", "pk": "id", "key": iid, "before": None}]}, via=via)
    s = ledger.summary(weeks=1)
    return {"ok": True, "income_id": iid, "amount": round(amt, 2), "source": src, "day": d.isoformat(), "note": note,
            "change_id": cid, "month_to_date": s["month_to_date"], "year_to_date_job_gross": s["year_to_date"]["job_gross"]}


def hours_log(hours: Any, *, week: str | None = None, job: str | None = None, note: str | None = None,
              via: str = "assistant") -> dict[str, Any]:
    """ledger.log_hours with an undo record (work log row + the week's pay row)."""
    from assistant import ledger, store

    try:
        ws = (getattr(ledger, "parse_week", None) or store.week_start_of)(week).isoformat()
        j = ledger.resolve_job(job) if (job or store.list_jobs()) else None
    except (ledger.LedgerError, ValueError) as exc:
        raise AdminError(str(exc)) from None
    before: list[dict[str, Any]] = []
    if j is not None:
        with store.transaction() as c:
            w = c.execute("SELECT * FROM as_work_log WHERE job_id = ? AND week_start = ?", (j.id, ws)).fetchone()
            i = c.execute("SELECT * FROM as_income WHERE source = 'job' AND ref = ?",
                          (f"hours:{j.id}:{ws}",)).fetchone()
        before = [{"w": dict(w) if w else None, "i": dict(i) if i else None}]
    try:
        res = ledger.log_hours(hours, week=week, job=job, source=via, note=note)
    except (ledger.LedgerError, ValueError) as exc:
        raise AdminError(str(exc)) from None
    j2 = store.get_job_by_name(res["job"])
    rows: list[dict[str, Any]] = []
    with store.transaction() as c:
        w = c.execute("SELECT id FROM as_work_log WHERE job_id = ? AND week_start = ?", (j2.id, ws)).fetchone()
        i = c.execute("SELECT id FROM as_income WHERE source = 'job' AND ref = ?", (f"hours:{j2.id}:{ws}",)).fetchone()
    prev = before[0] if before else {"w": None, "i": None}
    if w:
        rows.append({"table": "as_work_log", "pk": "id", "key": w["id"], "before": prev["w"]})
    if i:
        rows.append({"table": "as_income", "pk": "id", "key": i["id"], "before": prev["i"]})
    res["change_id"] = _record("ledger", f"hours logged: {res['hours']} h for week of {ws} ({res['job']})",
                               {"type": "rows", "rows": rows}, via=via)
    return res


def job_set(name: str, *, rate: Any = None, schedule: str | None = None, tax: Any = None, active: bool | None = None,
            rename: str | None = None, via: str = "assistant") -> dict[str, Any]:
    """Create or update a job (hourly rate, pay schedule, tax set-aside %, active, rename)."""
    from assistant import ledger, store

    if schedule is not None:
        schedule = schedule.strip().lower().replace("-", "").replace(" ", "")
        schedule = {"everyweek": "weekly", "everyotherweek": "biweekly", "fortnightly": "biweekly",
                    "twiceamonth": "semimonthly"}.get(schedule, schedule)
        if schedule not in store.PAY_SCHEDULES:
            raise AdminError(f"pay schedule is one of {', '.join(store.PAY_SCHEDULES)}")
    existing = store.get_job_by_name(" ".join((name or "").split()))
    before = None
    if existing is not None:
        with store.transaction() as c:
            before = dict(c.execute("SELECT * FROM as_jobs WHERE id = ?", (existing.id,)).fetchone())
    try:
        job = ledger.set_job(name, hourly_rate=None if rate is None else float(str(rate).replace("$", "")),
                             pay_schedule=schedule,
                             est_tax_rate=None if tax is None else float(str(tax).replace("%", "")))
    except (ledger.LedgerError, ValueError) as exc:
        raise AdminError(str(exc)) from None
    extra: dict[str, Any] = {}
    if active is not None:
        extra["active"] = int(bool(active))
    if rename:
        extra["name"] = " ".join(rename.split())
    if extra:
        try:
            with store.transaction() as c:
                c.execute(f"UPDATE as_jobs SET {', '.join(f'{k} = ?' for k in extra)}, updated_at = ? WHERE id = ?",
                          (*extra.values(), db.now_iso(), job.id))
        except sqlite3.IntegrityError:
            raise AdminError(f"a job called {rename!r} already exists") from None
        job = store.get_job(job.id)
    cid = _record("ledger", f"job set: {job.name}",
                  {"type": "rows", "rows": [{"table": "as_jobs", "pk": "id", "key": job.id, "before": before}]}, via=via)
    return {"ok": True, "job": job.to_dict(), "change_id": cid}


def income_edit(income_id: int, fields: dict[str, Any], *, via: str = "assistant") -> dict[str, Any]:
    allowed = {"amount_usd", "day", "source", "note"}
    f = {("amount_usd" if k == "amount" else k): v for k, v in (fields or {}).items()}
    bad = set(f) - allowed
    if bad:
        raise AdminError(f"income fields are amount, day, source, note (not {sorted(bad)})")
    if "day" in f:
        try:
            f["day"] = date.fromisoformat(str(f["day"])).isoformat()
        except ValueError:
            raise AdminError("day looks like YYYY-MM-DD") from None
    return app_update("as_income", income_id, f, via=via, summary=f"income #{income_id} edited")


def income_delete(income_id: int, *, via: str = "assistant") -> dict[str, Any]:
    return app_delete("as_income", income_id, via=via, summary=f"income #{income_id} deleted")


# ---------------------------------------------------------------------------
# staff (agents/*.md)
# ---------------------------------------------------------------------------

def _agents_dir() -> Path:
    from assistant import registry

    return Path(registry.AGENTS_DIR)


def _archive_dir() -> Path:
    return _agents_dir() / ".archive"


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    if s and not s[0].isalpha():
        s = "a-" + s
    return s[:48].rstrip("-")


def _yaml_scalar(v: str) -> str:
    import yaml

    v = " ".join(str(v).split())
    try:
        if yaml.safe_load(f"k: {v}") == {"k": v}:
            return v
    except yaml.YAMLError:
        pass
    return json.dumps(v, ensure_ascii=False)


def render_agent(meta: dict[str, Any], body: str) -> str:
    import yaml

    lines = ["---", f"name: {meta['name']}", f"description: {_yaml_scalar(meta.get('description', ''))}",
             f"model: {meta.get('model') or 'subagent'}",
             f"tools: [{', '.join(meta.get('tools') or [])}]", f"skills: [{', '.join(meta.get('skills') or [])}]"]
    for k, v in meta.items():
        if k in ("name", "description", "model", "tools", "skills") or v in (None, ""):
            continue
        lines.append(yaml.safe_dump({k: v}, allow_unicode=True, default_flow_style=False, width=1000).strip())
    return "\n".join(lines) + "\n---\n\n" + body.strip() + "\n"


def _read_agent(name: str) -> tuple[Path, dict[str, Any], str]:
    from assistant import registry

    p = _agents_dir() / f"{name}.md"
    if not p.is_file():
        raise AdminError(f"no staff member {name!r}; staff_list shows them")
    meta, body = registry.parse_frontmatter(p.read_text(encoding="utf-8"))
    return p, meta, body


def _registry_errors() -> set[str]:
    from assistant import registry, tools

    skills = registry.load_skills(strict=False)
    try:
        registry.load_agents(_agents_dir(), skills=skills, known_tools=tools.known_tool_names())
    except registry.RegistryError as exc:
        return set(exc.errors)
    return set()


def _staff_change(summary: str, paths: list[Path], apply: Callable[[], None], *, via: str) -> int:
    """Snapshot ``paths``, apply, validate with the registry (roll back on new errors), record undo."""
    before_errors = _registry_errors()
    snap = _snapshot_files(paths)
    try:
        apply()
        new = _registry_errors() - before_errors
    except Exception:
        _restore_files(snap)
        raise
    if new:
        _restore_files(snap)
        raise AdminError("the change would break the staff files, so I put them back: " + "; ".join(sorted(new))[:600])
    return _record("staff", summary, {"type": "files", "files": snap}, via=via)


def _list_or_none(v: Any) -> list[str] | None:
    if v is None:
        return None
    if isinstance(v, str):
        v = [x for x in re.split(r"[,\s]+", v) if x]
    return [str(x).strip() for x in v if str(x).strip()]


def staff_list() -> dict[str, Any]:
    from assistant import registry

    skills = registry.load_skills(strict=False)
    agents = registry.load_agents(_agents_dir(), skills=skills, strict=False)
    return {"staff": [{"name": n, "role": a.extra.get("role"), "display_name": a.extra.get("display_name"),
                       "description": a.description, "model": a.model, "tools": list(a.tools),
                       "skills": list(a.skills), "file": str(a.path)} for n, a in sorted(agents.items())],
            "archived": [p.name for p in sorted(_archive_dir().glob("*.md"))] if _archive_dir().is_dir() else [],
            "note": _commit_note()}


def _commit_note() -> str:
    return ("Staff files are part of the app's code (agents/*.md). On the full-power backend the self-edit flow "
            "tests and commits them after this turn; otherwise they stay as working-tree edits. app_undo reverts.")


def staff_add(name: str, *, description: str, prompt: str, role: str | None = None, model: str = "subagent",
              tools: Any = None, skills: Any = None, via: str = "assistant") -> dict[str, Any]:
    from assistant import registry
    from assistant import tools as tools_mod

    s = slug(name)
    if not registry.NAME_RE.match(s or ""):
        raise AdminError("a staff name needs letters (e.g. 'money-manager')")
    p = _agents_dir() / f"{s}.md"
    if p.exists():
        raise AdminError(f"{s} already exists; use staff_update")
    tl = _list_or_none(tools) or ["memory_search", "memory_read", "skill_read"]
    unknown = [t for t in tl if t not in tools_mod.known_tool_names()]
    if unknown:
        raise AdminError(f"unknown tools {unknown}")
    if "delegate" in tl:
        raise AdminError("only the main agent may delegate")
    meta: dict[str, Any] = {"name": s, "description": description, "model": model or "subagent", "tools": tl,
                            "skills": _list_or_none(skills) or []}
    if role:
        meta["role"] = role
    if slug(name) != name.strip():
        meta["display_name"] = " ".join(name.split())
    cid = _staff_change(f"staff added: {s}", [p], lambda: p.write_text(render_agent(meta, prompt), encoding="utf-8"),
                        via=via)
    return {"ok": True, "added": s, "file": str(p), "change_id": cid, "note": _commit_note()}


_UPDATABLE = ("description", "model", "tools", "skills", "prompt", "role", "display_name")


def staff_update(name: str, fields: dict[str, Any], *, via: str = "assistant") -> dict[str, Any]:
    from assistant import tools as tools_mod

    s = slug(name)
    p, meta, body = _read_agent(s)
    f = dict(fields or {})
    add_t, rm_t = _list_or_none(f.pop("add_tools", None)) or [], _list_or_none(f.pop("remove_tools", None)) or []
    bad = [k for k in f if k not in _UPDATABLE]
    if bad:
        raise AdminError(f"updatable fields: {', '.join(_UPDATABLE)}, add_tools, remove_tools (not {bad})")
    new_meta = dict(meta)
    for k in ("tools", "skills"):
        if k in f:
            new_meta[k] = _list_or_none(f.pop(k)) or []
    if add_t or rm_t:
        cur = list(new_meta.get("tools") or [])
        new_meta["tools"] = [t for t in cur if t not in rm_t] + [t for t in add_t if t not in cur]
    unknown = [t for t in new_meta.get("tools") or [] if t not in tools_mod.known_tool_names()]
    if unknown:
        raise AdminError(f"unknown tools {unknown}")
    new_body = f.pop("prompt", None) or body
    new_meta.update({k: v for k, v in f.items()})
    cid = _staff_change(f"staff updated: {s} ({', '.join(sorted(fields))})", [p],
                        lambda: p.write_text(render_agent(new_meta, new_body), encoding="utf-8"), via=via)
    return {"ok": True, "updated": s, "fields": sorted(fields), "change_id": cid, "note": _commit_note()}


def staff_rename(old: str, new: str, *, via: str = "assistant") -> dict[str, Any]:
    o, n = slug(old), slug(new)
    if o == "main":
        raise AdminError("main is Sterling himself; change ASSISTANT_NAME with config_set to rename him")
    from assistant import registry

    if not registry.NAME_RE.match(n or ""):
        raise AdminError("the new name needs letters")
    p, meta, body = _read_agent(o)
    target = _agents_dir() / f"{n}.md"
    if target.exists():
        raise AdminError(f"{n} already exists")
    others = [q for q in _agents_dir().glob("*.md") if q != p]
    pat = re.compile(rf"(?<![\w-]){re.escape(o)}(?![\w-])")
    new_meta = {**meta, "name": n}
    if not new_meta.get("role"):
        try:
            import home

            new_meta["role"] = home.AGENT_ROLES.get(o) or o.replace("-", " ").capitalize()
        except Exception:
            new_meta["role"] = o.replace("-", " ").capitalize()
    if slug(new) != new.strip():
        new_meta["display_name"] = " ".join(new.split())
    touched: list[str] = []

    def apply() -> None:
        target.write_text(render_agent(new_meta, body), encoding="utf-8")
        p.unlink()
        for q in others:
            text = q.read_text(encoding="utf-8")
            new_text = pat.sub(n, text)
            if new_text != text:
                q.write_text(new_text, encoding="utf-8")
                touched.append(q.name)

    cid = _staff_change(f"staff renamed: {o} -> {n}", [p, target, *others], apply, via=via)
    return {"ok": True, "renamed": {"from": o, "to": n}, "references_updated": touched, "change_id": cid,
            "note": _commit_note()}


def staff_delete(name: str, *, via: str = "assistant") -> dict[str, Any]:
    s = slug(name)
    if s == "main":
        raise AdminError("main is Sterling himself and cannot be deleted")
    p, _meta, _body = _read_agent(s)
    arch = _archive_dir() / f"{db.local_today().isoformat()}-{s}.md"
    k = 2
    while arch.exists():
        arch = _archive_dir() / f"{db.local_today().isoformat()}-{s}-{k}.md"
        k += 1

    def apply() -> None:
        arch.parent.mkdir(parents=True, exist_ok=True)
        arch.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
        p.unlink()

    cid = _staff_change(f"staff deleted (archived): {s}", [p, arch], apply, via=via)
    return {"ok": True, "deleted": s, "archived_as": str(arch), "change_id": cid,
            "note": "staff_restore brings them back. " + _commit_note()}


def staff_restore(name: str, *, via: str = "assistant") -> dict[str, Any]:
    s = slug(name)
    cands = sorted(_archive_dir().glob(f"*-{s}.md")) + sorted(_archive_dir().glob(f"*-{s}-[0-9]*.md"))
    cands = [c for c in cands if re.fullmatch(rf"\d{{4}}-\d{{2}}-\d{{2}}-{re.escape(s)}(-\d+)?\.md", c.name)]
    if not cands:
        raise AdminError(f"no archived staff member {s!r}")
    src = max(cands, key=lambda c: c.stat().st_mtime)
    target = _agents_dir() / f"{s}.md"
    if target.exists():
        raise AdminError(f"{s} already exists")

    def apply() -> None:
        target.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        src.unlink()

    cid = _staff_change(f"staff restored: {s}", [target, src], apply, via=via)
    return {"ok": True, "restored": s, "change_id": cid, "note": _commit_note()}


# ---------------------------------------------------------------------------
# memory notes (full write / delete; the Heartbeat checklist stays his)
# ---------------------------------------------------------------------------

def memory_write(path: str, body: str, *, mode: str = "replace", heading: str | None = None,
                 via: str = "assistant") -> dict[str, Any]:
    from assistant import vault

    vault.ensure_vault()
    try:
        abs_path, rel = vault.safe_path(path)
        vault._guard(rel, False)
    except vault.VaultError as exc:
        raise AdminError(str(exc)) from None
    snap = _snapshot_files([abs_path])
    try:
        if mode == "append":
            n = vault.append_to_note(rel, body, heading=heading or None, source=via)
        elif mode == "replace":
            cur = vault.read_note(rel) if abs_path.is_file() else None
            n = vault.write_note(rel, body, base_sha=cur.sha if cur else None, source=via, on_conflict="raise")
        else:
            raise AdminError("mode is replace or append")
    except vault.VaultError as exc:
        raise AdminError(str(exc)) from None
    cid = _record("memory", f"note {'written' if mode == 'replace' else 'appended'}: {rel}",
                  {"type": "files", "files": snap, "vault": True}, via=via)
    return {"ok": True, "note": n.path, "mode": mode, "change_id": cid}


def memory_delete(path: str, *, via: str = "assistant") -> dict[str, Any]:
    from assistant import vault

    vault.ensure_vault()
    try:
        abs_path, rel = vault.safe_path(path)
        snap = _snapshot_files([abs_path])
        if not vault.delete_note(rel):
            raise AdminError(f"no note at {rel}")
    except vault.VaultError as exc:
        raise AdminError(str(exc)) from None
    cid = _record("memory", f"note deleted: {rel}", {"type": "files", "files": snap, "vault": True}, via=via)
    return {"ok": True, "deleted": rel, "change_id": cid}


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

def _via(ctx: Any) -> str:
    return getattr(ctx, "channel", None) or "assistant"


def _wrap(fn: Callable[[Any, dict[str, Any]], Any]) -> Callable[[Any, dict[str, Any]], Any]:
    def run(ctx: Any, a: dict[str, Any]) -> Any:
        from assistant.tools import ToolError

        try:
            return redact_obj(fn(ctx, a))
        except AdminError as exc:
            raise ToolError(str(exc)) from None
    run.__name__ = getattr(fn, "__name__", "admin_tool")
    return run


TOOL_NAMES = ("app_map", "app_query", "app_search", "conversation_history", "config_get", "config_set", "pay_log",
              "hours_log", "job_set", "income_edit", "income_delete", "staff_list", "staff_add", "staff_rename",
              "staff_update", "staff_delete", "staff_restore", "memory_write", "memory_delete", "app_update",
              "app_delete", "app_undo", "app_changes")


def register_tools(register: Callable[..., None]) -> None:
    from assistant.tools import Tool, _obj

    S, I, N, B = {"type": "string"}, {"type": "integer"}, {"type": "number"}, {"type": "boolean"}
    KEY = {"type": ["integer", "string"]}
    OBJ = {"type": "object"}
    own = "assistant.admin"

    def t(name: str, desc: str, props: dict[str, Any], req: list[str], fn: Callable[..., Any],
          side: bool = False) -> None:
        register(Tool(name, desc, _obj(props, req), _wrap(fn), owner=own, side_effects=side), replace=True)

    t("app_map", "The live map of the whole app: every DB table (rows, columns, what it holds), settings, vault, "
      "staff, scheduler jobs, files and logs. No section = compact overview. Check this BEFORE ever saying you "
      "lack access to something.",
      {"section": {"type": "string", "enum": list(MAP_SECTIONS)}, "filter": S}, [],
      lambda c, a: app_map(a["section"], filter_text=a.get("filter") or "") if a.get("section") else app_map_compact())
    t("app_query", "Run ONE read-only SQL SELECT (or WITH) on the app database (all pipeline and as_* tables). "
      "Use ? placeholders with params. Secret columns read as NULL. Row cap 500.",
      {"sql": S, "params": {"type": "array"}, "limit": I}, ["sql"],
      lambda c, a: app_query(a["sql"], a.get("params"), limit=int(a.get("limit") or 100)))
    t("app_search", "Search everything at once: messages on every channel, imported Claude history, memory vault, "
      "events, notifications, approvals, clips, sources, goals, income, meals, check-ins, agent runs, kv.",
      {"text": S, "limit": I}, ["text"], lambda c, a: app_search(a["text"], limit=int(a.get("limit") or 8)))
    t("conversation_history", "Past conversations with Blake, grouped by date and channel (what he said, what you "
      "said), plus imported claude.ai chats and journal hits. days (default 7, 0 = all time), query, channel.",
      {"days": I, "query": S, "channel": S, "limit": I}, [],
      lambda c, a: conversation_history(a.get("days", 7), query=a.get("query"), channel=a.get("channel"),
                                        limit=int(a.get("limit") or 20)))
    t("config_get", "Current value of every setting (or those matching filter); secrets only say whether they are set.",
      {"filter": S}, [], lambda c, a: config_values(a.get("filter") or ""))
    t("config_set", "Change ANY non-secret setting live (config.env key such as DRY_RUN, MAX_UPLOADS_PER_DAY, "
      "ASSISTANT_DAILY_BUDGET_USD, TIMEZONE, VOICE_TTS_VOICE; or a friendly name: voice, voice_engine, voice_speed, "
      "wake_word, heartbeat_minutes, quiet_hours). Says if a restart is needed. Secrets are refused.",
      {"key": S, "value": {"type": ["string", "number", "boolean"]}}, ["key", "value"],
      lambda c, a: config_set(a["key"], a["value"], via=_via(c)), True)
    t("pay_log", "Record money Blake received: a paycheck, tips, a side job (source job|reels|other; date "
      "YYYY-MM-DD, default today). Shows in the ledger summary at once. Hours-based pay is computed by hours_log; "
      "do not log the same pay twice.",
      {"amount": N, "date": S, "source": S, "note": S}, ["amount"],
      lambda c, a: pay_log(a["amount"], day=a.get("date"), source=a.get("source") or "job", note=a.get("note"),
                           via=_via(c)), True)
    t("hours_log", "Log hours worked in a week (any date in that week, default this week; job name if several). "
      "Computes the week's pay from the job's rate.",
      {"hours": N, "week": S, "job": S, "note": S}, ["hours"],
      lambda c, a: hours_log(a["hours"], week=a.get("week"), job=a.get("job"), note=a.get("note"), via=_via(c)), True)
    t("job_set", "Create or update a job: hourly rate, pay schedule (weekly|biweekly|semimonthly|monthly), tax "
      "set-aside percent, active, rename.",
      {"name": S, "rate": N, "schedule": S, "tax": N, "active": B, "rename": S}, ["name"],
      lambda c, a: job_set(a["name"], rate=a.get("rate"), schedule=a.get("schedule"), tax=a.get("tax"),
                           active=a.get("active"), rename=a.get("rename"), via=_via(c)), True)
    t("income_edit", "Edit an income row by id (amount, day, source, note). Find ids with app_query on as_income.",
      {"id": I, "fields": OBJ}, ["id", "fields"], lambda c, a: income_edit(a["id"], a["fields"], via=_via(c)), True)
    t("income_delete", "Delete an income row by id (undoable).", {"id": I}, ["id"],
      lambda c, a: income_delete(a["id"], via=_via(c)), True)
    t("staff_list", "List the staff (agents/*.md): name, role, description, model, tools, skills, archived ones.",
      {}, [], lambda c, a: staff_list())
    t("staff_add", "Hire a new staff member (sub-agent): name, description (when to delegate to them), prompt "
      "(their instructions), optional role, model (subagent|main|claude-...), tools, skills.",
      {"name": S, "role": S, "description": S, "model": S, "tools": {"type": "array", "items": S},
       "skills": {"type": "array", "items": S}, "prompt": S}, ["name", "description", "prompt"],
      lambda c, a: staff_add(a["name"], description=a["description"], prompt=a["prompt"], role=a.get("role"),
                             model=a.get("model") or "subagent", tools=a.get("tools"), skills=a.get("skills"),
                             via=_via(c)), True)
    t("staff_rename", "Rename a staff member (file, name, and references in other staff files).",
      {"old": S, "new": S}, ["old", "new"], lambda c, a: staff_rename(a["old"], a["new"], via=_via(c)), True)
    t("staff_update", "Change a staff member: fields = {description, model, tools, skills, prompt, role, "
      "display_name, add_tools, remove_tools}.",
      {"name": S, "fields": OBJ}, ["name", "fields"], lambda c, a: staff_update(a["name"], a["fields"], via=_via(c)),
      True)
    t("staff_delete", "Let a staff member go (moved to agents/.archive, restorable). main cannot be deleted.",
      {"name": S}, ["name"], lambda c, a: staff_delete(a["name"], via=_via(c)), True)
    t("staff_restore", "Bring back an archived staff member.", {"name": S}, ["name"],
      lambda c, a: staff_restore(a["name"], via=_via(c)), True)
    t("memory_write", "Write a whole vault note (mode replace) or add to it (mode append, optional heading). "
      "Any note except Agent/Heartbeat.md, which is Blake's.",
      {"path": S, "body": S, "mode": {"type": "string", "enum": ["replace", "append"]}, "heading": S},
      ["path", "body"], lambda c, a: memory_write(a["path"], a["body"], mode=a.get("mode") or "replace",
                                                  heading=a.get("heading"), via=_via(c)), True)
    t("memory_delete", "Delete a vault note (undoable). Not Agent/Heartbeat.md.", {"path": S}, ["path"],
      lambda c, a: memory_delete(a["path"], via=_via(c)), True)
    t("app_update", "Change columns of one row in any app table by its key (id, or key for kv). sources/clips "
      "status changes follow their state machines (force=true for manual repair). Undoable.",
      {"table": S, "id": KEY, "fields": OBJ, "force": B}, ["table", "id", "fields"],
      lambda c, a: app_update(a["table"], a["id"], a["fields"], force=bool(a.get("force")), via=_via(c)), True)
    t("app_delete", "Delete one row from any app table by its key (linked rows are snapshotted too). Undoable.",
      {"table": S, "id": KEY}, ["table", "id"], lambda c, a: app_delete(a["table"], a["id"], via=_via(c)), True)
    t("app_undo", "Undo a change made with these tools (change_id; default the latest).", {"change_id": I}, [],
      lambda c, a: app_undo(a.get("change_id")), True)
    t("app_changes", "Recent changes made with the admin tools (ids for app_undo).", {"limit": I}, [],
      lambda c, a: list_changes(int(a.get("limit") or 20)))
