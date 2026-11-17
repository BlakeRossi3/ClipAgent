"""The staff (agents/*.md) at work: who they are, and a record of every job Cecil hands them.

Blake: "Most of the staff isn't really getting used ... They seem kind of useless because Cecil does
the vast majority of things." Two causes, both handled here:

* **Invisible work.** On the full-power backend (every spoken turn, background task, text and
  dashboard turn) Cecil hands work to his staff through Claude Code's own ``Agent`` tool
  (``subagent_type`` = the staff id), not through our ``delegate`` tool, so nothing wrote an
  ``as_agent_runs`` row and the Staff page showed them idle. :class:`StaffTracker` watches a run's
  tool calls (the SDK stream in ``assistant/brain.py``; the stream-json transcript in
  ``assistant/claude_code.run_prompt``) and records each hand-off as a run of that staff member: opened
  ``running`` when the Agent call starts, closed ``ok``/``error`` with the result when it returns.
* **Never asked.** The spoken prompt never mentioned the staff. :func:`roster_text` is the
  "who does what" block every prompt now carries, generated from agents/*.md so renames and new hires
  stay in step, with :data:`HAND_OFF_RULE`: work in a specialist's area goes to that specialist.

No model calls, no network. Unknown ``subagent_type`` values (Claude Code's built-in general-purpose
or Explore agents) are not staff and are not recorded.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import db

log = logging.getLogger(__name__)

AGENT_TOOLS = frozenset({"Agent", "Task"})   # Claude Code's sub-agent tool (renamed Task -> Agent in 2.x)
TASK_CHARS = 2000
SUMMARY_CHARS = 2000

HAND_OFF_RULE = (
    "Your staff are specialists, and using them is part of your job: when a request falls in one of their areas "
    "and is more than a one-line answer, hand it to that specialist with the Agent tool (subagent_type = their id) "
    "and a self-contained brief with every fact they need; several can work at once. Do quick things yourself. "
    "Their results come back to you; you answer him in your own voice. He sees on the Staff page who did what.")


def roster(*, include_main: bool = False) -> list[dict[str, str]]:
    """[{name, display, description}] from agents/*.md (broken files are skipped, never raised)."""
    try:
        from assistant import registry

        agents = registry.load_agents(skills=registry.load_skills(strict=False), strict=False)
    except Exception as exc:   # a prompt must still be built
        log.warning("staff roster unavailable: %s", exc)
        return []
    out = []
    for name, spec in sorted(agents.items()):
        if name == "main" and not include_main:
            continue
        display = str(spec.extra.get("display_name") or name.replace("-", " ").capitalize())
        out.append({"name": name, "display": display, "description": " ".join(str(spec.description).split())})
    return out


def staff_ids() -> set[str]:
    return {r["name"] for r in roster()}


def resolve(ref: str | None) -> str | None:
    """A staff id from an id or a display name ("Allen", "researcher"); None when nobody matches."""
    low = " ".join(str(ref or "").split()).lower()
    if not low:
        return None
    for r in roster():
        if low in (r["name"].lower(), r["display"].lower()):
            return r["name"]
    return None


def roster_text(*, heading: str = "# Your staff") -> str:
    """The "who does what" block for prompts: one line per specialist, then :data:`HAND_OFF_RULE`."""
    rows = roster()
    if not rows:
        return ""
    lines = [f"- {r['name']} ({r['display']}): {r['description']}" for r in rows]
    return f"{heading}\n" + "\n".join(lines) + "\n" + HAND_OFF_RULE


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_text_of(c.get("text") if isinstance(c, dict) else c) for c in content if c)
    if isinstance(content, dict):
        return _text_of(content.get("text") or content.get("content"))
    return "" if content is None else str(content)


@dataclass
class _Open:
    run_id: int
    agent: str
    started: float


@dataclass
class StaffTracker:
    """Turns Agent-tool calls into staff runs. Feed it tool starts/results (:meth:`tool_use`,
    :meth:`tool_result`) or raw stream-json lines (:meth:`feed_line`); :meth:`close` ends anything
    still open (a run cut off by a cancel, timeout or crash)."""

    parent_id: int | None = None
    channel: str = ""
    model: str = ""
    open: dict[str, _Open] = field(default_factory=dict)
    recorded: list[int] = field(default_factory=list)
    lines_seen: int = 0
    _known: set[str] | None = None

    def _staff(self) -> set[str]:
        if self._known is None:
            self._known = staff_ids()
        return self._known

    def tool_use(self, tool_id: str | None, name: str, inp: Any) -> int | None:
        """An Agent call started; returns the new run id (None when it is not a staff hand-off)."""
        if not tool_id or name not in AGENT_TOOLS or tool_id in self.open or not isinstance(inp, dict):
            return None
        agent = str(inp.get("subagent_type") or "").strip()
        if agent not in self._staff():
            return None
        desc = " ".join(str(inp.get("description") or "").split())
        prompt = str(inp.get("prompt") or "").strip()
        task = (desc + ("\n\n" if desc and prompt else "") + prompt)[:TASK_CHARS] or "(no brief)"
        try:
            from assistant import store

            model = self.model
            if not model:
                from assistant.settings import get_assistant_settings

                model = get_assistant_settings().cc_subagent_model
            rid = store.record_agent_run(agent, task, model=model or "subagent", duration_ms=0, cost_usd=0.0,
                                         status="running", parent_id=self.parent_id)
        except Exception as exc:   # bookkeeping must never break the turn
            log.warning("staff run not recorded: %s", exc)
            return None
        self.open[tool_id] = _Open(rid, agent, time.monotonic())
        self.recorded.append(rid)
        db.log_event("assistant", f"{agent} took on a job", data={"run_id": rid, "agent": agent,
                                                                   "task": (desc or prompt)[:200],
                                                                   "channel": self.channel})
        return rid

    def tool_result(self, tool_id: str | None, *, ok: bool, text: Any = "") -> None:
        o = self.open.pop(tool_id or "", None)
        if o is None:
            return
        self._finish(o, "ok" if ok else "error", _text_of(text))

    def _finish(self, o: _Open, status: str, summary: str) -> None:
        try:
            from assistant import store

            store.finish_agent_run(o.run_id, status=status, duration_ms=int((time.monotonic() - o.started) * 1000),
                                   cost_usd=0.0, summary=summary.strip()[:SUMMARY_CHARS] or None)
        except Exception as exc:
            log.warning("staff run not closed: %s", exc)

    def close(self, status: str = "error", why: str = "the run ended before this job reported back") -> None:
        for tid in list(self.open):
            self._finish(self.open.pop(tid), status, why)

    # -- stream-json (claude -p --output-format stream-json) -----------------------------
    def feed_line(self, line: str) -> None:
        line = (line or "").strip()
        if not line.startswith("{"):
            return
        try:
            msg = json.loads(line)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        self.lines_seen += 1
        inner = msg.get("message") if isinstance(msg.get("message"), dict) else {}
        content = inner.get("content") if isinstance(inner.get("content"), list) else []
        if msg.get("type") == "assistant":
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    self.tool_use(b.get("id"), str(b.get("name") or ""), b.get("input"))
        elif msg.get("type") == "user":
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    self.tool_result(b.get("tool_use_id"), ok=not b.get("is_error"), text=b.get("content"))

    def feed_output(self, stdout: str) -> None:
        for ln in (stdout or "").splitlines():
            self.feed_line(ln)
