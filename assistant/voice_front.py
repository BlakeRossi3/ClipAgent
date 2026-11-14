"""The fast voice front: what answers when the owner speaks (channel "voice").

Two layers, as in adewaskar/jarvis (MIT, (c) 2026 Aditya Dewaskar), whose bridge keeps a quick
conversational loop apart from the slow Claude Code worker:

* **front** (this module): one direct, streaming Messages API call per utterance on a small model
  (``VOICE_FRONT_MODEL``, default ``claude-haiku-5-5``; low effort, ~300 output tokens) with a tiny
  context: the persona, a plain-code status snapshot (:func:`status_snapshot`) and the last few voice
  exchanges. Only quick in-process tools (status, memory_search, ledger_summary, usage_status,
  open_url, desktop_mode_run) plus ``delegate_task``. Text streams out sentence by sentence
  (``on_sentence``) so speech starts on the first sentence while the rest is generated.
* **worker**: anything needing real work goes to :func:`delegate`, which runs the full agent
  (``runtime.run_turn(channel="voice-task")``: Claude Code with its tools, or the API loop) in a
  background thread with a fresh session and a short brief. When it finishes, the front model
  summarises the result into one to three spoken sentences, saved as a voice message of kind
  ``task_done`` (``meta.origin`` = who should speak it: "browser" or "voice"). The browser picks
  these up through ``/voice/api/status``; the background voice process through :class:`Announcer`.

Costs are recorded as ``assistant.voice_front`` usage and count against the assistant daily budget.
Without an API key (and outside fake mode) the front falls back to Claude Code with model ``haiku``,
no tools, no resume and one turn: slower (a process start), but far quicker than the full agent.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable

import db
from assistant import persona, registry, runtime, store, tools
from assistant.settings import AssistantSettings, get_assistant_settings

log = logging.getLogger(__name__)

AGENT = "voice_front"
MAX_TOKENS = 300
SUMMARY_MAX_TOKENS = 200
MAX_ROUNDS = 3
HISTORY_EXCHANGES = 10
TASK_KEEP = 12
KV_TASKS = "voice_front:tasks"
QUICK_TOOLS = ("memory_search", "ledger_summary", "usage_status", "open_url", "desktop_mode_run")
DELEGATE_MARK = "DELEGATE:"
TASK_CHANNEL = "voice-task"

FRONT_PROMPT = """# Right now you are the voice
You are talking out loud with {owner}. Everything you write is spoken by a text-to-speech voice, at once.

- Answer in one to three short, conversational sentences (under about sixty words). No lists,
  markdown, symbols or links. Say numbers the way a person would.
- Speak the answer itself. Never say the details are on the dashboard, the desktop or the screen, and
  never say you have pasted, posted or saved something for him to read, unless he asks for that.
- "What's going on", status, approvals, check-ins, money, the Shorts channel, plan usage and your
  background tasks: answer straight from the <status> block. No tool call is needed for those.
- Quick lookups only: memory_search (his notes), ledger_summary, usage_status, open_url, desktop_mode_run.
- Anything that needs real work (reading or changing code or files, research, the web or his browser,
  several steps, drafting something long, fixing or improving yourself): say one short in-character
  line first, such as "Right away, {h}. I'll look into that.", then call delegate_task with a clear,
  self-contained brief. You will report back out loud when it is done; never claim it is done now.
- If he asks how a task is going, answer from the tasks in <status>.
- If you do not know, say so in one sentence. Address him as "{h}" at most once."""

FALLBACK_NOTE = """
You have no tools in this mode. If the request needs real work, write on the first line exactly
"DELEGATE: <a clear, self-contained brief>" and on the next line your short spoken acknowledgement."""

SUMMARY_PROMPT = """You turn the result of a background task into what {name} says out loud to {owner}.
One to three short spoken sentences, the key facts first, no lists, markdown or links, never "see the
dashboard". Start naturally, for example "{H}, about the ...". Address him as "{h}" at most once."""

_DASHBOARD_POINTER = re.compile(
    r"\b(see|check|look at|open|read|find)\b.{0,25}\b(dashboard|history panel)\b|"
    r"\b(details|results|full (answer|text|report|list)|everything|it'?s|they'?re|pasted|posted|put|saved|left|"
    r"dropped|written)\b.{0,50}\b(on|to|in|onto) (your|the) (dashboard|desktop|screen|history)\b", re.I)
_URL = re.compile(r"\b(?:https?://|www\.)\S+", re.I)


@dataclass
class FrontResult:
    text: str
    status: str = "ok"                   # ok | budget | stopped | error
    cost_usd: float = 0.0
    task_id: str | None = None
    sentences: list[str] = field(default_factory=list)
    first_sentence_ms: float | None = None
    total_ms: float = 0.0
    backend: str = ""
    message_in_id: int | None = None
    message_out_id: int | None = None


# ---------------------------------------------------------------------------
# backend choice
# ---------------------------------------------------------------------------

_backend: Any = None
_api_backend: Any = None
_lock = threading.Lock()


def set_backend(backend: Any) -> None:
    """Tests: install a backend for the front (``None`` = choose from settings)."""
    global _backend
    _backend = backend


def reset() -> None:
    global _backend, _api_backend, _task_runner
    _backend = _api_backend = None
    _task_runner = None


class ClaudeCodeFront:
    """No API key: one tool-less ``claude -p --model haiku`` turn (no resume, no MCP, max-turns 1)."""

    name = "claude_code_front"
    fake = False
    streams = False

    def create(self, req: runtime.ModelRequest) -> runtime.ModelReply:
        from assistant import claude_code

        parts = []
        for m in req.messages:
            c = m["content"]
            parts.append(c if isinstance(c, str) else json.dumps(c, default=str))
        res = claude_code.run_prompt("\n\n".join(parts), system="\n\n".join(b for b in req.system if b) + FALLBACK_NOTE,
                                     role="voice", max_turns=1, effort="low", model="haiku", timeout=60)
        if not res.billed_to_api:
            claude_code.record_notional(res.notional_usd)
        usage = runtime.Usage(res.input_tokens, res.output_tokens, res.cache_read_tokens, res.cache_write_tokens)
        reply = runtime.ModelReply([{"type": "text", "text": res.text}], res.text, [], "end_turn", usage, res.model,
                                   res.session_id)
        reply.request_id = f"cc:{'api' if res.billed_to_api else 'plan'}:{res.session_id or ''}"
        return reply


def api_key_saved() -> bool:
    import config

    return bool(config.get_settings().anthropic_api_key)


def get_backend() -> Any:
    """Installed one; else fake (tests / FAKE_ASSISTANT; the runtime's FakeBackend when one is
    installed, so tests can script both layers with one object); else the API with the saved key
    (one cached client: the connection is reused, which matters for latency); else Claude Code."""
    global _api_backend
    if _backend is not None:
        return _backend
    s = get_assistant_settings()
    if s.fake:
        installed = runtime._backend
        return installed if isinstance(installed, runtime.FakeBackend) else runtime.FakeBackend()
    if api_key_saved():
        with _lock:
            if _api_backend is None:
                _api_backend = runtime.AnthropicBackend(timeout=30.0, max_retries=1)
            return _api_backend
    return ClaudeCodeFront()


def _call(backend: Any, req: runtime.ModelRequest, on_text: Callable[[str], None]) -> runtime.ModelReply:
    if getattr(backend, "streams", True) and hasattr(backend, "stream"):
        return backend.stream(req, on_text)
    reply = backend.create(req)
    if reply.text:
        on_text(reply.text)
    return reply


def _record(reply: runtime.ModelReply, fake: bool) -> float:
    rid = reply.request_id or ""
    if rid.startswith("cc:"):   # Claude Code fallback: plan runs cost $0 here, API-billed runs count
        cost = runtime.cost_of(reply.model, reply.usage) if rid.startswith("cc:api") else 0.0
        db.insert_llm_usage(model=reply.model, purpose=f"{runtime.PURPOSE_PREFIX}{AGENT}", tier=runtime.TIER,
                            input_tokens=reply.usage.input_tokens, output_tokens=reply.usage.output_tokens,
                            cache_read_tokens=reply.usage.cache_read_tokens,
                            cache_write_tokens=reply.usage.cache_write_tokens, cost_usd=cost,
                            request_id=rid[:120], fake=False)
        return cost
    return runtime._record_usage(AGENT, reply, fake)


# ---------------------------------------------------------------------------
# the status snapshot (plain code; what "what's going on" is answered from)
# ---------------------------------------------------------------------------

def _ago(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        mins = int((db.utcnow() - db.parse_iso(iso)).total_seconds() // 60)
    except Exception:
        return ""
    return "just now" if mins < 1 else (f"{mins} min ago" if mins < 90 else f"{mins // 60} h ago")


def _local_hm(iso: str | None) -> str:
    try:
        return db.parse_iso(iso).astimezone(db.local_tz()).strftime("%H:%M") if iso else ""
    except Exception:
        return ""


def status_snapshot() -> str:
    """Compact, speakable facts about right now. Every part is optional: a failing source is skipped."""
    s = get_assistant_settings()
    lines = [f"Local time: {db.local_now().strftime('%A %d %B, %H:%M')}"]

    def part(fn: Callable[[], list[str]]) -> None:
        try:
            lines.extend(fn())
        except Exception as exc:
            log.debug("status part skipped: %s", exc)

    def tasks() -> list[str]:
        out = []
        for t in list_tasks():
            if t["status"] == "running":
                out.append(f"Background task running (started {_ago(t['started'])}): {t['task']}")
            elif t.get("finished") and db.parse_iso(t["finished"]) > db.utcnow() - timedelta(hours=12):
                out.append(f"Background task finished {_ago(t['finished'])} ({t['status']}): {t['task']}"
                           + (f" -> {t['summary']}" if t.get("summary") else ""))
        running = [r for r in store.list_agent_runs(10) if r.status == "running"]
        if running and not any(t["status"] == "running" for t in list_tasks()):
            out.append(f"Agent run in progress: {running[0].agent} (started {_ago(running[0].ts)})")
        return out or ["Background tasks: none running"]

    def approvals() -> list[str]:
        pend = db.list_decisions("pending", limit=5)
        if not pend:
            return ["Approvals waiting: none"]
        return [f"Approvals waiting: {len(pend)}"] + [f"- #{d.id} {d.kind}: {d.summary[:120]}" for d in pend[:3]]

    def checkins() -> list[str]:
        today = db.local_today()
        rows = [c for c in store.list_checkins(None, limit=200) if db.local_date_of(c.scheduled_for) == today]
        return [f"Check-in today {_local_hm(c.scheduled_for)} ({c.status}): {c.question[:100]}" for c in rows[:4]]

    def money() -> list[str]:
        wk = store.week_start_of()
        total = store.income_total(wk, db.local_today())
        hours = sum(w.hours for w in store.list_work_log(since_week=wk))
        return [f"This week (from Monday {wk.isoformat()}): income logged ${total:,.2f}, hours logged {hours:g}"]

    def shorts() -> list[str]:
        snap = tools.studio_snapshot(7)
        top = snap["top_clips"][0] if snap["top_clips"] else None
        line = (f"Shorts: {snap['uploads_today']} uploaded today (cap {snap['upload_cap']}), "
                f"{snap['period_views']} views in 7 days over {snap['period_clips_tracked']} clips")
        if top:
            line += f"; best: {str(top['title'])[:60]!r} with {top['views']} views"
        if snap.get("pipeline_paused"):
            line += "; pipeline spending paused"
        return [line]

    def plan() -> list[str]:
        from assistant import usage

        u = usage.usage_status()
        bits = [f"{k} window {w['left_pct']}% left" for k, w in u["windows"].items() if w.get("left_pct") is not None]
        if u["limited"]:
            bits.append(f"plan limited until {u['limited_text']}")
        bits.append(f"{u['runs_today']} of {u['runs_cap']} Claude runs used today")
        spent = store.assistant_spent_today()
        bits.append(f"assistant API spend today ${spent:.2f} of ${s.daily_budget_usd:.2f}")
        return ["Usage: " + "; ".join(bits)]

    def events() -> list[str]:
        evs = db.list_events(limit=4)
        return ["Recent events:"] + [f"- {_local_hm(e.ts)} {e.module} ({e.level}): {e.message[:90]}" for e in evs] \
            if evs else []

    def heartbeat() -> list[str]:
        from assistant import heartbeat as hb

        line = hb.status_line()
        return [line] if line else []

    for fn in (tasks, approvals, checkins, money, shorts, plan, heartbeat, events):
        part(fn)
    return "\n".join(lines)


def _recent_voice(exclude_id: int | None) -> str:
    since = db.to_iso(db.utcnow() - timedelta(hours=12))
    msgs = [m for m in store.list_messages(HISTORY_EXCHANGES * 2 + 1, channel="voice", since=since)
            if m.id != exclude_id][: HISTORY_EXCHANGES * 2]
    if not msgs:
        return ""
    lines = [f"[{'him' if m.direction == 'in' else 'you'} {_local_hm(m.ts)}] {' '.join(m.body.split())[:300]}"
             for m in reversed(msgs)]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# sentence streaming + spoken-text rules
# ---------------------------------------------------------------------------

class SentenceStream:
    """Feed text deltas; get back finished sentences (the last piece waits for more text or flush)."""

    def __init__(self) -> None:
        self.buf = ""

    def feed(self, delta: str) -> list[str]:
        from voice import speech

        self.buf += delta
        parts = speech.split_sentences(self.buf)
        if len(parts) <= 1:
            return []
        done = parts[:-1]
        self.buf = parts[-1] + (" " if self.buf[-1:].isspace() else "")   # keep the word boundary
        return done

    def flush(self) -> list[str]:
        from voice import speech

        out, self.buf = speech.split_sentences(self.buf), ""
        return out


def clean_sentence(text: str, *, asked_for_details: bool = False, settings: AssistantSettings | None = None) -> str:
    """One sentence as it is spoken: no links, no "it's on the dashboard", persona rules applied."""
    t = _URL.sub("", text or "")
    if not asked_for_details and _DASHBOARD_POINTER.search(t):
        return ""
    return persona.polish(t, "voice", settings=settings).strip()


def _wants_details(heard: str) -> bool:
    return bool(re.search(r"\b(dashboard|screen|write (it|that) (down|up)|send me|text me|details)\b", heard, re.I))


# ---------------------------------------------------------------------------
# one utterance
# ---------------------------------------------------------------------------

def _tool_defs(fallback: bool) -> tuple[list[dict[str, Any]], set[str]]:
    if fallback:
        return [], set()
    quick = tools.available(list(QUICK_TOOLS))
    defs = [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in quick]
    defs.append({"name": "status_snapshot", "description": "Refresh the live status (tasks, approvals, money, "
                 "Shorts, usage, events). It is already in <status>; call only if he asks to re-check.",
                 "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}})
    defs.append({"name": "delegate_task",
                 "description": "Hand real work (code, files, research, web, browser, several steps, self-edits) to "
                                "your full self in the background. Returns at once; you report back aloud when done.",
                 "input_schema": {"type": "object", "additionalProperties": False, "required": ["task"],
                                  "properties": {"task": {"type": "string",
                                                          "description": "Self-contained brief: what he wants, "
                                                                         "every fact needed."}}}})
    return defs, {t.name for t in quick}


def system_blocks(s: AssistantSettings, *, fallback: bool = False) -> list[str]:
    skills = registry.load_skills()
    persona_text = persona._render(skills["persona"].body, s) if "persona" in skills else f"You are {s.name}."
    front = FRONT_PROMPT.format(owner=s.owner_name or "the owner", h=s.owner_honorific)
    return [persona_text + "\n\n" + front + (FALLBACK_NOTE if fallback else "")]


def respond(heard: str, *, on_sentence: Callable[[str], None] | None = None, origin: str = "voice",
            record: bool = True) -> FrontResult:
    """Answer one utterance fast. ``on_sentence(text)`` gets each spoken sentence as soon as it is
    complete. Records the in/out voice messages. Never raises for model trouble: it says so instead."""
    s = get_assistant_settings()
    h = s.owner_honorific
    t0 = time.monotonic()
    res = FrontResult(text="")
    in_id = store.add_message("in", "voice", heard, kind="chat") if record else None
    res.message_in_id = in_id

    def emit(sentence: str) -> None:
        c = clean_sentence(sentence, asked_for_details=_wants_details(heard), settings=s)
        if not c:
            return
        if res.first_sentence_ms is None:
            res.first_sentence_ms = (time.monotonic() - t0) * 1000
        res.sentences.append(c)
        if on_sentence is not None:
            try:
                on_sentence(c)
            except Exception as exc:  # a speaker problem must not stop the answer
                log.warning("on_sentence failed: %s", exc)

    if runtime.is_stopped():
        emit(runtime.CANNED["stopped"].format(h=h))
        res.status = "stopped"
    else:
        try:
            _converse(heard, s, res, emit, origin, in_id)
        except runtime.AssistantBudgetExceeded as exc:
            res.status = "budget"
            if not res.sentences:
                emit(runtime.CANNED.get(exc.reason, runtime.CANNED["daily"]).format(h=h, when=exc.when or "soon"))
        except Exception as exc:
            log.exception("voice front failed")
            db.log_event("voice", "voice front failed", level="error", data={"error": repr(exc)[:300]})
            res.status = "error"
            if not res.sentences:
                emit(f"Something went wrong on my side, {h}. I've logged it.")
    if not res.sentences:
        emit(f"Understood, {h}.")
    res.text = " ".join(res.sentences)
    res.total_ms = (time.monotonic() - t0) * 1000
    if record:
        res.message_out_id = store.add_message("out", "voice", res.text, kind="reply", agent=AGENT,
                                               cost_usd=round(res.cost_usd, 6), reply_to=in_id,
                                               meta={"task_id": res.task_id} if res.task_id else None)
    return res


def _converse(heard: str, s: AssistantSettings, res: FrontResult, emit: Callable[[str], None], origin: str,
              in_id: int | None) -> None:
    backend = get_backend()
    fallback = isinstance(backend, ClaudeCodeFront)
    res.backend = getattr(backend, "name", "")
    defs, quick = _tool_defs(fallback)
    system = system_blocks(s, fallback=fallback)
    recent = _recent_voice(in_id)
    user = ("<status>\n" + persona.escape_untrusted(status_snapshot()) + "\n</status>\n\n"
            + (f"<recent_voice>\n{persona.escape_untrusted(recent)}\n</recent_voice>\n\n" if recent else "")
            + f'<owner_message channel="voice">\n{persona.escape_untrusted(heard)}\n</owner_message>')
    messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
    turn = runtime._TurnBudget(s.max_turn_usd)
    ctx = tools.ToolContext(channel="voice", agent=AGENT, source="voice", message_id=in_id)
    for _ in range(MAX_ROUNDS):
        req = runtime.ModelRequest(AGENT, s.voice_front_model, system, messages, defs, MAX_TOKENS, "low")
        runtime.check_budget(runtime.estimate_cost(req), turn, s)
        splitter = SentenceStream()
        spoken_before = len(res.sentences)
        mark: dict[str, Any] = {"pending": "", "checked": not fallback}

        def on_text(delta: str) -> None:
            if not mark["checked"]:   # fallback mode: hold the first line back until we know it is no DELEGATE
                mark["pending"] += delta
                if "\n" not in mark["pending"] and len(mark["pending"]) < len(DELEGATE_MARK) + 2:
                    return
                mark["checked"] = True
                delta, mark["pending"] = mark["pending"], ""
                if delta.lstrip().upper().startswith(DELEGATE_MARK):
                    mark["delegate"] = True
                    return
            if mark.get("delegate"):
                return
            for sent in splitter.feed(delta):
                emit(sent)

        reply = _call(backend, req, on_text)
        cost = _record(reply, bool(getattr(backend, "fake", False)))
        turn.spent += cost
        res.cost_usd += cost
        if fallback:
            text = reply.text.strip()
            if text.upper().startswith(DELEGATE_MARK):
                brief, _, ack = text[len(DELEGATE_MARK):].strip().partition("\n")
                res.task_id = delegate(brief.strip() or heard, heard=heard, origin=origin)
                ack = ack.strip() or f"Right away, {s.owner_honorific}. I'll look into that."
                from voice import speech

                for sent in speech.split_sentences(ack):
                    emit(sent)
                return
            if mark["pending"]:
                for sent in splitter.feed(mark["pending"]):
                    emit(sent)
        for sent in splitter.flush():
            emit(sent)
        if not reply.tool_uses:
            return
        messages.append({"role": "assistant", "content": reply.content})
        results = []
        delegated_only = True
        for use in reply.tool_uses:
            if use.name == "delegate_task":
                task = str(use.input.get("task") or "").strip() or heard
                if res.task_id is None:
                    res.task_id = delegate(task, heard=heard, origin=origin)
                    out, ok = f"Task {res.task_id} started in the background.", True
                else:
                    out, ok = "Only one background task per request; already started.", False
            elif use.name == "status_snapshot":
                delegated_only = False
                out, ok = status_snapshot(), True
            else:
                delegated_only = False
                r = tools.dispatch(use.name, use.input, ctx, allowed=quick)
                out, ok = runtime.wrap_untrusted(f"tool:{use.name}", r.content), r.ok
            results.append({"type": "tool_result", "tool_use_id": use.id, "content": out, "is_error": not ok})
        if delegated_only:
            if len(res.sentences) == spoken_before:
                emit(f"Right away, {s.owner_honorific}. I'll look into that.")
            return
        messages.append({"role": "user", "content": results})
    if not res.sentences:
        emit(f"I couldn't finish that quickly, {s.owner_honorific}.")


# ---------------------------------------------------------------------------
# the slow worker (background tasks)
# ---------------------------------------------------------------------------

_tasks_lock = threading.Lock()
_threads: dict[str, threading.Thread] = {}
_task_runner: Callable[[str, dict[str, Any]], tuple[str, str]] | None = None


def set_task_runner(fn: Callable[[str, dict[str, Any]], tuple[str, str]] | None) -> None:
    """Tests: ``fn(brief, context) -> (full_text, status)`` instead of ``runtime.run_turn``."""
    global _task_runner
    _task_runner = fn


def list_tasks() -> list[dict[str, Any]]:
    v = db.kv_get(KV_TASKS, [])
    return v if isinstance(v, list) else []


def running_tasks() -> list[dict[str, Any]]:
    return [t for t in list_tasks() if t.get("status") == "running"]


def _save_task(task: dict[str, Any]) -> None:
    with _tasks_lock:
        rows = [t for t in list_tasks() if t.get("id") != task["id"]]
        rows.append(task)
        db.kv_set(KV_TASKS, rows[-TASK_KEEP:])


def get_task(task_id: str) -> dict[str, Any] | None:
    return next((t for t in list_tasks() if t.get("id") == task_id), None)


def brief_for(task: str, heard: str) -> str:
    return (f'{heard.strip()}\n\n<voice_task>\nHe asked this out loud; the voice front handed it to you as a '
            f'background task. Brief: {task.strip()}\nDo the work now. Finish with a short plain answer (what you '
            f'found or did); it will be summarised and spoken to him.\n</voice_task>')


def delegate(task: str, *, heard: str = "", origin: str = "voice") -> str:
    """Start the full agent on ``task`` in a background thread; returns the task id at once."""
    tid = uuid.uuid4().hex[:10]
    row = {"id": tid, "task": " ".join(task.split())[:300], "heard": heard[:300], "origin": origin,
           "status": "running", "started": db.now_iso(), "finished": None, "summary": ""}
    _save_task(row)
    db.log_event("voice", "voice task started", data={"task_id": tid, "task": row["task"][:200]})
    th = threading.Thread(target=_run_task, args=(row,), name=f"voice-task-{tid[:6]}", daemon=True)
    _threads[tid] = th
    th.start()
    return tid


def wait(task_id: str, timeout: float = 10.0) -> bool:
    th = _threads.get(task_id)
    if th is not None:
        th.join(timeout)
        return not th.is_alive()
    return True


def _run_task(row: dict[str, Any]) -> None:
    s = get_assistant_settings()
    try:
        context = {"status": status_snapshot()[:1200]}
    except Exception:
        context = {}
    brief = brief_for(row["task"], row["heard"] or row["task"])
    try:
        if _task_runner is not None:
            full, status = _task_runner(brief, context)
        else:
            # channel "voice-task" is not a resumed Claude Code channel: every task gets a fresh session
            r = runtime.run_turn(brief, channel=TASK_CHANNEL, context=context)
            full, status = r.text, r.status
    except Exception as exc:
        log.exception("voice task failed")
        full, status = runtime.CANNED["error"].format(h=s.owner_honorific), "error"
        db.log_event("voice", "voice task failed", level="error", data={"error": repr(exc)[:300]})
    try:
        summary = summarise(row["task"], full)
    except Exception as exc:
        log.warning("task summary failed: %s", exc)
        summary = fallback_summary(full)
    row.update(status="done" if status == "ok" else status, finished=db.now_iso(), summary=summary[:400])
    _save_task(row)
    try:
        store.add_message("out", "dashboard", persona.polish(full, "dashboard", settings=s), kind="voice_detail",
                          agent="main", meta={"task_id": row["id"], "task": row["task"]})
        store.add_message("out", "voice", summary, kind="task_done", agent=AGENT,
                          meta={"task_id": row["id"], "origin": row["origin"], "task": row["task"][:200]})
    except Exception as exc:
        log.warning("task result not recorded: %s", exc)
    db.log_event("voice", "voice task finished", data={"task_id": row["id"], "status": row["status"]})


def fallback_summary(full: str) -> str:
    from voice import speech

    sents = [clean_sentence(x) for x in speech.split_sentences(persona.polish(full or "", "voice"))]
    out = " ".join([x for x in sents if x][:3]).strip()
    return out or f"That task is finished, {get_assistant_settings().owner_honorific}."


def summarise(task: str, full: str) -> str:
    """The finished task's result as one to three spoken sentences (fast model; plain fallback)."""
    s = get_assistant_settings()
    backend = get_backend()
    system = [persona._render(SUMMARY_PROMPT, s).format(name=s.name, owner=s.owner_name or "the owner",
                                                       h=s.owner_honorific,
                                                       H=s.owner_honorific[:1].upper() + s.owner_honorific[1:])]
    user = (f"<task>{persona.escape_untrusted(task)}</task>\n\n"
            + runtime.wrap_untrusted("task_result", (full or "")[:6000]))
    req = runtime.ModelRequest(f"{AGENT}_summary", s.voice_front_model, system, [{"role": "user", "content": user}],
                               [], SUMMARY_MAX_TOKENS, "low")
    try:
        runtime.check_budget(runtime.estimate_cost(req), runtime._TurnBudget(s.max_turn_usd), s)
        reply = _call(backend, req, lambda _d: None)
        _record(reply, bool(getattr(backend, "fake", False)))
    except runtime.AssistantBudgetExceeded:
        return fallback_summary(full)
    from voice import speech

    sents = [clean_sentence(x, settings=s) for x in speech.split_sentences(reply.text)]
    out = " ".join([x for x in sents if x][:3]).strip()
    return out or fallback_summary(full)


# ---------------------------------------------------------------------------
# finished-task announcements
# ---------------------------------------------------------------------------

def task_events(after_id: int = 0, *, limit: int = 20) -> list[dict[str, Any]]:
    """Spoken ``task_done`` messages newer than ``after_id`` (oldest first)."""
    out = []
    for m in store.list_messages(limit, channel="voice", direction="out"):
        if m.kind == "task_done" and m.id > after_id:
            meta = m.meta if isinstance(m.meta, dict) else {}
            out.append({"id": m.id, "text": m.body, "origin": meta.get("origin", ""), "task_id": meta.get("task_id"),
                        "task": meta.get("task", "")})
    return sorted(out, key=lambda e: e["id"])


def latest_event_id() -> int:
    ev = task_events(0)
    return ev[-1]["id"] if ev else 0


class Announcer:
    """For the background voice process: the summaries of tasks it delegated, once each."""

    def __init__(self, origin: str = "voice"):
        self.origin = origin
        self.after: int | None = None

    def __call__(self) -> list[str]:
        if self.after is None:
            self.after = latest_event_id()
            return []
        evs = task_events(self.after)
        if evs:
            self.after = evs[-1]["id"]
        return [e["text"] for e in evs if e["origin"] in (self.origin, "any")]   # "any": heartbeat news
