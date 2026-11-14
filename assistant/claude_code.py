"""Run the assistant on the owner's Claude subscription through Claude Code in headless mode.

``ASSISTANT_BACKEND=claude_code`` makes every assistant model call (Sterling, every sub-agent,
check-ins and briefs, dashboard chat, live evals, the improvement loop) a ``claude -p`` run of the
unmodified Claude Code CLI, signed in with the owner's own Claude account. The Shorts pipeline
(``llm.py``) keeps using the Anthropic API key.

Two kinds of run:

* **Full powers** (the main agent, ``ASSISTANT_CC_FULL=true``, default): Claude Code with its own
  built-in tools (Read/Edit/Write/Glob/Grep/Bash/PowerShell/WebFetch/WebSearch/Agent/Skill...), its
  own system prompt with the persona appended, cwd = the app checkout (its own code), ``--add-dir``
  for the data dir and his folders, ``--permission-mode dontAsk`` + allow/deny rules in a generated
  ``--settings`` file (secrets and destructive commands denied), our sub-agents as ``--agents`` and
  our skills mirrored into ``<CLAUDE_CONFIG_DIR>/skills``, one resumed session per channel
  (:func:`session_for`). Self-edit guardrails wrap the run (``assistant/selfedit.py``).
* **Tools-only** (sub-agents run as scheduled tasks, evals, the sign-in test), below.

How a tools-only run works::

    claude -p --output-format stream-json --verbose --model <alias> --system-prompt <persona>
           --tools "" --setting-sources= --strict-mcp-config --mcp-config <file>
           --allowedTools mcp__clipagent__<tool>,... --permission-mode dontAsk
           --no-session-persistence --disable-slash-commands --max-turns N --effort E
    (the conversation text goes in on stdin; never through a shell; no console window on Windows)

* No built-in tools (no shell, no file edits, no web fetch), no user/project settings, no
  CLAUDE.md, no other MCP servers. The only capabilities are Sterling's own tools, served by
  ``assistant/mcp_server.py`` (stdio MCP) which enforces the agent's allowlist itself and records
  every call exactly like the in-process path (audit trail, approvals, owner-only texts, SMS cap,
  DRY_RUN all live in ``assistant.tools``).
* Delegation stays a tool (``delegate``) served by that MCP server: it runs the sub-agent as a
  nested ``claude -p`` with its own MCP server and its own allowlist (Claude Code's built-in
  sub-agents would share one MCP server, so per-agent tool limits and audit could not be enforced).
* Claude Code runs in its own config directory, ``<data_dir>/claude-code`` (``CLAUDE_CONFIG_DIR``),
  and in an empty working folder inside it: the owner's personal Claude Code settings, CLAUDE.md
  files, plugins and MCP servers are never loaded, and signing ClipAgent out does not sign his own
  Claude Code out.
* Sign-in is Claude Code's own flow (``claude auth login``): the browser opens Claude's sign-in page
  and Claude Code stores its credential itself. ClipAgent never sees or stores the Claude login.
  (An owner-provided ``CLAUDE_CODE_OAUTH_TOKEN`` in config.env is passed through if present.)
* Subscription use is not billed per call: ``llm_usage`` rows are written with ``cost_usd=0`` (the
  assistant's dollar cap is not consumed) and Claude Code's notional figure is kept per day in kv
  for the dashboard. Instead :data:`ASSISTANT_MAX_TURNS_PER_DAY` caps ``claude -p`` runs per local
  day, and a plan usage limit ("You've hit your session limit · resets 4pm") switches the assistant
  off until the reset time (told once; never retried in a loop).
* Models: aliases (``opus``, ``sonnet``...) tried in order; a "not available on your plan" answer
  moves to the next one and the working model is remembered in kv.

Tests never start the real CLI: :func:`set_runner` injects a fake.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import db
from assistant.settings import get_assistant_settings

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_SERVER_NAME = "clipagent"
TOOL_PREFIX = f"mcp__{MCP_SERVER_NAME}__"
KV_TURNS = "assistant:cc:turns:{day}"
KV_NOTIONAL = "assistant:cc:notional_microusd:{day}"
KV_MODEL = "assistant:cc:model:{role}"           # model id that last worked for main / sub
KV_BACKOFF = "assistant:cc:backoff"               # {"until": iso, "reason": text}
KV_PLAN_HINT = "assistant:cc:plan_hint"
KV_LAST_ERROR = "assistant:cc:last_error"
DEFAULT_BACKOFF = timedelta(hours=1)
CREATE_NO_WINDOW = 0x08000000
# Environment variables Claude Code must not inherit: the pipeline's API key (it would win over
# the subscription in -p mode) and anything that selects another provider or account.
STRIP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_PROFILE",
             "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
             "CLAUDE_CODE_USE_FOUNDRY", "CLAUDECODE", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_ENTRYPOINT")
# Non-secret settings the MCP server process must share with this one.
_MCP_ENV_PASS = ("PYTHONUTF8", "PYTHONIOENCODING", "SYSTEMROOT", "PATH", "TZ", "LANG", "LC_ALL",
                 "DASHBOARD_TRUST_LOOPBACK", "LOG_LEVEL")
TOOL_NOTE = ("\n\n# Tools in this session\nYour tools come from the '" + MCP_SERVER_NAME + "' server, so each is "
             "called " + TOOL_PREFIX + "<name> (for example " + TOOL_PREFIX + "memory_search). You have no "
             "other tools: no shell, no files, no web unless a web tool is listed.")


class ClaudeCodeError(RuntimeError):
    """A ``claude -p`` run failed. ``kind``: not_installed | auth | rate | timeout | blocked | error."""

    def __init__(self, kind: str, message: str, *, calls: list[Any] | None = None):
        super().__init__(message)
        self.kind = kind
        self.calls = calls or []


# ---------------------------------------------------------------------------
# locations
# ---------------------------------------------------------------------------

_home_override: Path | None = None


def set_home(path: Path | None) -> None:
    """Pin Claude Code's config dir (evals keep the real sign-in while sandboxing the data dir)."""
    global _home_override
    _home_override = Path(path) if path else None


def home_dir() -> Path:
    """``CLAUDE_CONFIG_DIR`` for every run: ClipAgent's own Claude Code profile."""
    import config

    return _home_override or (config.get_settings().data_dir / "claude-code")


def work_dir() -> Path:
    p = home_dir() / "work"
    p.mkdir(parents=True, exist_ok=True)
    return p


def runs_dir() -> Path:
    p = home_dir() / "runs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _is_windows() -> bool:
    return sys.platform == "win32"


def candidate_paths() -> list[Path]:
    home = Path.home()
    if _is_windows():
        return [home / ".local" / "bin" / "claude.exe",
                Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
                / "Microsoft" / "WinGet" / "Links" / "claude.exe"]
    return [home / ".local" / "bin" / "claude", Path("/usr/local/bin/claude"), Path("/opt/homebrew/bin/claude")]


def find_claude() -> Path | None:
    """The claude executable: ASSISTANT_CLAUDE_BIN, then PATH, then the installers' usual places.

    On Windows only a real ``.exe`` is accepted (a ``.cmd``/``.bat`` shim would run through cmd.exe,
    where argument quoting cannot be made safe)."""
    s = get_assistant_settings()
    found: list[Path] = []
    if s.claude_bin:
        found.append(Path(s.claude_bin))
    else:
        w = shutil.which("claude")
        if w:
            found.append(Path(w))
        found += candidate_paths()
    for p in found:
        if _is_windows() and p.suffix.lower() in (".cmd", ".bat"):
            exe = p.with_suffix(".exe")
            if not exe.is_file():
                continue
            p = exe
        if p.is_file():
            return p
    return None


# ---------------------------------------------------------------------------
# the process runner (tests inject a fake)
# ---------------------------------------------------------------------------

@dataclass
class Completed:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., Completed]   # runner(argv, *, input, env, cwd, timeout) -> Completed


def _popen_kwargs() -> dict[str, Any]:
    return {"creationflags": CREATE_NO_WINDOW} if _is_windows() else {}


def _subprocess_runner(argv: list[str], *, input: str | None, env: Mapping[str, str], cwd: str,
                       timeout: float) -> Completed:
    p = subprocess.run(argv, input=input, env=dict(env), cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout, check=False, **_popen_kwargs())
    return Completed(p.returncode, p.stdout or "", p.stderr or "")


_runner: Runner = _subprocess_runner


def set_runner(runner: Runner | None) -> None:
    """Install a fake process runner (tests). ``None`` restores the real one."""
    global _runner
    _runner = runner or _subprocess_runner


def blocked_message(exc: OSError) -> str | None:
    """A friendly message when Windows refused to start the executable."""
    code = getattr(exc, "winerror", None)
    if code in (4551, 4550, 1260) or "application control" in str(exc).lower():
        return ("Windows blocked Claude Code from starting (Smart App Control or a company policy). Claude Code "
                "is signed by \"Anthropic, PBC\". Open Windows Security > App & browser control to allow it, "
                "then press Sign in again.")
    if code == 225 or "virus" in str(exc).lower():
        return ("Your antivirus stopped Claude Code from starting. Allow claude.exe (signed by \"Anthropic, "
                "PBC\") in the antivirus, then press Sign in again.")
    if code == 5:
        return "Windows denied access to claude.exe. Try Sign in again; if it repeats, reinstall Claude Code."
    return None


def base_env(*, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    s = get_assistant_settings()
    env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
    env["CLAUDE_CONFIG_DIR"] = str(home_dir())
    home_dir().mkdir(parents=True, exist_ok=True)
    if api_fallback_active():
        import config

        env["ANTHROPIC_API_KEY"] = config.get_settings().anthropic_api_key  # the plan is limited: bill the API
    elif s.cc_oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = s.cc_oauth_token
    if extra:
        env.update(extra)
    return env


# ---------------------------------------------------------------------------
# output parsing + error classification
# ---------------------------------------------------------------------------

def parse_output(stdout: str) -> dict[str, Any] | None:
    """The ``--output-format json`` result object (the last JSON object with type=result)."""
    text = (stdout or "").strip()
    if not text:
        return None
    candidates = [text] + [ln for ln in reversed(text.splitlines()) if ln.strip().startswith("{")]
    for c in candidates:
        try:
            data = json.loads(c)
        except ValueError:
            continue
        if isinstance(data, list):  # stream-json collected as an array
            data = next((d for d in reversed(data) if isinstance(d, dict) and d.get("type") == "result"), None)
        if isinstance(data, dict) and ("result" in data or data.get("type") == "result" or "is_error" in data):
            return data
    return None


_LIMIT_RE = re.compile(r"(hit your (?P<scope>[\w\s-]{0,20}?)\s*limit|usage limit reached|out of extra usage)", re.I)
_MODEL_LIMIT_RE = re.compile(r"hit your (opus|sonnet|fable|haiku) limit", re.I)
_MODEL_RE = re.compile(r"(not available (with|on|for) (the |your )?claude|is not a recognized model|model .{0,60}"
                       r"not found|issue with the selected model|restricted by your organization|"
                       r"not available\. your organization|couldn't confirm model|requires usage credits|"
                       r"invalid model)", re.I)
_AUTH_RE = re.compile(r"(not logged in|please run /login|/login|oauth token (revoked|has expired)|"
                      r"invalid api key|authentication (required|failed)|failed to authenticate|"
                      r"invalid authentication credentials|login expired|could not resolve authentication|"
                      r"does not have access to claude|login not accepted)", re.I)
_RATE_RE = re.compile(r"(\b429\b|temporarily limiting requests|rate.?limit|overloaded|\b529\b)", re.I)


def classify_error(text: str, data: Mapping[str, Any] | None = None) -> str:
    """limit | model_limit | model | auth | rate | error."""
    t = text or ""
    if _MODEL_LIMIT_RE.search(t):
        return "model_limit"
    if _LIMIT_RE.search(t):
        return "limit"
    if _MODEL_RE.search(t):
        return "model"
    if _AUTH_RE.search(t) or (data or {}).get("api_error_status") in (401, 403):
        return "auth"
    if _RATE_RE.search(t) or (data or {}).get("api_error_status") in (429, 529):
        return "rate"
    return "error"


_RESET_RE = re.compile(r"resets?\s+(?:at\s+)?(?:(?P<dow>mon|tue|wed|thu|fri|sat|sun)\w*\s+)?"
                       r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)?"
                       r"(?:\s*\((?P<tz>[A-Za-z_]+/[A-Za-z_/]+)\))?", re.I)
_EPOCH_RE = re.compile(r"\|(\d{10})\b")
_DOW = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def parse_reset(text: str, now: datetime | None = None) -> datetime | None:
    """Reset time from a usage-limit message, as an aware UTC datetime (None if not stated).

    Handles "resets 3:45pm", "resets Mon 12:00am", "resets 4pm (America/New_York)" and the older
    "Claude AI usage limit reached|1731600000" form. Times without a zone are the machine's local
    zone (Claude Code prints local time); ClipAgent's TIMEZONE is used for that."""
    from datetime import UTC
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    now = now or db.utcnow()
    m = _EPOCH_RE.search(text or "")
    if m:
        return datetime.fromtimestamp(int(m.group(1)), UTC)
    m = _RESET_RE.search(text or "")
    if not m:
        return None
    h, mi, ap = int(m.group("h")), int(m.group("m") or 0), (m.group("ap") or "").lower()
    if ap == "pm" and h != 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    tz = db.local_tz()
    if m.group("tz"):
        try:
            tz = ZoneInfo(m.group("tz"))
        except (ZoneInfoNotFoundError, ValueError):
            pass
    local_now = now.astimezone(tz)
    cand = local_now.replace(hour=h, minute=mi, second=0, microsecond=0)
    if m.group("dow"):
        target = _DOW.index(m.group("dow").lower()[:3])
        cand += timedelta(days=(target - cand.weekday()) % 7)
        if cand <= local_now:
            cand += timedelta(days=7)
    elif cand <= local_now:
        cand += timedelta(days=1)
    return cand.astimezone(UTC)


def friendly_time(dt: datetime) -> str:
    """'at 4pm', 'at 4:30pm', 'on Mon at 12am' in the owner's time zone."""
    loc = dt.astimezone(db.local_tz())
    hm = loc.strftime("%I").lstrip("0") + (f":{loc.minute:02d}" if loc.minute else "") + loc.strftime("%p").lower()
    if loc.date() == db.local_now().date():
        return f"at {hm}"
    if loc.date() == db.local_now().date() + timedelta(days=1):
        return f"tomorrow at {hm}"
    return f"on {loc.strftime('%a')} at {hm}"


# ---------------------------------------------------------------------------
# guards: pause, plan-limit backoff, daily run cap
# ---------------------------------------------------------------------------

def backoff_until() -> datetime | None:
    v = db.kv_get(KV_BACKOFF)
    if not isinstance(v, dict) or not v.get("until"):
        return None
    until = db.parse_iso(v["until"])
    if until <= db.utcnow():
        return None
    return until


def clear_backoff() -> None:
    db.kv_delete(KV_BACKOFF)


def turns_today() -> int:
    return int(db.kv_get(KV_TURNS.format(day=db.local_today().isoformat()), 0) or 0)


def _budget_exc(reason: str, message: str, when: str = "") -> Exception:
    from assistant.runtime import AssistantBudgetExceeded

    return AssistantBudgetExceeded(reason, message, when=when)


def check_guards(*, count_turn: bool = True) -> None:
    """Raise ``AssistantBudgetExceeded`` (paused | limit | turns) before starting a run."""
    if db.kv_get("budget:paused") is not None:
        raise _budget_exc("paused", "AI spending is paused by the owner")
    until = backoff_until()
    if until is not None and not api_fallback_active():
        raise _budget_exc("limit", f"Claude plan limit; back {friendly_time(until)}", friendly_time(until))
    if until is not None and _api_budget_left() <= 0.0:
        raise _budget_exc("daily", "today's API allowance for the plan-limit fallback is spent")
    if count_turn:
        cap = get_assistant_settings().max_turns_per_day
        if turns_today() >= cap:
            if db.kv_set_if_absent(f"assistant:cc:turn_cap_alerted:{db.local_today().isoformat()}", db.now_iso()):
                db.log_event("assistant", "daily Claude run cap reached", level="warning", data={"cap": cap})
            raise _budget_exc("turns", f"daily Claude run cap ({cap}) reached")


def _set_backoff(text: str, *, alert_channel: str) -> datetime:
    until = parse_reset(text) or (db.utcnow() + DEFAULT_BACKOFF)
    until = min(until, db.utcnow() + timedelta(days=8))
    db.kv_set(KV_BACKOFF, {"until": db.to_iso(until), "reason": (text or "")[:200], "set_at": db.now_iso()})
    if db.kv_set_if_absent(f"assistant:cc:limit_alert:{db.to_iso(until)}", db.now_iso()):
        when = friendly_time(until)
        db.log_event("assistant", f"Claude plan limit reached; assistant paused until {when}", level="warning",
                     data={"until": db.to_iso(until), "message": (text or "")[:200]})
        if alert_channel == "system":  # an owner message gets this in its reply already
            s = get_assistant_settings()
            try:
                from assistant import tools

                tools.send_to_owner(f"I've reached your Claude plan's limit, {s.owner_honorific}; back {when}.",
                                    kind="plan_limit", agent="main")
            except Exception as exc:  # cap reached, no phone...: the dashboard event still shows it
                log.info("plan-limit text not sent: %s", exc)
    return until


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------

def model_candidates(role: str) -> list[str]:
    """Models to try, in order: the one that last worked, the configured one, then fallbacks."""
    s = get_assistant_settings()
    first = s.cc_model if role == "main" else s.cc_subagent_model
    out: list[str] = []
    remembered = db.kv_get(KV_MODEL.format(role=role))
    if isinstance(remembered, dict) and remembered.get("alias") and remembered.get("configured") == first:
        out.append(str(remembered["alias"]))
    for m in (first, *s.cc_fallback_models):
        if m and m not in out:
            out.append(m)
    return out[:4]


def model_in_use(role: str = "main") -> str:
    """Model id that last answered for ``role`` (main | sub), else the configured alias."""
    v = db.kv_get(KV_MODEL.format(role=role))
    if isinstance(v, dict) and v.get("model"):
        return str(v["model"])
    s = get_assistant_settings()
    return s.cc_model if role == "main" else s.cc_subagent_model


def _remember_model(role: str, alias: str, model: str) -> None:
    s = get_assistant_settings()
    db.kv_set(KV_MODEL.format(role=role), {"alias": alias, "model": model, "at": db.now_iso(),
                                           "configured": s.cc_model if role == "main" else s.cc_subagent_model})


# ---------------------------------------------------------------------------
# one run
# ---------------------------------------------------------------------------

@dataclass
class CCResult:
    text: str
    model: str
    alias: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    notional_usd: float = 0.0
    billed_to_api: bool = False              # ran on the API-key fallback: notional_usd is a real charge
    session_id: str | None = None
    num_turns: int = 0
    status: str = "ok"             # ok | rounds | denied
    tool_calls: list[Any] = field(default_factory=list)


@dataclass
class McpSpec:
    """Who the MCP server serves: one agent's tools and the turn's context."""
    agent: str
    allowed: list[str]
    channel: str = "system"
    source: str = "system"
    message_id: int | None = None
    run_id: int | None = None


# ---------------------------------------------------------------------------
# full powers (main agent): Claude Code's own tools + ours, its own code, his folders
# ---------------------------------------------------------------------------

BUILTIN_TOOLS = ("Read", "Glob", "Grep", "Edit", "Write", "NotebookEdit", "Bash", "PowerShell", "Monitor",
                 "WebFetch", "WebSearch", "Agent", "Skill", "TodoWrite", "TaskCreate", "TaskGet", "TaskList",
                 "TaskUpdate", "TaskStop", "ToolSearch")
CHROME_TOOLS = "mcp__claude-in-chrome__*"
# Files no tool may read or change: ClipAgent's secrets, OAuth tokens, keys, Claude Code's own profile.
SECRET_PATTERNS = ("//**/config.env", "//**/.env", "//**/*.env", "//**/*token*.json", "//**/client_secret*.json",
                   "//**/*.pem", "//**/*.key", "//**/.ssh/**", "//**/.credentials.json")
# Commands that are never run (deny rules beat allow rules in every mode). Deleting in bulk,
# publishing code, rewriting history, powering the laptop off.
DENY_COMMANDS = ("rm -rf *", "rm -fr *", "rm -r *", "rm -R *", "git push *", "git reset --hard *", "git clean *",
                 "git checkout -- *", "shutdown *", "format *", "mkfs *", "dd *", "del /s *", "rd /s *",
                 "rmdir /s *", "cmd /c del *", "cmd /c rd *", "reg delete *", "curl * | sh*", "curl * | bash*")
DENY_POWERSHELL = ("Remove-Item * -Recurse*", "Remove-Item -Recurse *", "Remove-Item * -r *", "Format-Volume *",
                   "Clear-Disk *", "Stop-Computer*", "Restart-Computer*", "git push *", "git reset --hard *",
                   "git clean *", "Remove-ItemProperty *", "Invoke-Expression *", "iex *")
SESSION_CHANNELS = ("dashboard", "sms", "whatsapp", "voice")
KV_SESSION = "assistant:cc:session:{channel}"
DEFAULT_USER_DIRS = ("Desktop", "Documents", "Downloads", "Pictures", "Videos")


def rule_path(p: Path | str) -> str:
    """Absolute path in permission-rule form: ``//home/x`` or, on Windows, ``//c/Users/x``."""
    s = str(p).replace("\\", "/")
    if re.match(r"^[A-Za-z]:", s):
        s = "/" + s[0].lower() + s[2:]
    if not s.startswith("/"):
        s = "/" + s
    return "/" + s.rstrip("/")


def api_fallback_active() -> bool:
    """True while the Claude plan is limited and Sterling should run Claude Code on the saved Anthropic
    API key instead (ASSISTANT_API_FALLBACK, default on). Same full powers; billed per token, capped by
    ASSISTANT_DAILY_BUDGET_USD (passed to Claude Code as --max-budget-usd)."""
    s = get_assistant_settings()
    if s.fake or s.backend != "claude_code" or not s.api_fallback:
        return False
    import config

    if not config.get_settings().anthropic_api_key:
        return False
    return backoff_until() is not None


def _api_budget_left() -> float:
    from assistant import runtime

    try:
        return float(runtime.remaining_today())
    except Exception:  # pragma: no cover - defensive
        return 0.0


def full_mode(agent: str = "main") -> bool:
    s = get_assistant_settings()
    return s.backend == "claude_code" and s.cc_full and agent == "main"


def user_dirs() -> list[Path]:
    """The owner's folders the assistant may use: ASSISTANT_USER_DIRS, else the usual ones that exist."""
    s = get_assistant_settings()
    if s.user_dirs:
        return [Path(d).expanduser() for d in s.user_dirs if Path(d).expanduser().is_dir()]
    home = Path.home()
    out: list[Path] = []
    for name in DEFAULT_USER_DIRS:
        for base in (home, home / "OneDrive"):
            if (base / name).is_dir() and (base / name) not in out:
                out.append(base / name)
    return out


def uploads_dir() -> Path:
    import config

    p = config.get_settings().data_dir / "assistant" / "uploads"
    p.mkdir(parents=True, exist_ok=True)
    return p


def working_dirs() -> list[Path]:
    """cwd first (the app checkout = its own code), then the data dir and his folders (--add-dir)."""
    import config

    out = [REPO_ROOT, config.get_settings().data_dir]
    for d in user_dirs():
        if d not in out:
            out.append(d)
    return out


def permission_settings(*, chrome: bool = False, mcp_tools: list[str] | None = None) -> dict[str, Any]:
    """The ``--settings`` JSON for a full-power run (pure apart from reading settings; tested)."""
    dirs = working_dirs()
    allow = [t for t in BUILTIN_TOOLS if t not in ("Edit", "Write", "NotebookEdit")]
    for d in dirs:
        allow += [f"Edit({rule_path(d)}/**)", f"Write({rule_path(d)}/**)", f"NotebookEdit({rule_path(d)}/**)"]
    # Each of our tools by name (verified rule form) plus the server-wide glob.
    allow += [TOOL_PREFIX + n for n in (mcp_tools or [])] + [TOOL_PREFIX + "*"]
    if chrome:
        allow.append(CHROME_TOOLS)
    secret = list(SECRET_PATTERNS) + [rule_path(home_dir()) + "/**"]
    deny = [f"Read({p})" for p in secret] + [f"Edit({p})" for p in secret]
    deny += [f"Bash({c})" for c in DENY_COMMANDS] + [f"PowerShell({c})" for c in DENY_POWERSHELL]
    from assistant import heartbeat   # his checklist: proposals go through request_approval

    for p in heartbeat.protected_paths():
        deny += [f"Edit({rule_path(p)})", f"Write({rule_path(p)})"]
    return {"permissions": {"allow": allow, "deny": deny, "defaultMode": "dontAsk"},
            # Keep the PowerShell tool on next to Bash deny rules (Windows with Git Bash).
            "env": {"CLAUDE_CODE_USE_POWERSHELL_TOOL": "1"}}


def native_agents() -> dict[str, Any]:
    """Our sub-agents (agents/*.md) as Claude Code ``--agents`` JSON: one source of truth."""
    from assistant import registry, tools

    s = get_assistant_settings()
    skills = registry.load_skills(strict=False)
    out: dict[str, Any] = {}
    for name, spec in sorted(registry.load_agents(skills=skills, strict=False).items()):
        if name == "main":
            continue
        own = [TOOL_PREFIX + t.name for t in tools.available(spec.tools)]
        # Read-only built-ins: changes to files (and the self-edit guardrails) stay with the main agent.
        builtin = ["Read", "Glob", "Grep", "WebSearch", "WebFetch", "Skill"]
        out[name] = {"description": spec.description, "prompt": spec.prompt, "tools": builtin + own,
                     "model": s.cc_subagent_model}
    return out


def sync_native_skills() -> list[str]:
    """Mirror skills/*/ into Claude Code's user skills folder (``<CLAUDE_CONFIG_DIR>/skills``).
    skills/ stays the source of truth; stale mirrors are removed. Returns the names synced."""
    from assistant import registry

    dst_root = home_dir() / "skills"
    dst_root.mkdir(parents=True, exist_ok=True)
    names = []
    for name, spec in sorted(registry.load_skills(strict=False).items()):
        if name == "persona":  # already the system prompt
            continue
        src_dir = Path(spec.path).parent
        dst = dst_root / name
        dst.mkdir(exist_ok=True)
        for f in src_dir.iterdir():
            if f.is_file():
                data = f.read_bytes()
                target = dst / f.name
                if not target.is_file() or target.read_bytes() != data:
                    target.write_bytes(data)
        names.append(name)
    for old in dst_root.iterdir():
        if old.is_dir() and old.name not in names and (old / ".clipagent").is_file():
            shutil.rmtree(old, ignore_errors=True)
    for n in names:
        (dst_root / n / ".clipagent").write_text("mirrored from skills/ by ClipAgent\n", encoding="utf-8")
    return names


def full_note(*, chrome: bool) -> str:
    s = get_assistant_settings()
    dirs = working_dirs()
    folders = "\n".join(f"- {d}" for d in dirs[2:]) or "- (none found)"
    browser = ("Claude in Chrome is connected: use its browser tools (mcp__claude-in-chrome__*) to open pages, "
               "read them, click and download in his own signed-in Chrome."
               if chrome else
               f"Use {TOOL_PREFIX}open_url to open a page in his default browser (his own signed-in Chrome).")
    return f"""

# Your powers (Claude Code on {s.owner_name}'s laptop)
You have Claude Code's built-in tools: read/search/edit files, a shell (PowerShell on Windows, Bash
where available), web search and fetch, skills, and sub-agents (the Agent tool; your specialists are
listed there: where your instructions say `delegate`, use the Agent tool with that specialist). Your own
tools come from the '{MCP_SERVER_NAME}' server ({TOOL_PREFIX}<name>).

Folders:
- Your own code (the ClipAgent app; the working directory): {dirs[0]}
- Your data (database, vault, uploads, logs): {dirs[1]}
{folders}

Browser: you can open any URL in {s.owner_name}'s own signed-in browser on this laptop. {browser}
Pages that need his login (claude.ai, YouTube, Instagram...) work that way because they use his own
session, so do it yourself instead of saying you cannot access them. Never extract, read or store his
cookies, passwords or session tokens.

Images he attaches are files under {uploads_dir()}: open them with Read.

Editing yourself: you may change any file in your own code. Before you do, read CLAUDE.md (the
engineering contract) and docs/ASSISTANT.md, follow their conventions, and add or update tests.
After your turn the app runs the full offline test suite: if it passes, your change is committed on
the local branch '{s.self_edit_branch}' and the app restarts; if it fails, your change is rolled back
and you will be told why. Do not commit, push, reset or switch branches yourself. He can text UNDO.

Never answer "I can't" to something he asks: if no tool, skill, the shell or his browser covers it,
give yourself the capability (a new tool module or skill in your own code, with tests) and then do
the task. Follow the toolsmith skill. Modules such as assistant/history_import.py, reels.py,
desktop_modes.py and twilio_doctor.py are yours to improve or rewrite when they fall short.

Ask {s.owner_name} first (request_approval or just ask) before: texting or emailing anyone but him,
spending money or buying anything, posting anything publicly, deleting files outside your own code and
data folders, or changing system settings. Never print, copy or send secrets (API keys, tokens,
passwords, config.env); those files are blocked for you anyway."""


def build_full_argv(exe: str, *, model: str, system: str, mcp_config: str | None, settings_path: str,
                    agents_path: str | None, add_dirs: list[str], resume: str | None, effort: str | None,
                    max_turns: int, chrome: bool) -> list[str]:
    """``claude -p`` with Claude Code's own tools and system prompt kept (pure; tested).

    The persona is *appended* (``--append-system-prompt``) so Claude Code's tool instructions stay.
    Only ClipAgent's own Claude Code profile is read (``--setting-sources user`` with
    CLAUDE_CONFIG_DIR = <data>/claude-code): never his personal settings, nor the repo's CLAUDE.md.
    """
    argv = [exe, "-p", "--output-format", "stream-json", "--verbose", "--model", model,
            "--append-system-prompt", system,
            "--setting-sources", "user", "--settings", settings_path, "--permission-mode", "dontAsk",
            "--strict-mcp-config"]
    if mcp_config:
        argv += ["--mcp-config", mcp_config]
    if agents_path:
        argv += ["--agents", agents_path]
    for d in add_dirs:
        argv += ["--add-dir", d]
    if resume:
        argv += ["--resume", resume]
    argv += ["--chrome" if chrome else "--no-chrome", "--max-turns", str(max(1, int(max_turns)))]
    if effort:
        argv += ["--effort", effort]
    return argv


def prepare_full_run(tag: str, *, chrome: bool, mcp_tools: list[str] | None = None) -> tuple[Path, Path | None]:
    """Write the run's settings and agents files (regenerated every run from the sources)."""
    settings_path = runs_dir() / f"{tag}.settings.json"
    settings_path.write_text(json.dumps(permission_settings(chrome=chrome, mcp_tools=mcp_tools), indent=1), encoding="utf-8")
    agents_path: Path | None = None
    try:
        sync_native_skills()
        agents = native_agents()
        if agents:
            agents_path = runs_dir() / f"{tag}.agents.json"
            agents_path.write_text(json.dumps(agents), encoding="utf-8")
    except Exception as exc:  # a broken skill/agent file must not silence the assistant
        db.log_event("assistant", "skills/agents sync failed", level="warning", data={"error": repr(exc)[:300]})
    return settings_path, agents_path


# -- conversation memory: one resumable session per channel ------------------

def session_state(channel: str) -> dict[str, Any] | None:
    v = db.kv_get(KV_SESSION.format(channel=channel))
    return v if isinstance(v, dict) and v.get("id") else None


def session_for(channel: str) -> str | None:
    """The session id to resume on ``channel``, or None (new session; the old one is rolled over
    when it is from another day or too long)."""
    if channel not in SESSION_CHANNELS:
        return None
    v = session_state(channel)
    if v is None:
        return None
    s = get_assistant_settings()
    reason = None
    if v.get("day") != db.local_today().isoformat():
        reason = "new day"
    elif int(v.get("turns") or 0) >= s.session_max_turns:
        reason = "long conversation"
    elif int(v.get("tokens") or 0) >= s.session_max_tokens:
        reason = "context full"
    if reason is None:
        return str(v["id"])
    rollover(channel, reason)
    return None


def rollover(channel: str, reason: str = "manual") -> bool:
    """End ``channel``'s session: a plain-code summary of it goes to the vault journal."""
    v = session_state(channel)
    db.kv_delete(KV_SESSION.format(channel=channel))
    if v is None:
        return False
    try:
        from assistant import store, vault

        msgs = [m for m in store.list_messages(200, channel=channel, since=v.get("started")) if m.kind != "plan_limit"]
        if msgs:
            lines = [f"- {'He' if m.direction == 'in' else 'I'}: {' '.join(m.body.split())[:160]}"
                     for m in reversed(msgs[:12])]
            vault.append_journal(f"Conversation on {channel} closed ({reason}; {len(msgs)} messages since "
                                 f"{v.get('started', '?')[:16]}Z). Last exchanges:\n" + "\n".join(lines),
                                 source="claude_code")
    except Exception as exc:
        db.log_event("assistant", "session summary not written", level="warning", data={"error": repr(exc)[:300]})
    db.log_event("assistant", f"{channel} conversation rolled over", data={"reason": reason, "session": v.get("id")})
    return True


def _record_session(channel: str, res: "CCResult", resumed: bool) -> None:
    if channel not in SESSION_CHANNELS or not res.session_id:
        return
    prev = session_state(channel) if resumed else None
    db.kv_set(KV_SESSION.format(channel=channel), {
        "id": res.session_id, "day": db.local_today().isoformat(),
        "started": (prev or {}).get("started") or db.now_iso(),
        "turns": int((prev or {}).get("turns") or 0) + 1,
        "tokens": res.input_tokens + res.cache_read_tokens + res.cache_write_tokens})


def build_argv(exe: str, *, model: str, system: str, allowed_tools: list[str], mcp_config: str | None,
               effort: str | None, web_search: bool, max_turns: int) -> list[str]:
    """The fixed ``claude -p`` command line (pure; tested)."""
    argv = [exe, "-p", "--output-format", "stream-json", "--verbose", "--model", model, "--system-prompt", system,
            "--tools", "WebSearch" if web_search else "",
            "--setting-sources=", "--strict-mcp-config"]
    if mcp_config:
        argv += ["--mcp-config", mcp_config]
    allowed = [TOOL_PREFIX + t for t in allowed_tools] + (["WebSearch"] if web_search else [])
    if allowed:
        argv += ["--allowedTools", ",".join(allowed)]
    argv += ["--permission-mode", "dontAsk", "--no-session-persistence", "--disable-slash-commands",
             "--max-turns", str(max(1, int(max_turns)))]
    if effort:
        argv += ["--effort", effort]
    return argv


def mcp_server_env(spec: McpSpec, calls_path: Path) -> dict[str, str]:
    """Environment of the MCP server process (non-secret: it is written to a file). Paths and modes
    are passed explicitly so the server sees exactly this process's data dir, DB and DRY_RUN."""
    import config

    cs = config.get_settings()
    a = get_assistant_settings()
    env = {k: os.environ[k] for k in _MCP_ENV_PASS if k in os.environ}
    env.update({
        "PYTHONPATH": str(REPO_ROOT), "PYTHONUTF8": "1",
        "CLIPAGENT_DATA_DIR": str(cs.data_dir), "CLIPAGENT_DB_PATH": str(cs.db_path),
        "CLIPAGENT_STRATEGY_PATH": str(cs.strategy_path), "CLIPAGENT_CONFIG_FILE": str(cs.config_file),
        "VAULT_DIR": str(a.vault_dir), "DRY_RUN": "true" if cs.dry_run else "false",
        "FAKE_ASSISTANT": "true" if a.fake else "false", "ASSISTANT_BACKEND": a.backend,
        "CLIPAGENT_MCP_AGENT": spec.agent, "CLIPAGENT_MCP_ALLOWED": ",".join(spec.allowed),
        "CLIPAGENT_MCP_CHANNEL": spec.channel, "CLIPAGENT_MCP_SOURCE": spec.source,
        "CLIPAGENT_MCP_MESSAGE_ID": "" if spec.message_id is None else str(spec.message_id),
        "CLIPAGENT_MCP_RUN_ID": "" if spec.run_id is None else str(spec.run_id),
        "CLIPAGENT_MCP_CALLS": str(calls_path), "CLIPAGENT_MCP_HOME": str(home_dir()),
    })
    if a.claude_bin:
        env["ASSISTANT_CLAUDE_BIN"] = a.claude_bin
    return env


def mcp_config(spec: McpSpec, calls_path: Path) -> dict[str, Any]:
    return {"mcpServers": {MCP_SERVER_NAME: {"type": "stdio", "command": sys.executable,
                                             "args": ["-m", "assistant.mcp_server"],
                                             "env": mcp_server_env(spec, calls_path)}}}


def read_calls(path: Path) -> list[Any]:
    from assistant import tools

    out = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for ln in lines:
        try:
            d = json.loads(ln)
            out.append(tools.ToolCall(str(d["agent"]), str(d["name"]), dict(d.get("args") or {}), bool(d["ok"]),
                                      str(d.get("result", ""))))
        except (ValueError, KeyError, TypeError):
            continue
    return out


def run_prompt(prompt: str, *, system: str, role: str = "main", mcp: McpSpec | None = None,
               effort: str | None = None, web_search: bool = False, max_turns: int | None = None,
               count_turn: bool = True, alert_channel: str = "system", timeout: float | None = None,
               full: bool = False, channel: str = "system", model: str | None = None) -> CCResult:
    """One headless Claude Code run (tools via MCP when ``mcp`` is given; Claude Code's own tools,
    his folders and the channel's resumed session too when ``full``). Raises
    ``AssistantBudgetExceeded`` (paused | limit | turns) or :class:`ClaudeCodeError`."""
    s = get_assistant_settings()
    check_guards(count_turn=count_turn)
    exe = find_claude()
    if exe is None:
        raise ClaudeCodeError("not_installed", "Claude Code is not installed")
    timeout = timeout or (s.cc_task_timeout_sec if full else s.cc_timeout_sec)
    run_tag = uuid.uuid4().hex[:12]
    cfg_path = calls_path = settings_path = agents_path = None
    resume = session_for(channel) if full else None
    if mcp is not None:
        calls_path = runs_dir() / f"{run_tag}.calls.jsonl"
        cfg_path = runs_dir() / f"{run_tag}.mcp.json"
        cfg_path.write_text(json.dumps(mcp_config(mcp, calls_path)), encoding="utf-8")
    if full:
        settings_path, agents_path = prepare_full_run(run_tag, chrome=s.chrome,
                                                      mcp_tools=list(mcp.allowed) if mcp else None)
    env = base_env(extra={"MCP_TIMEOUT": "30000", "MCP_TOOL_TIMEOUT": str(int(timeout * 1000))})
    try:
        models = [model] if model else model_candidates(role)
        i = 0
        while i < len(models):
            alias = models[i]
            if full:
                argv = build_full_argv(str(exe), model=alias, system=system + full_note(chrome=s.chrome),
                                       mcp_config=str(cfg_path) if cfg_path else None,
                                       settings_path=str(settings_path),
                                       agents_path=str(agents_path) if agents_path else None,
                                       add_dirs=[str(d) for d in working_dirs()[1:]], resume=resume,
                                       effort=effort, max_turns=max_turns or s.cc_max_turns, chrome=s.chrome)
            else:
                argv = build_argv(str(exe), model=alias, system=system + (TOOL_NOTE if mcp else ""),
                                  allowed_tools=list(mcp.allowed) if mcp else [],
                                  mcp_config=str(cfg_path) if cfg_path else None, effort=effort,
                                  web_search=web_search, max_turns=max_turns or (s.max_tool_rounds + 2))
            on_api = api_fallback_active()
            if on_api:
                argv += ["--max-budget-usd", f"{max(_api_budget_left(), 0.01):.2f}"]
            if count_turn:
                db.kv_incr(KV_TURNS.format(day=db.local_today().isoformat()))
            try:
                done = _runner(argv, input=prompt, env=env, cwd=str(REPO_ROOT if full else work_dir()),
                               timeout=timeout)
            except subprocess.TimeoutExpired:
                raise ClaudeCodeError("timeout", f"Claude Code took longer than {int(timeout)} s") from None
            except OSError as exc:
                raise ClaudeCodeError("blocked", blocked_message(exc) or f"could not start Claude Code: {exc}") from exc
            try:  # plan usage (rate_limit_event lines in the stream-json transcript)
                from assistant import usage

                usage.record_events(done.stdout)
            except Exception as exc:
                log.info("usage events not recorded: %s", exc)
            data = parse_output(done.stdout)
            if data is not None and ((not data.get("is_error") and done.returncode == 0)
                                     or data.get("subtype") == "error_max_turns"):
                res = _result(data, alias)
                res.billed_to_api = on_api
                if calls_path is not None:
                    res.tool_calls = read_calls(calls_path)
                if not model:
                    _remember_model(role, alias, res.model)
                db.kv_delete(KV_LAST_ERROR)
                if full:
                    _record_session(channel, res, resumed=resume is not None)
                return res
            text = str((data or {}).get("result") or "") or (done.stderr or done.stdout or "").strip()
            kind = classify_error(text, data)
            if resume and kind == "error" and re.search(r"no conversation found|session.{0,40}not found", text, re.I):
                log.info("session %s cannot be resumed; starting a new one", resume)
                db.kv_delete(KV_SESSION.format(channel=channel))
                resume = None
                continue
            if "pro plan" in text.lower():
                db.kv_set(KV_PLAN_HINT, "Pro")
            last = i == len(models) - 1
            if kind in ("model", "model_limit") and not last:
                log.info("claude model %s unavailable (%s); trying %s", alias, kind, models[i + 1])
                i += 1
                continue
            db.kv_set(KV_LAST_ERROR, {"kind": kind, "text": text[:300], "at": db.now_iso()})
            if kind in ("limit", "model_limit") and on_api:
                raise _budget_exc("daily", f"API fallback stopped: {text[:120]}")
            if kind in ("limit", "model_limit"):
                until = _set_backoff(text, alert_channel=alert_channel)
                raise _budget_exc("limit", f"Claude plan limit: {text[:120]}", friendly_time(until))
            if kind == "model":
                raise ClaudeCodeError("model", f"none of the models {models} is available on this Claude plan")
            raise ClaudeCodeError(kind, text[:300] or f"claude exited with code {done.returncode}")
        raise ClaudeCodeError("error", "no model to try")  # pragma: no cover - models is never empty
    except ClaudeCodeError as exc:
        if calls_path is not None:
            exc.calls = read_calls(calls_path)
        raise
    finally:
        for f in (calls_path, cfg_path, settings_path, agents_path):
            if f is not None:
                f.unlink(missing_ok=True)


def _result(data: Mapping[str, Any], alias: str) -> CCResult:
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    mu = data.get("modelUsage") if isinstance(data.get("modelUsage"), dict) else {}
    model = max(mu, key=lambda k: (mu[k] or {}).get("outputTokens", 0) if isinstance(mu[k], dict) else 0) if mu else alias
    subtype = str(data.get("subtype") or "")
    status = "rounds" if subtype == "error_max_turns" else ("denied" if data.get("stop_reason") == "refusal" else "ok")
    try:
        notional = float(data.get("total_cost_usd") or 0.0)
    except (TypeError, ValueError):
        notional = 0.0
    return CCResult(text=str(data.get("result") or "").strip(), model=str(model), alias=alias,
                    input_tokens=int(usage.get("input_tokens") or 0), output_tokens=int(usage.get("output_tokens") or 0),
                    cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
                    cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
                    notional_usd=notional, session_id=data.get("session_id"),
                    num_turns=int(data.get("num_turns") or 0), status=status)


def record_notional(usd: float) -> None:
    if usd > 0:
        db.kv_incr(KV_NOTIONAL.format(day=db.local_today().isoformat()), int(round(usd * 1_000_000)))


def notional_today() -> float:
    return int(db.kv_get(KV_NOTIONAL.format(day=db.local_today().isoformat()), 0) or 0) / 1_000_000


# ---------------------------------------------------------------------------
# backend object (runtime.get_backend() returns this when ASSISTANT_BACKEND=claude_code)
# ---------------------------------------------------------------------------

class ClaudeCodeBackend:
    """Marker + single-shot adapter. ``whole_run``: the runtime hands the entire agent run (tool
    loop included) to :func:`run_prompt` instead of calling :meth:`create` per round."""

    name = "claude_code"
    fake = False
    whole_run = True

    def __init__(self) -> None:
        self.requests: list[Any] = []

    def create(self, req: Any) -> Any:
        """A tool-less single answer (eval judge, ad-hoc callers of the per-call interface)."""
        from assistant import runtime

        self.requests.append(req)
        last = req.messages[-1]["content"] if req.messages else ""
        prompt = last if isinstance(last, str) else json.dumps(last, default=str)
        res = run_prompt(prompt, system="\n\n".join(b for b in req.system if b), role="sub", max_turns=1,
                         effort=req.effort)
        record_notional(res.notional_usd)
        usage = runtime.Usage(res.input_tokens, res.output_tokens, res.cache_read_tokens, res.cache_write_tokens)
        return runtime.ModelReply([{"type": "text", "text": res.text}], res.text, [], "end_turn", usage,
                                  res.model, res.session_id)


# ---------------------------------------------------------------------------
# status (settings page, doctor, dashboard)
# ---------------------------------------------------------------------------

_status_cache: dict[str, Any] = {}
STATUS_TTL = 60.0


def _quick(argv: list[str], timeout: float = 20) -> Completed:
    return _runner(argv, input=None, env=base_env(), cwd=str(work_dir()), timeout=timeout)


def auth_status(exe: Path) -> dict[str, Any]:
    """``claude auth status --json`` (logged_in, auth_method, raw fields)."""
    try:
        done = _quick([str(exe), "auth", "status", "--json"])
    except (OSError, subprocess.SubprocessError) as exc:
        return {"logged_in": False, "error": blocked_message(exc) if isinstance(exc, OSError) else str(exc)}
    try:
        data = json.loads(done.stdout.strip() or "{}")
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {"logged_in": bool(data.get("loggedIn")), "auth_method": data.get("authMethod"),
            "subscription": data.get("subscriptionType") or data.get("subscription"),
            "email": data.get("email"), "error": None if done.returncode in (0, 1) else done.stderr[:200]}


def status(*, probe: bool = True, refresh: bool = False) -> dict[str, Any]:
    """{installed, version, path, logged_in, plan_hint, model_in_use, sub_model_in_use, turns_today,
    turns_cap, backoff_until, notional_today, last_error, error}. ``probe`` runs the CLI (cached)."""
    s = get_assistant_settings()
    exe = find_claude()
    out: dict[str, Any] = {
        "backend": s.backend, "installed": exe is not None, "path": str(exe) if exe else None, "version": None,
        "logged_in": None, "plan_hint": db.kv_get(KV_PLAN_HINT), "model_in_use": model_in_use("main"),
        "sub_model_in_use": model_in_use("sub"), "turns_today": turns_today(), "turns_cap": s.max_turns_per_day,
        "backoff_until": None, "backoff_text": "", "notional_today": round(notional_today(), 4),
        "last_error": db.kv_get(KV_LAST_ERROR), "error": None, "auth_method": None,
    }
    until = backoff_until()
    if until is not None:
        out["backoff_until"] = db.to_iso(until)
        out["backoff_text"] = friendly_time(until)
    if not probe or exe is None:
        return out
    key = str(exe)
    cached = _status_cache.get(key)
    if cached and not refresh and time.monotonic() - cached[0] < STATUS_TTL:
        out.update(cached[1])
        return out
    probed: dict[str, Any] = {}
    try:
        v = _quick([key, "--version"], 15)
        probed["version"] = (v.stdout.strip().split() or [None])[0]
    except (OSError, subprocess.SubprocessError) as exc:
        probed["error"] = blocked_message(exc) if isinstance(exc, OSError) else str(exc)
    if not probed.get("error"):
        a = auth_status(exe)
        probed["logged_in"] = a["logged_in"]
        probed["auth_method"] = a.get("auth_method")
        if a.get("subscription"):
            probed["plan_hint"] = str(a["subscription"]).capitalize()
        if a.get("error"):
            probed["error"] = a["error"]
    _status_cache[key] = (time.monotonic(), probed)
    out.update(probed)
    return out


def forget_status_cache() -> None:
    _status_cache.clear()


# ---------------------------------------------------------------------------
# install + sign-in (the setup wizard drives this; one flow at a time)
# ---------------------------------------------------------------------------

INSTALL_DOC = "https://code.claude.com/docs/en/setup"
_URL_RE = re.compile(r"https://[^\s\"'<>]+")
_URL_HOSTS = ("claude.ai", "claude.com", "platform.claude.com", "console.anthropic.com", "anthropic.com")
CODE_RE = re.compile(r"^[A-Za-z0-9_\-#.~]{8,600}$")


def install_argv() -> list[str]:
    """Anthropic's official native installer, run as the current user (no admin). Fixed command."""
    if _is_windows():
        ps = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        return [str(ps), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                "irm https://claude.ai/install.ps1 | iex"]
    return ["/bin/bash", "-c", "curl -fsSL https://claude.ai/install.sh | bash"]


def safe_login_url(text: str) -> str | None:
    for m in _URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(".,)")
        host = url.split("/", 3)[2].split(":")[0].lower()
        if any(host == h or host.endswith("." + h) for h in _URL_HOSTS):
            return url
    return None


ACTIVE_STATES = ("installing", "signing_in", "testing")


class SignIn:
    """Install (if needed) -> ``claude auth login`` (browser) -> one test prompt. Thread-based;
    :meth:`snapshot` is what the wizard polls."""

    LOGIN_TIMEOUT = 600.0
    INSTALL_TIMEOUT = 300.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._cancel = threading.Event()
        self.reset()

    def reset(self) -> None:
        self.state, self.message, self.url, self.model = "idle", "", None, None
        self.log: deque[str] = deque(maxlen=60)
        self.started_at: float | None = None

    # -- public ---------------------------------------------------------------
    def start(self, on_success: Callable[[str], None] | None = None) -> bool:
        with self._lock:
            if self.state in ACTIVE_STATES:
                return False
            self.reset()
            self._cancel.clear()
            self.state, self.message, self.started_at = "starting", "Starting...", time.monotonic()
            self._thread = threading.Thread(target=self._run, args=(on_success,), name="claude-signin", daemon=True)
            self._thread.start()
            return True

    def cancel(self) -> None:
        self._cancel.set()
        p = self._proc
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass
        if self.state in ACTIVE_STATES or self.state == "starting":
            self._set("cancelled", "Cancelled. Press Sign in when you are ready.")

    def submit_code(self, code: str) -> bool:
        """Paste-code fallback: hand the code Claude's page showed straight to Claude Code's prompt."""
        code = (code or "").strip()
        p = self._proc
        if self.state != "signing_in" or p is None or p.stdin is None or not CODE_RE.match(code):
            return False
        try:
            p.stdin.write((code + "\n").encode("utf-8"))
            p.stdin.flush()
        except OSError:
            return False
        self._set("signing_in", "Code sent. Finishing sign-in...")
        return True

    def snapshot(self) -> dict[str, Any]:
        return {"state": self.state, "message": self.message, "url": self.url, "model": self.model,
                "log": list(self.log)[-12:], "active": self.state in ACTIVE_STATES or self.state == "starting",
                "manual": INSTALL_DOC}

    # -- worker ---------------------------------------------------------------
    def _set(self, state: str, message: str) -> None:
        self.state, self.message = state, message

    def _run(self, on_success: Callable[[str], None] | None) -> None:
        try:
            self._flow(on_success)
        except Exception as exc:  # never leave the wizard spinning
            log.exception("claude sign-in failed")
            self._set("error", f"Something went wrong: {type(exc).__name__}. Try again.")
        finally:
            self._proc = None

    def _flow(self, on_success: Callable[[str], None] | None) -> None:
        exe = find_claude()
        if exe is None:
            self._set("installing", "Installing Claude Code (about a minute; no admin rights needed)...")
            # The official installer can linger (or open its own sign-in) after claude.exe is in
            # place, so move on as soon as the executable appears instead of waiting for it to exit.
            code = self._stream(install_argv(), self.INSTALL_TIMEOUT, stdin=False,
                                stop_when=lambda: find_claude() is not None)
            if self._cancel.is_set():
                return
            exe = find_claude()
            if exe is None:
                self._set("error", "Claude Code could not be installed automatically "
                                   f"(installer exit code {code}). Install it by hand from the link below, then "
                                   "press Sign in again.")
                return
            db.log_event("setup", "Claude Code installed", data={"path": str(exe)})
        forget_status_cache()
        a = auth_status(exe)
        if a.get("error") and not a["logged_in"] and "blocked" in str(a["error"]).lower():
            self._set("error", str(a["error"]))
            return
        if not a["logged_in"]:
            self._set("signing_in", "Your browser is opening Claude's sign-in page. Sign in with your Claude "
                                    "account and press Authorize, then come back here.")
            code = self._stream([str(exe), "auth", "login", "--claudeai"], self.LOGIN_TIMEOUT, stdin=True)
            if self._cancel.is_set():
                return
            forget_status_cache()
            a = auth_status(exe)
            if not a["logged_in"]:
                self._set("error", "Sign-in did not finish" + (" (it timed out)" if code is None else "")
                          + ". Press Sign in to try again.")
                return
            db.log_event("setup", "Claude account signed in for the assistant")
        self._set("testing", "Signed in. Asking Claude a one-word test question...")
        try:
            res = run_prompt("Reply with the single word OK.", system="You are a connectivity check. Reply OK.",
                             role="main", max_turns=1, count_turn=False, effort="low", timeout=180)
        except ClaudeCodeError as exc:
            msg = {"auth": "Claude did not accept the sign-in. Press Sign in again.",
                   "blocked": str(exc), "timeout": "Claude did not answer in time. Press Sign in to retry."}
            self._set("error", msg.get(exc.kind, f"The test question failed: {str(exc)[:160]}"))
            return
        except Exception as exc:  # AssistantBudgetExceeded: plan limit
            self._set("error", f"Signed in, but Claude is not answering yet: {str(exc)[:160]}")
            return
        self.model = res.model
        if on_success is not None:
            on_success(res.model)
        self._set("done", f"Signed in with your Claude account. {get_assistant_settings().name} is using "
                          f"{res.model}.")

    def _stream(self, argv: list[str], timeout: float, *, stdin: bool,
                stop_when: Callable[[], bool] | None = None) -> int | None:
        """Run ``argv``, collecting output into the log (and the sign-in URL). Returns the exit code
        (None on timeout/cancel)."""
        try:
            p = subprocess.Popen(argv, stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=base_env(),
                                 cwd=str(work_dir()), **_popen_kwargs())
        except OSError as exc:
            self._set("error", blocked_message(exc) or f"Could not start: {exc}")
            self._cancel.set()
            return None
        self._proc = p
        buf = ""

        def pump() -> None:
            nonlocal buf
            assert p.stdout is not None
            while True:
                chunk = p.stdout.read1(4096) if hasattr(p.stdout, "read1") else p.stdout.read(4096)
                if not chunk:
                    return
                text = chunk.decode("utf-8", "replace")
                buf = (buf + text)[-8000:]
                for line in text.splitlines():
                    line = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", line).strip()
                    if line and "code=" not in line:  # never echo OAuth query strings into the log
                        self.log.append(line[:200])
                url = safe_login_url(buf)
                if url and not self.url:
                    self.url = url

        t = threading.Thread(target=pump, daemon=True)
        t.start()
        deadline = time.monotonic() + timeout
        next_check = time.monotonic() + 3
        while p.poll() is None:
            if stop_when is not None and time.monotonic() >= next_check:
                next_check = time.monotonic() + 3
                if stop_when():
                    try:
                        p.terminate()
                    except OSError:
                        pass
                    t.join(timeout=2)
                    return 0
            if self._cancel.is_set() or time.monotonic() > deadline:
                try:
                    p.terminate()
                except OSError:
                    pass
                return None
            time.sleep(0.25)
        t.join(timeout=2)
        return p.returncode


SIGNIN = SignIn()


def sign_out() -> bool:
    """``claude auth logout`` in ClipAgent's own Claude Code profile (his own Claude Code stays signed in)."""
    exe = find_claude()
    ok = True
    if exe is not None:
        try:
            ok = _quick([str(exe), "auth", "logout"], 30).returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
    forget_status_cache()
    SIGNIN.reset()
    for role in ("main", "sub"):
        db.kv_delete(KV_MODEL.format(role=role))
    db.log_event("setup", "Claude account signed out of the assistant")
    return ok
