"""The code map: one generated page that tells Cecil (and anyone) where everything lives and who reads what.

Why: Cecil kept fixing one screen and leaving another showing the old number (the Studio counted live
views, the home tile still summed only YouTube Analytics and read 0). Exploring the code file by file to
find every reader burns plan usage; this page costs zero tokens to build (plain ``ast`` + regex, well
under a second) and a few thousand to read.

``docs/CODEMAP.md`` (git-ignored: regenerated, never merged):

* **Modules** - every module with the first line of its docstring and its public functions.
* **Shared data and who reads it** - each ``db`` helper, SQL table and kv key prefix used by two or more
  modules, with those modules. Change how one of them is read or shown -> check every module listed.
* **Scheduled jobs** - name, schedule and what it does (``main.job_specs``), so "is X refreshed often
  enough?" is answered without reading ``main.py``.
* **Pages and API routes** - path -> module.

:func:`ensure_fresh` rebuilds it when any source file is newer (initiative and self-repair call it before
they start; ``python main.py codemap`` rebuilds by hand). Pure apart from reading the source and writing
the one file; importing this module does nothing.
"""

from __future__ import annotations

import ast
import os
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = Path("docs") / "CODEMAP.md"
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "tests", "data", "node_modules", "evals", "skills",
             "vault_template", "extension", "windows", "deploy", "static", "templates", "docs", "agents"}
MAX_FUNCS = 10
#: db plumbing, not data: listing every user of these would bury the data readers that matter
DB_PLUMBING = re.compile(r"^(kv_\w+|log_event|list_events|local_\w+|now_iso|utcnow|to_iso|parse_iso|connect|init_db|"
                         r"transaction|schema_version|[A-Z]\w*)$")
#: demo/test-data writers are not readers to keep in step
IGNORED_USERS = {"seed_demo", "assistant.evals"}
MAX_READERS = 12
_TABLE_RE = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+([a-z_][a-z0-9_]*)\b")     # SQL is upper case here
_CREATE_RE = re.compile(r"CREATE\s+(?:VIRTUAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z_][a-z0-9_]*)")
_SQL_WORDS = {"select", "where", "values", "set", "the", "a", "an", "it", "them", "this", "that", "json_each",
              "pragma_table_info", "sqlite_master", "excluded", "t", "s", "u", "c"}
_ROUTE_RE = re.compile(r"@(?:router|app)\.(get|post|put|delete|api_route)\(\s*[\"']([^\"']*)[\"']")
_PREFIX_RE = re.compile(r"APIRouter\(\s*prefix\s*=\s*[\"']([^\"']*)[\"']")
_KV_CALL_RE = re.compile(r"\bkv_(?:get|set|set_if_absent|incr|delete|update|append)\(\s*(?:f?[\"']([^\"']+)[\"']|([A-Z_]+))")
_KV_CONST_RE = re.compile(r"^([A-Z][A-Z0-9_]*)\s*=\s*f?[\"']([a-z0-9_]+:[^\"']*)[\"']", re.M)


def _sources(root: Path) -> list[Path]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        out += [Path(dirpath) / f for f in sorted(filenames) if f.endswith(".py")]
    return out


def _mod(root: Path, path: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    parts = [p for p in rel.parts if p != "__init__"]
    return ".".join(parts) or rel.parts[0]


def _summary(tree: ast.Module) -> str:
    doc = ast.get_docstring(tree) or ""
    first = " ".join(doc.strip().split("\n\n")[0].split())
    return first[:220]


def _public_funcs(tree: ast.Module) -> list[str]:
    names = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            names.append(node.name)
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            names.append(node.name)
    return names


def _jobs(tree: ast.Module) -> list[tuple[str, str, str, str]]:
    """(name, cron|interval, schedule, description) of every ``JobSpec(...)`` literal in main.py."""
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "JobSpec"):
            continue
        a = node.args
        if len(a) < 4 or not (isinstance(a[0], ast.Constant) and isinstance(a[2], ast.Constant)):
            continue
        what = a[4].value if len(a) > 4 and isinstance(a[4], ast.Constant) else ""
        out.append((str(a[0].value), str(a[2].value), ast.unparse(a[3]), str(what)))
    return out


def _key_prefix(key: str) -> str:
    """kv key -> its stable prefix: ``main:job:{name}`` -> ``main:job:*``; ``pipeline:live:upload`` kept."""
    key = re.sub(r"\{[^}]*\}.*$", "*", key)
    parts = key.split(":")
    if parts[-1] != "*" and len(parts) > 2:
        parts = parts[:2] + ["*"]
    return ":".join(parts)


def build(root: Path | None = None) -> str:
    """The whole page (markdown). Pure: reads ``root``'s source, writes nothing."""
    root = root or ROOT
    files = _sources(root)
    modules: list[tuple[str, str, list[str]]] = []
    db_users: dict[str, set[str]] = defaultdict(set)
    table_users: dict[str, set[str]] = defaultdict(set)
    kv_users: dict[str, set[str]] = defaultdict(set)
    routes: list[tuple[str, str]] = []
    jobs: list[tuple[str, str, str, str]] = []
    db_funcs: set[str] = set()
    trees: dict[str, tuple[ast.Module, str]] = {}
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        name = _mod(root, path)
        trees[name] = (tree, text)
        if name == "db":
            db_funcs = set(_public_funcs(tree))
    for name, (tree, text) in trees.items():
        modules.append((name, _summary(tree), _public_funcs(tree)))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "db"
                    and node.attr in db_funcs):
                db_users[node.attr].add(name)
        for t in _TABLE_RE.findall(text):
            t = t.lower()
            if t not in _SQL_WORDS and len(t) > 2:
                table_users[t].add(name)
        consts = dict(_KV_CONST_RE.findall(text))
        for lit, const in _KV_CALL_RE.findall(text):
            key = lit or consts.get(const, "")
            if ":" in key:
                kv_users[_key_prefix(key)].add(name)
        for const_key in consts.values():
            kv_users[_key_prefix(const_key)].add(name)
        prefix = (_PREFIX_RE.search(text) or [None, ""])[1] if _PREFIX_RE.search(text) else ""
        for _verb, route in _ROUTE_RE.findall(text):
            routes.append((prefix + route, name))
        if name == "main":
            jobs += _jobs(tree)
    created = {t for _tree, text in trees.values() for t in _CREATE_RE.findall(text)}
    known_tables = {t for t in table_users if t in created}
    lines = ["# Code map", "",
             "Generated by `codemap.py` from the source (zero tokens). Do not edit; it is rebuilt when code changes "
             "(`python main.py codemap`). Read this before exploring the code file by file.", "",
             "**The rule it exists for:** when you change how one place reads, counts or shows some data, every "
             "other module listed next to that data below must get the same change (or be checked). One screen "
             "fixed and another still wrong is the bug he notices most.", ""]
    lines += ["## Shared data and who reads it", "",
              "Only data used by two or more modules. `db` helpers (`db.<name>`):", ""]
    for fn in sorted(db_users):
        if DB_PLUMBING.match(fn):
            continue
        users = sorted(u for u in db_users[fn] if u != "db" and u not in IGNORED_USERS)
        if len(users) >= 2:
            lines.append(f"- `db.{fn}` - {_list(users)}")
    lines += ["", "SQL tables (modules with SQL naming the table):", ""]
    for t in sorted(known_tables):
        users = sorted(u for u in table_users[t] if u not in IGNORED_USERS)
        if len(users) >= 2:
            lines.append(f"- `{t}` - {_list(users)}")
    shared_kv = [(k, sorted(u for u in v if u not in IGNORED_USERS)) for k, v in sorted(kv_users.items())]
    shared_kv = [(k, users) for k, users in shared_kv if len(users) >= 2]
    if shared_kv:
        lines += ["", "kv keys (prefix - modules):", ""]
        lines += [f"- `{k}` - {_list(users)}" for k, users in shared_kv]
    if jobs:
        lines += ["", "## Scheduled jobs (main.job_specs; local time)", ""]
        for jname, kind, when, what in jobs:
            lines.append(f"- `{jname}` - {kind} {when}" + (f" - {what}" if what else ""))
    if routes:
        lines += ["", "## Pages and API routes", ""]
        by_mod: dict[str, list[str]] = defaultdict(list)
        for r, m in routes:
            by_mod[m].append(r)
        for m in sorted(by_mod):
            lines.append(f"- `{m}`: " + ", ".join(f"`{r}`" for r in sorted(set(by_mod[m]))))
    lines += ["", "## Modules", ""]
    for name, summary, funcs in sorted(modules):
        shown = ", ".join(funcs[:MAX_FUNCS]) + (f", +{len(funcs) - MAX_FUNCS} more" if len(funcs) > MAX_FUNCS else "")
        lines.append(f"- `{name}` - {summary or '(no docstring)'}" + (f"  \n  {shown}" if shown else ""))
    return "\n".join(lines) + "\n"


def _list(users: list[str]) -> str:
    more = len(users) - MAX_READERS
    return ", ".join(users[:MAX_READERS]) + (f", +{more} more" if more > 0 else "")


_out_override: Path | None = None


def set_output(p: Path | None) -> None:
    """Tests: write the map here instead of the app's docs/ (None = default)."""
    global _out_override
    _out_override = p


def path(root: Path | None = None) -> Path:
    if root is None and _out_override is not None:
        return _out_override
    return (root or ROOT) / OUT


def write(root: Path | None = None) -> Path:
    """Build and write ``docs/CODEMAP.md`` atomically. Returns the path."""
    out = path(root)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".md.tmp")
    tmp.write_text(build(root), encoding="utf-8")
    os.replace(tmp, out)
    return out


def stale(root: Path | None = None) -> bool:
    """True when the map is missing or any source file changed after it was written."""
    root = root or ROOT
    out = path(root)
    if not out.exists():
        return True
    built = out.stat().st_mtime
    return any(p.stat().st_mtime > built for p in _sources(root))


def ensure_fresh(root: Path | None = None) -> Path:
    """Rebuild only when stale (cheap check: file mtimes)."""
    return write(root) if stale(root) else path(root)
