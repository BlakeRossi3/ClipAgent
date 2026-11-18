"""Guardrails: the code-shape rules from docs/ENGINEERING.md, checked on every test run.

Blake: "too many things are failing and then not retrying" - the causes were overlapping systems doing one
job, errors thrown away, bookkeeping written after the work it describes had already finished, and lists
in kv updated with a read-then-write that two writers can interleave. These tests fail when NEW code
brings any of that back. Existing code that predates a rule is listed in ``guardrails_baseline.json``;
that list may shrink (fix one, it is no longer needed) but never grow.

After fixing a grandfathered site, refresh the baseline::

    .venv/bin/python tests/test_guardrails.py --update
"""

from __future__ import annotations

import ast
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = Path(__file__).with_name("guardrails_baseline.json")

# Modules allowed to read the raw usage signals directly (they report usage or compute retry timing, or
# ARE the gate). Everything that decides whether to START background work asks usage.wait_reason().
RAW_GATE_OK = {"assistant/usage.py", "assistant/claude_code.py", "assistant/brain.py", "assistant/handoff.py",
               "assistant/retry.py", "assistant/heartbeat.py", "assistant/schedule.py", "assistant/initiative.py",
               "assistant/runtime.py", "home.py", "setup_web.py"}
RAW_GATE_CALLS = ("constrained", "backoff_until")
# Where background threads may be started. New background work goes through assistant/tasks.py (workers)
# or assistant/usage.py (the deferred queue) so it is visible, retried and counted.
THREAD_OK = {"assistant/brain.py", "assistant/claude_code.py", "assistant/publish.py", "assistant/schedule.py",
             "assistant/selfedit.py", "assistant/semantic.py", "assistant/skill_catalog.py", "assistant/tasks.py",
             "assistant/usage.py", "home.py", "main.py", "voice/app.py", "voice/smart_turn.py", "voice/web.py",
             }


def _files() -> list[Path]:
    out = sorted(ROOT.glob("*.py")) + sorted((ROOT / "assistant").glob("*.py")) + sorted((ROOT / "voice").glob("*.py"))
    return [p for p in out if not p.name.startswith("seed_")]


def _rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def _innermost(tree: ast.AST) -> dict[int, str]:
    best: dict[int, tuple[int, str]] = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            span = (n.end_lineno or n.lineno) - n.lineno
            for ln in range(n.lineno, (n.end_lineno or n.lineno) + 1):
                if ln not in best or span < best[ln][0]:
                    best[ln] = (span, n.name)
    return {ln: name for ln, (_, name) in best.items()}


def silent_excepts() -> Counter:
    """``except Exception`` blocks that neither log, record, re-raise nor use the error."""
    out: Counter = Counter()
    for p in _files():
        src = p.read_text(encoding="utf-8")
        tree = ast.parse(src)
        where = _innermost(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            t = node.type
            if not (t is None or (isinstance(t, ast.Name) and t.id in ("Exception", "BaseException"))):
                continue
            body = ast.Module(body=node.body, type_ignores=[])
            if any(isinstance(n, (ast.Call, ast.Raise)) for n in ast.walk(body)):
                continue
            if node.name and node.name in {n.id for n in ast.walk(body) if isinstance(n, ast.Name)}:
                continue
            out[f"{_rel(p)}::{where.get(node.lineno, '<module>')}"] += 1
    return out


def _calls_in_order(fn: ast.AST, src: str) -> list[tuple[int, str, ast.Call]]:
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
    return sorted(((c.lineno, ast.get_source_segment(src, c.func) or "", c) for c in calls), key=lambda x: x[0])


def kv_read_then_write() -> Counter:
    """``x = kv_get(K)`` then ``kv_set(K, <something computed from x>)`` in one function: an append, merge or
    count that a second writer can interleave with. Use ``db.kv_update`` / ``db.kv_append``. (Replacing a
    whole value, e.g. a refreshed cache, is fine and not flagged.)"""
    out: Counter = Counter()
    for p in _files():
        if p.name == "db.py":
            continue
        src = p.read_text(encoding="utf-8")
        for fn in ast.walk(ast.parse(src)):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # names holding a kv value read in this function (and names computed from them, one level)
            tainted: dict[str, str] = {}
            for node in sorted((n for n in ast.walk(fn) if isinstance(n, ast.Assign)), key=lambda n: n.lineno):
                names = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
                keys = [tainted[x] for x in names if x in tainted]
                for c in (n for n in ast.walk(node.value) if isinstance(n, ast.Call)):
                    if (ast.get_source_segment(src, c.func) or "").endswith("kv_get") and c.args:
                        keys.append(ast.get_source_segment(src, c.args[0]) or "")
                for t in node.targets:
                    for nm in (n.id for n in ast.walk(t) if isinstance(n, ast.Name)):
                        if keys:
                            tainted[nm] = keys[0]
            for _line, name, c in _calls_in_order(fn, src):
                if name.endswith("kv_set") and len(c.args) >= 2:
                    key = ast.get_source_segment(src, c.args[0]) or ""
                    used = {n.id for n in ast.walk(c.args[1]) if isinstance(n, ast.Name)}
                    if any(tainted.get(u) == key for u in used):
                        out[f"{_rel(p)}::{fn.name}::{key}"] += 1
    return out


def write_after_start() -> Counter:
    """Bookkeeping written AFTER ``tasks.start``: a run that ends at once has already updated that record,
    and the late write overwrites it. Write first, or merge atomically (``store.merge_*_meta``)."""
    out: Counter = Counter()
    for p in _files():
        src = p.read_text(encoding="utf-8")
        for fn in ast.walk(ast.parse(src)):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            started = None
            for line, name, _c in _calls_in_order(fn, src):
                if name in ("tasks.start", "tasks.delegate"):
                    started = line
                elif started and line > started and name.split(".")[-1] in ("kv_set", "update_deferred", "update_backlog"):
                    out[f"{_rel(p)}::{fn.name}"] += 1
    return out


def raw_gate_calls() -> Counter:
    out: Counter = Counter()
    for p in _files():
        if _rel(p) in RAW_GATE_OK:
            continue
        src = p.read_text(encoding="utf-8")
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, ast.Call):
                name = (ast.get_source_segment(src, n.func) or "").split(".")[-1]
                if name in RAW_GATE_CALLS:
                    out[f"{_rel(p)}::{name}"] += 1
    return out


def thread_starts() -> Counter:
    out: Counter = Counter()
    for p in _files():
        if _rel(p) in THREAD_OK:
            continue
        src = p.read_text(encoding="utf-8")
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, ast.Call) and (ast.get_source_segment(src, n.func) or "").endswith("Thread"):
                out[_rel(p)] += 1
    return out


CHECKS = {"silent_excepts": silent_excepts, "kv_read_then_write": kv_read_then_write,
          "write_after_start": write_after_start}


def _baseline() -> dict[str, dict[str, int]]:
    return json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}


def _grown(name: str) -> list[str]:
    allowed = Counter(_baseline().get(name, {}))
    now = CHECKS[name]()
    return [f"{k} ({v} > {allowed.get(k, 0)})" for k, v in sorted(now.items()) if v > allowed.get(k, 0)]


def test_no_new_silent_excepts():
    """A broad ``except`` must log the error, record it, re-raise, or use it - never just pass/return.
    (A failure nobody can see is how "it failed after one second and nobody knew why" happened.)"""
    grown = _grown("silent_excepts")
    assert not grown, ("New silent `except Exception` blocks: log it (log.debug/warning with the error) or "
                       "record it. See docs/ENGINEERING.md rule 3.\n" + "\n".join(grown))


def test_no_new_kv_read_then_write():
    grown = _grown("kv_read_then_write")
    assert not grown, ("kv_get then kv_set of the same key loses updates when two writers interleave: use "
                       "db.kv_update(key, fn) or db.kv_append(key, item, keep=N). docs/ENGINEERING.md rule 4.\n"
                       + "\n".join(grown))


def test_no_bookkeeping_written_after_starting_the_work():
    grown = _grown("write_after_start")
    assert not grown, ("A record written after tasks.start() can overwrite what the finished run recorded: "
                       "write it before starting, or use store.merge_deferred_meta / merge_backlog_meta. "
                       "docs/ENGINEERING.md rule 5.\n" + "\n".join(grown))


def test_background_work_asks_the_one_gate():
    bad = raw_gate_calls()
    assert not bad, ("Code that decides whether to start background work must ask usage.wait_reason(), not "
                     "combine constrained()/backoff_until() itself. docs/ENGINEERING.md rule 1.\n"
                     + "\n".join(sorted(bad)))


def test_background_threads_only_through_the_runners():
    bad = thread_starts()
    assert not bad, ("Start background work through assistant/tasks.py (a worker) or assistant/usage.py (the "
                     "deferred queue), so it is visible, retried and counted. docs/ENGINEERING.md rule 2.\n"
                     + "\n".join(sorted(bad)))


def test_should_defer_only_reruns_the_same_job():
    from assistant import usage

    for name in ("auto_memory", "initiative_harvest", "heartbeat", "reel"):
        try:
            usage.should_defer(name, "anything")
        except ValueError:
            continue
        raise AssertionError(f"should_defer({name!r}) must refuse: deferral may only rerun a JOB_FUNCS job")


def test_baseline_is_not_stale():
    """Entries that no longer exist are dropped from the baseline (it only ever shrinks)."""
    stale = []
    for name, fn in CHECKS.items():
        now = fn()
        stale += [f"{name}: {k}" for k, v in _baseline().get(name, {}).items() if now.get(k, 0) < v]
    assert not stale, ("These grandfathered sites were fixed - shrink the baseline: "
                       ".venv/bin/python tests/test_guardrails.py --update\n" + "\n".join(stale))


if __name__ == "__main__":
    if "--update" in sys.argv:
        old = _baseline()
        data = {}
        for name, fn in CHECKS.items():
            now = fn()
            # never grow an entry past what was there: --update only records fixes
            prev = old.get(name)
            data[name] = {k: (min(v, prev.get(k, 0)) if prev is not None else v) for k, v in sorted(now.items())
                          if prev is None or prev.get(k, 0) > 0}
            data[name] = {k: v for k, v in data[name].items() if v > 0}
        BASELINE.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"baseline written: {', '.join(f'{k}={sum(v.values())}' for k, v in data.items())}")


def test_tests_never_point_self_edits_at_the_real_checkout():
    """``selfedit.set_app_dir(None)`` = the real repository. A test that resets to it lets a leftover background
    thread commit to / switch branches in the real checkout (it happened). Reset to a throwaway folder."""
    bad = [f"{p.name}:{i}" for p in sorted((ROOT / "tests").glob("*.py"))
           for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
           if "set_app_dir(None)" in line and "bad = " not in line and "``" not in line]
    assert not bad, "\n".join(bad)
