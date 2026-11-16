"""What is eating the owner's Claude usage: a breakdown of every model call in a window.

On his Claude Pro plan the turns cost $0 in the ledger, so the report prices every row at the API
list price ("API-equivalent dollars") - not what he pays, but a fair weight for comparing what uses
the plan's allowance. Data: ``llm_usage`` (one row per model call or turn, tokens included) and
``as_agent_runs`` (background tasks), plus :func:`overhead` - the fixed prompt a spoken turn carries
before a word of conversation.

``main.py assistant usage-report [--hours N]``, tool ``usage_breakdown``.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import timedelta
from typing import Any, Callable

import db

_LABELS = {
    "assistant.main": "spoken / chat turns with Cecil",
    "assistant.heartbeat": "heartbeat (checks in the background every few minutes)",
    "assistant.evals": "nightly self-tests",
}


def _label(purpose: str) -> str:
    if purpose in _LABELS:
        return _LABELS[purpose]
    if purpose.startswith("assistant."):
        return f"staff / background: {purpose.split('.', 1)[1]}"
    return f"Shorts pipeline: {purpose} (API key)"


def _cost(model: str, inp: int, out: int, cr: int, cw: int) -> float:
    from assistant import runtime

    try:
        return float(runtime.price_for(model).cost(inp, out, cr, cw))
    except Exception:
        return 0.0


def report(hours: float = 24) -> dict[str, Any]:
    from assistant import store

    end = db.utcnow()
    start = end - timedelta(hours=hours)
    rows = [r for r in db.llm_usage_between(db.to_iso(start), db.to_iso(end + timedelta(seconds=1))) if not r.fake]
    groups: dict[str, dict[str, Any]] = defaultdict(lambda: {"calls": 0, "input": 0, "cache_read": 0,
                                                            "cache_write": 0, "output": 0, "usd": 0.0})
    hourly: dict[str, float] = defaultdict(float)
    heavy = []
    for r in rows:
        g = groups[r.purpose]
        usd = _cost(r.model, r.input_tokens, r.output_tokens, r.cache_read_tokens, r.cache_write_tokens)
        g["calls"] += 1
        g["input"] += r.input_tokens
        g["cache_read"] += r.cache_read_tokens
        g["cache_write"] += r.cache_write_tokens
        g["output"] += r.output_tokens
        g["usd"] += usd
        hourly[db.local_date_of(r.ts).isoformat() + " " + db.parse_iso(r.ts).astimezone(db.local_tz()).strftime("%H:00")] += usd
        heavy.append((usd, r))
    total = sum(g["usd"] for g in groups.values()) or 0.0
    by = []
    for purpose, g in sorted(groups.items(), key=lambda kv: -kv[1]["usd"]):
        tokens = g["input"] + g["cache_read"] + g["cache_write"] + g["output"]
        by.append({"purpose": purpose, "what": _label(purpose), "calls": g["calls"], "tokens": tokens,
                   "avg_tokens_per_call": tokens // max(1, g["calls"]), "api_usd": round(g["usd"], 4),
                   "share_pct": round(100 * g["usd"] / total, 1) if total else 0.0,
                   "fresh_input": g["input"], "cache_read": g["cache_read"], "cache_write": g["cache_write"],
                   "output": g["output"]})
    heavy.sort(key=lambda x: -x[0])
    runs = store.list_agent_runs(500, since=db.to_iso(start))
    run_counts: dict[str, int] = defaultdict(int)
    for r in runs:
        run_counts[r.agent] += 1
    out = {"hours": hours, "calls": len(rows), "api_usd_total": round(total, 4), "by_purpose": by,
           "heaviest_calls": [{"at": r.ts, "what": _label(r.purpose), "model": r.model, "api_usd": round(u, 4),
                               "tokens": r.input_tokens + r.cache_read_tokens + r.cache_write_tokens + r.output_tokens}
                              for u, r in heavy[:5]],
           "busiest_hours": sorted(({"hour": h, "api_usd": round(v, 4)} for h, v in hourly.items()),
                                   key=lambda x: -x["api_usd"])[:5],
           "background_runs": dict(sorted(run_counts.items(), key=lambda kv: -kv[1])),
           "fixed_overhead_per_turn": overhead()}
    out["findings"] = findings(out)
    return out


def overhead() -> dict[str, int]:
    """Tokens a spoken turn sends before any conversation (ours only; Claude Code's own prompt comes on top)."""
    out = {"instructions": 0, "tool_definitions": 0, "tools": 0, "subagents": 0, "skills": 0}
    try:
        from assistant import brain, registry, tools

        tools.load_extensions()
        out["instructions"] = len(brain.system_prompt(lean=True)) // 4
        table = brain.get_brain().tool_table()
        out["tools"] = len(table)
        out["tool_definitions"] = sum(len(t.description) + len(json.dumps(t.input_schema))
                                      for n in table if (t := tools.REGISTRY.get(n))) // 4
        skills = registry.load_skills(strict=False)
        agents = registry.load_agents(skills=skills, strict=False)
        out["subagents"] = sum(len(a.description) + len(a.prompt) for a in agents.values()) // 4
        out["skills"] = sum(len(s.description) for s in skills.values()) // 4
    except Exception:
        pass
    out["total"] = out["instructions"] + out["tool_definitions"] + out["subagents"] + out["skills"]
    return out


def findings(rep: dict[str, Any]) -> list[str]:
    """Plain-English observations (what to fix first)."""
    f: list[str] = []
    if not rep["calls"]:
        return [f"No model calls recorded in the last {rep['hours']:g} hours."]
    top = rep["by_purpose"][0]
    f.append(f"Biggest user: {top['what']} - {top['share_pct']}% ({top['calls']} calls, "
             f"about {top['avg_tokens_per_call'] // 1000}k tokens each).")
    for g in rep["by_purpose"]:
        if g["purpose"] == "assistant.heartbeat" and g["calls"] >= 10:
            f.append(f"The heartbeat called the model {g['calls']} times; it can run less often "
                     "(HEARTBEAT_MINUTES) or be switched off.")
        if g["purpose"] == "assistant.main" and g["avg_tokens_per_call"] > 40_000:
            f.append(f"Each spoken turn re-reads about {g['avg_tokens_per_call'] // 1000}k tokens (the whole "
                     "conversation so far plus Cecil's setup, again for every tool step). Shorter sessions or an "
                     "earlier rollover would cut this.")
    bg = sum(g["share_pct"] for g in rep["by_purpose"] if g["purpose"] != "assistant.main"
             and g["purpose"].startswith("assistant."))
    if bg >= 30:
        f.append(f"Background work (staff tasks, heartbeat, imports) was {bg:.0f}% - not things you asked for live.")
    oh = rep.get("fixed_overhead_per_turn") or {}
    if oh.get("total"):
        f.append(f"Fixed setup sent with every turn: about {oh['total'] // 1000}k tokens ({oh.get('tools', 0)} tool "
                 f"descriptions are {oh.get('tool_definitions', 0) // 1000}k of it), before Claude Code's own prompt.")
    return f


def text(rep: dict[str, Any]) -> str:
    lines = [f"Usage, last {rep['hours']:g} h: {rep['calls']} model calls, about ${rep['api_usd_total']:.2f} "
             "at API prices (your plan pays $0; this is the weight against your allowance)."]
    for g in rep["by_purpose"][:8]:
        lines.append(f"  {g['share_pct']:5.1f}%  {g['what']}: {g['calls']} calls, ~{g['avg_tokens_per_call'] // 1000}k "
                     f"tokens/call, ${g['api_usd']:.2f}")
    if rep["busiest_hours"]:
        lines.append("Busiest hours: " + ", ".join(f"{h['hour']} (${h['api_usd']:.2f})" for h in rep["busiest_hours"][:3]))
    lines += ["Findings:", *[f"  - {x}" for x in rep["findings"]]]
    return "\n".join(lines)


def register_tools(register: Callable[..., None]) -> None:
    from assistant.tools import Tool, _obj

    register(Tool("usage_breakdown", "What is using his Claude allowance: every model call in the last N hours "
                  "(default 24) grouped by what made it, priced at API rates, with the heaviest calls, busiest hours "
                  "and plain findings.", _obj({"hours": {"type": "number"}}, []),
                  lambda ctx, a: report(float(a.get("hours") or 24)), owner="assistant.usage_report"), replace=True)
