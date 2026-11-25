"""Trusted skill installer: a catalog of skills from approved GitHub repos, reviewed and approved by the owner.

Builds on :mod:`assistant.skill_vetting` (quarantine, static checks, reputation, the SAFE/CAUTION/UNSAFE
decision table, ``skills/third_party.json``, the self-edit commit); nothing here duplicates it.

1. **Catalog** (:func:`catalog`, :func:`search`). Approved sources = :func:`trusted_repos`
   (``anthropics/skills`` + ``SKILLS_TRUSTED_REPOS``). For each repo: the HEAD commit (GitHub API),
   the recursive tree at that commit (every folder holding a ``SKILL.md``; each folder's tree sha
   tells whether *that skill* changed) and each ``SKILL.md`` read from ``raw.githubusercontent.com``
   at the pinned commit. Cached in kv for 24 h (:data:`CACHE_TTL`). Every entry is pinned to the
   commit sha; an installed skill whose folder tree sha differs shows "update available". Updates
   are never applied automatically: they go through the same review.
2. **Review** (:func:`request_install`): ``skill_vetting.vet`` fetches the pinned commit into
   quarantine (never executed) and scans it; :func:`detect_requirements` lists the Python packages
   it declares (requirements files, the SKILL.md "Dependencies" section, ``pip install`` lines and
   the imports of its scripts) and :func:`pin_requirements` pins each to an exact PyPI version and
   checks for a Windows wheel. Cecil then reads every file and writes the plain-English summary,
   what it can touch and its red flags (:func:`submit_summary`, tool ``skill_request_install``).
3. **Card** on Settings -> Skills (``/dashboard/skills``, :mod:`assistant.toolkit_web`). Tier from
   the decision table (:func:`card`): trusted + SAFE + no red flag at all = one-tap install;
   CAUTION = the reasons are shown and installing needs an explicit tick; UNSAFE = cannot be
   installed. **Only the owner approves**, on that page (dashboard auth + CSRF): no tool approves
   a review, these reviews never become phase-1 decisions, so no standing approval can cover them
   (``approvals`` refuses skill installs as standing rules as well).
4. **Install** (:func:`approve`): quarantine hashes re-checked, approved pip packages installed into
   this venv at the pinned versions, then ``skill_vetting.install`` copies the folder to
   ``skills/<name>/`` and records source, commit, tree sha, file hashes and the packages in the
   manifest through the self-edit flow (tests, commit, UNDO).
5. **Restricted by default** (:func:`is_restricted`): a third-party skill stays restricted until
   the owner promotes it (:func:`promote`, explicit confirm; bound to the skill's file hashes, so an
   update or an edit restricts it again). Restricted means:

   * it is not mirrored into Claude Code's native skills folder, so it is only reachable through
     ``skill_read``; reading it marks the turn (and the MCP server process) restricted, and
     ``tools.dispatch`` then refuses outbound and secret-adjacent tools
     (:data:`assistant.tools.RESTRICTED_SKILL_BLOCKED`: message_owner, request_approval, open_url,
     web search, config, ...) for the rest of that turn;
   * Claude Code deny rules forbid editing its folder and the manifest (:func:`protected_paths`);
     the existing deny rules already cover config.env, tokens and keys; its own folder is allowed
     for reading.

   **Honest limits.** Deny rules bind Claude Code's own tools, not a script it runs: a skill's
   Python script launched through Bash runs as the owner's user and the OS does not stop it from
   opening a file. What stands in the way is the static scan (any mention of secrets, browser data,
   encoded commands or auto-run hooks is UNSAFE), Cecil's reading, the trusted-source allowlist and
   the owner's approval. Under the full-power backend Cecil could also read a restricted skill's
   files with Read instead of ``skill_read`` and miss the marker; the skill is still not in his
   native skills list.
6. **Uninstall** (:func:`uninstall`) archives the folder to ``skills/.archive/<date>-<name>/`` (like
   ``staff_delete``) and moves its manifest entry under ``archived``; a commit, so UNDO restores it.

Tools: ``skills_catalog_search``, ``skill_request_install``, ``skills_installed``.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import json
import logging
import re
import shutil
import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import quote, urlparse

import db
from assistant import skill_vetting as sv

log = logging.getLogger(__name__)

MODULE = "assistant.skill_catalog"
DEFAULT_REPOS = ("anthropics/skills", "obra/superpowers")
CACHE_TTL = timedelta(hours=24)
KV_CACHE = "skill_catalog:cache:{repo}"
KV_REVIEWS = "skill_catalog:reviews"
KV_PROMOTED = "skill_catalog:promoted"
MAX_SKILLS_PER_REPO = 100
MAX_REVIEWS = 60
TIERS = ("one_tap", "caution", "unsafe")
OPEN_STATUSES = ("awaiting_summary", "pending", "installing")
_SHA = re.compile(r"[0-9a-f]{40}")
_PKG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_VER = re.compile(r"^[0-9][0-9A-Za-z.+!-]{0,40}$")
_REQ_LINE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*((?:[<>=!~]=?|===)\s*[^;#\s]+(?:\s*,\s*"
                       r"(?:[<>=!~]=?|===)\s*[^;#\s,]+)*)?")

# import name -> PyPI name, for third-party modules skills commonly use. Unknown imports are ignored
# (they are usually the skill's own helper modules).
IMPORT_TO_PYPI = {
    "docx": "python-docx", "pptx": "python-pptx", "openpyxl": "openpyxl", "pypdf": "pypdf", "PyPDF2": "PyPDF2",
    "pdfplumber": "pdfplumber", "pdf2image": "pdf2image", "pytesseract": "pytesseract", "PIL": "Pillow",
    "defusedxml": "defusedxml", "lxml": "lxml", "pandas": "pandas", "numpy": "numpy", "imageio": "imageio",
    "imageio_ffmpeg": "imageio-ffmpeg", "markitdown": "markitdown", "reportlab": "reportlab",
    "playwright": "playwright", "anthropic": "anthropic", "mcp": "mcp", "requests": "requests", "httpx": "httpx",
    "yaml": "PyYAML", "bs4": "beautifulsoup4", "matplotlib": "matplotlib", "pypdfium2": "pypdfium2",
    "fitz": "PyMuPDF", "xlsxwriter": "XlsxWriter", "jinja2": "Jinja2", "markdown": "Markdown",
}


class CatalogError(Exception):
    """Catalog / review / install problem (message is safe to show)."""


_lock = threading.RLock()


# ---------------------------------------------------------------------------
# network (GitHub goes through skill_vetting.http_get; tests replace both)
# ---------------------------------------------------------------------------

def _pypi_get(url: str) -> bytes:
    import httpx

    host = (urlparse(url).hostname or "").lower()
    if host != "pypi.org":
        raise CatalogError(f"refusing to fetch from {host}")
    with httpx.Client(timeout=20, follow_redirects=True, headers={"Accept": "application/json",
                                                                   "User-Agent": "clipagent-skill-catalog"}) as c:
        r = c.get(url)
        if r.status_code == 404:
            raise CatalogError("not on PyPI")
        r.raise_for_status()
        if len(r.content) > 20 * 1024 * 1024:
            raise CatalogError("PyPI answer too large")
        return r.content


pypi_get: Callable[[str], bytes] = _pypi_get


def _gh_json(url: str) -> Any:
    try:
        return json.loads(sv.http_get(url))
    except ValueError as exc:
        raise CatalogError(f"GitHub sent something unexpected ({exc})") from None


def _raw(repo: str, sha: str, path: str) -> str:
    url = f"https://raw.githubusercontent.com/{repo}/{sha}/{quote(path)}"
    return sv.http_get(url, accept="text/plain").decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# catalog
# ---------------------------------------------------------------------------

def trusted_repos() -> list[str]:
    """The approved sources: anthropics/skills + SKILLS_TRUSTED_REPOS (owner/repo, lower-case, deduplicated)."""
    from assistant.toolkit_settings import get_toolkit_settings

    out: list[str] = []
    for r in (*DEFAULT_REPOS, *get_toolkit_settings().skills_trusted_repos):
        r = r.strip().strip("/").lower().removesuffix(".git")
        if sv._REPO_RE.match(r) and r not in out:
            out.append(r)
    return out


def _skill_folders(tree: list[dict[str, Any]]) -> list[str]:
    paths = sorted({PurePosixPath(t["path"]).parent.as_posix() for t in tree
                    if t.get("type") == "blob" and PurePosixPath(str(t.get("path", ""))).name == "SKILL.md"},
                   key=lambda p: (p.count("/"), p))
    keep: list[str] = []
    for p in paths:
        p = "" if p == "." else p
        parts = p.split("/") if p else []
        if any(x.startswith(".") for x in parts) or (parts[:1] == ["template"]) or ".." in parts:
            continue
        if any(p == k or p.startswith(k + "/") or k == "" for k in keep):
            continue  # a SKILL.md inside another skill (an example) is part of that skill
        keep.append(p)
    return keep[:MAX_SKILLS_PER_REPO]


def _collections(repo: str, sha: str) -> dict[str, str]:
    """skill folder -> plugin/collection name from the repo's marketplace file (optional)."""
    try:
        data = json.loads(_raw(repo, sha, ".claude-plugin/marketplace.json"))
    except Exception:
        return {}
    out: dict[str, str] = {}
    for plugin in data.get("plugins") or [] if isinstance(data, dict) else []:
        if not isinstance(plugin, dict):
            continue
        for s in plugin.get("skills") or []:
            if isinstance(s, str):
                out.setdefault(s.strip().removeprefix("./").strip("/"), str(plugin.get("name") or "")[:60])
    return out


def fetch_catalog(repo: str) -> dict[str, Any]:
    """Read one repo's skills from GitHub, pinned to its current HEAD commit (network)."""
    from assistant import registry

    commit = _gh_json(f"https://api.github.com/repos/{repo}/commits/HEAD")
    sha = commit.get("sha") if isinstance(commit, dict) else None
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise CatalogError(f"could not pin {repo} to a commit")
    tree = _gh_json(f"https://api.github.com/repos/{repo}/git/trees/{sha}?recursive=1")
    items = tree.get("tree") if isinstance(tree, dict) else None
    if not isinstance(items, list):
        raise CatalogError(f"could not list {repo}")
    tree_sha = {str(t.get("path")): str(t.get("sha")) for t in items if t.get("type") == "tree"}
    groups = _collections(repo, sha)
    entries = []
    for folder in _skill_folders(items):
        prefix = folder + "/" if folder else ""
        files = [t for t in items if t.get("type") == "blob" and str(t.get("path", "")).startswith(prefix)]
        try:
            meta, _ = registry.parse_frontmatter(_raw(repo, sha, prefix + "SKILL.md"))
        except (ValueError, sv.VettingError) as exc:
            log.info("skipping %s/%s: %s", repo, folder, exc)
            continue
        leaf = PurePosixPath(folder).name or repo.split("/")[1]
        name = sv._norm_name(str(meta.get("name") or leaf))
        exts = [PurePosixPath(str(t["path"])).suffix.lower() for t in files]
        entries.append({
            "repo": repo, "path": folder, "name": name, "title": str(meta.get("name") or leaf)[:80],
            "description": " ".join(str(meta.get("description") or "").split())[:1024],
            "license": " ".join(str(meta.get("license") or "").split())[:120],
            "commit": sha, "tree_sha": tree_sha.get(folder) if folder else str(tree.get("sha") or ""),
            "files": len(files), "scripts": sum(1 for e in exts if e in sv.SCRIPT_EXTS),
            "requirements_file": any(PurePosixPath(str(t["path"])).name.lower().startswith("requirements")
                                     and str(t["path"]).endswith(".txt") for t in files),
            "collection": groups.get(folder, ""),
            "url": f"https://github.com/{repo}/tree/{sha}/{folder}".rstrip("/"),
        })
    return {"repo": repo, "commit": sha, "fetched_at": db.now_iso(), "entries": entries,
            "truncated": bool(tree.get("truncated"))}


def _cached(repo: str, *, refresh: bool = False) -> dict[str, Any]:
    key = KV_CACHE.format(repo=repo)
    cache = db.kv_get(key)
    fresh = False
    if isinstance(cache, dict) and cache.get("fetched_at"):
        try:
            fresh = db.utcnow() - db.parse_iso(cache["fetched_at"]) < CACHE_TTL
        except ValueError:
            fresh = False
    if fresh and not refresh:
        return {**cache, "stale": False, "error": None}
    try:
        data = fetch_catalog(repo)
    except (CatalogError, sv.VettingError, OSError) as exc:
        if isinstance(cache, dict) and cache.get("entries") is not None:
            return {**cache, "stale": True, "error": str(exc)[:300]}
        return {"repo": repo, "commit": None, "fetched_at": None, "entries": [], "stale": True,
                "error": str(exc)[:300]}
    except Exception as exc:  # httpx errors etc.: show, never crash the page
        return {**(cache if isinstance(cache, dict) else {"repo": repo, "entries": []}), "stale": True,
                "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    db.kv_set(key, data)
    db.log_event(MODULE, f"skill catalog refreshed: {repo} ({len(data['entries'])} skills)",
                 data={"commit": data["commit"]})
    return {**data, "stale": False, "error": None}


def catalog(*, refresh: bool = False) -> dict[str, Any]:
    """Every trusted repo's skills (cached 24 h), each annotated with install/update/review state."""
    repos, entries = [], []
    index = _installed_index()
    open_reviews = {(r["repo"], r["path"]): r for r in list_reviews() if r["status"] in OPEN_STATUSES}
    for repo in trusted_repos():
        c = _cached(repo, refresh=refresh)
        repos.append({k: c.get(k) for k in ("repo", "commit", "fetched_at", "stale", "error", "truncated")})
        for e in c.get("entries") or []:
            entries.append(_annotate(dict(e), index, open_reviews))
    return {"repos": repos, "entries": entries}


def _annotate(e: dict[str, Any], index: dict[tuple[str, str], tuple[str, dict[str, Any]]],
              open_reviews: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    hit = index.get((e["repo"], e["path"]))
    e["installed_as"] = hit[0] if hit else None
    if hit:
        inst = hit[1]
        e["installed_commit"] = inst.get("commit")
        if inst.get("tree_sha") and e.get("tree_sha"):
            e["update_available"] = inst["tree_sha"] != e["tree_sha"]
        else:
            e["update_available"] = bool(inst.get("commit")) and inst.get("commit") != e["commit"]
        e["restricted"] = is_restricted(hit[0])
    else:
        e["installed_commit"], e["update_available"], e["restricted"] = None, False, None
    rv = open_reviews.get((e["repo"], e["path"]))
    e["review_id"] = rv["id"] if rv else None
    return e


def search(query: str = "", *, refresh: bool = False, limit: int = 50) -> list[dict[str, Any]]:
    words = [w for w in re.split(r"\s+", (query or "").lower()) if w]
    out = []
    for e in catalog(refresh=refresh)["entries"]:
        hay = f"{e['name']} {e['title']} {e['description']} {e.get('collection', '')}".lower()
        if all(w in hay for w in words):
            out.append(e)
    out.sort(key=lambda e: (0 if words and words[0] in e["name"] else 1, e["name"]))
    return out[:limit]


def find_entry(name: str, repo: str | None = None) -> dict[str, Any]:
    n = sv._norm_name(name or "")
    for refresh in (False, True):
        for e in catalog(refresh=refresh)["entries"]:
            if (e["name"] == n or e["path"].rsplit("/", 1)[-1].lower() == (name or "").lower()) \
                    and (repo is None or e["repo"] == repo.lower()):
                return e
    raise CatalogError(f"no skill called {name!r} in the trusted sources ({', '.join(trusted_repos())})")


# ---------------------------------------------------------------------------
# installed skills, restriction, promotion
# ---------------------------------------------------------------------------

def _source_parts(entry: dict[str, Any]) -> tuple[str, str] | None:
    if entry.get("repo"):
        return str(entry["repo"]).lower(), str(entry.get("path") or "")
    src = str(entry.get("source") or "")
    if src.startswith("github:"):
        try:
            s = sv.parse_source(src)
        except sv.VettingError:
            return None
        return (s.repo or "").lower(), s.path
    return None


def _installed_index() -> dict[tuple[str, str], tuple[str, dict[str, Any]]]:
    out = {}
    for name, e in sv.read_manifest().get("skills", {}).items():
        parts = _source_parts(e)
        if parts:
            out[parts] = (name, e)
    return out


def _digest(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files or {}, sort_keys=True).encode()).hexdigest()


def modified_files(name: str, entry: dict[str, Any] | None = None) -> list[str]:
    """Files of an installed third-party skill whose bytes differ from the manifest (or are missing/new)."""
    entry = entry if entry is not None else sv.read_manifest().get("skills", {}).get(name) or {}
    want: dict[str, str] = dict(entry.get("files") or {})
    root = sv.skills_dir() / name
    if not root.is_dir():
        return sorted(want) or ["(folder missing)"]
    out = []
    seen = set()
    for p in root.rglob("*"):
        if not p.is_file() or "__pycache__" in p.parts:
            continue
        rel = p.relative_to(root).as_posix()
        seen.add(rel)
        if rel not in want or hashlib.sha256(p.read_bytes()).hexdigest() != want[rel]:
            out.append(rel)
    out += [r for r in want if r not in seen]
    return sorted(out)


def _promotions() -> dict[str, Any]:
    v = db.kv_get(KV_PROMOTED, {}) or {}
    return v if isinstance(v, dict) else {}


def is_third_party(name: str) -> bool:
    return name in sv.read_manifest().get("skills", {})


def is_restricted(name: str) -> bool:
    """True for a third-party skill not (validly) promoted. Built-in skills are never restricted."""
    entry = sv.read_manifest().get("skills", {}).get(name)
    if entry is None:
        return False
    p = _promotions().get(name)
    if not isinstance(p, dict) or p.get("digest") != _digest(entry.get("files") or {}):
        return True
    return bool(modified_files(name, entry))


def restricted_names() -> list[str]:
    return sorted(n for n in sv.read_manifest().get("skills", {}) if is_restricted(n))


def protected_paths() -> list[Path]:
    """For Claude Code deny rules: the manifest and every restricted skill's folder (never edited by the agent)."""
    return [sv.manifest_path()] + [sv.skills_dir() / n for n in restricted_names()]


def installed() -> list[dict[str, Any]]:
    out = []
    promos = _promotions()
    cat_by_key = {}
    for repo in trusted_repos():
        cache = db.kv_get(KV_CACHE.format(repo=repo))
        for e in (cache or {}).get("entries") or [] if isinstance(cache, dict) else []:
            cat_by_key[(e["repo"], e["path"])] = e
    for name, e in sorted(sv.read_manifest().get("skills", {}).items()):
        parts = _source_parts(e)
        latest = cat_by_key.get(parts) if parts else None
        update = False
        if latest:
            update = (latest.get("tree_sha") != e.get("tree_sha")) if e.get("tree_sha") and latest.get("tree_sha") \
                else latest.get("commit") != e.get("commit")
        mod = modified_files(name, e)
        out.append({"name": name, "source": e.get("source"), "repo": parts[0] if parts else None,
                    "path": parts[1] if parts else None, "commit": e.get("commit"),
                    "installed_at": e.get("installed_at"), "approved_by": e.get("approved_by"),
                    "trusted_source": e.get("trusted_source"), "restricted": is_restricted(name),
                    "promoted": (promos.get(name) or {}) if not is_restricted(name) else None,
                    "modified": mod, "python_requirements": e.get("python_requirements") or [],
                    "update_available": update, "latest_commit": (latest or {}).get("commit"),
                    "rating": (e.get("verdict") or {}).get("rating"), "static_rating": e.get("static_rating")})
    return out


def all_skills() -> list[dict[str, Any]]:
    """Every skill in ``skills/`` (built-in, self-made and third-party), name + one-line description + kind."""
    from assistant import registry

    manifest = sv.read_manifest().get("skills", {})
    out = []
    for name, s in sorted(registry.load_skills(sv.skills_dir(), strict=False).items()):
        third = name in manifest
        out.append({"name": name, "description": s.description,
                    "kind": "third-party" if third else "built-in",
                    "restricted": is_restricted(name) if third else False,
                    "files": 1 + len(s.resources)})
    return out


def promote(name: str, *, via: str, confirm: bool) -> dict[str, Any]:
    """Lift a third-party skill's restrictions (owner only, explicit confirm). Bound to its current files."""
    if not confirm:
        raise CatalogError("promoting lifts the restrictions: tick the confirmation first")
    entry = sv.read_manifest().get("skills", {}).get(name)
    if entry is None:
        raise CatalogError(f"{name} is not an installed third-party skill")
    mod = modified_files(name, entry)
    if mod:
        raise CatalogError(f"{name} was changed since it was reviewed ({', '.join(mod[:5])}); reinstall it first")
    with _lock:
        p = _promotions()
        p[name] = {"at": db.now_iso(), "via": via, "digest": _digest(entry.get("files") or {}),
                   "commit": entry.get("commit")}
        db.kv_set(KV_PROMOTED, p)
    db.log_event(MODULE, f"skill promoted (restrictions lifted): {name}", level="warning", data={"via": via})
    return {"promoted": name}


def restrict(name: str, *, via: str) -> dict[str, Any]:
    with _lock:
        p = _promotions()
        had = p.pop(name, None) is not None
        db.kv_set(KV_PROMOTED, p)
    if had:
        db.log_event(MODULE, f"skill restricted again: {name}", data={"via": via})
    return {"restricted": name}


def uninstall(name: str, *, via: str) -> dict[str, Any]:
    """Archive skills/<name>/ to skills/.archive/<date>-<name>/ and drop it from the manifest (self-edit commit)."""
    m = sv.read_manifest()
    if name not in m.get("skills", {}):
        raise CatalogError(f"{name} is not an installed third-party skill (built-in skills are not removed here)")
    root = sv.skills_dir()
    arch = root / ".archive" / f"{db.local_today().isoformat()}-{name}"
    k = 2
    while arch.exists():
        arch = root / ".archive" / f"{db.local_today().isoformat()}-{name}-{k}"
        k += 1

    def change() -> None:
        src = root / name
        arch.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.move(str(src), str(arch))
        entry = m["skills"].pop(name)
        m.setdefault("archived", {})[arch.name] = {**entry, "uninstalled_at": db.now_iso(), "uninstalled_via": via}
        sv._write_manifest(m)

    res = sv.committer(f"uninstall third-party skill {name} (archived)", change)
    restrict(name, via=via)
    db.log_event(MODULE, f"third-party skill uninstalled: {name}", level="warning",
                 data={"archived_as": arch.name, "via": via})
    return {"uninstalled": name, "archived_as": str(arch), "commit_result": res}


# ---------------------------------------------------------------------------
# Python requirements
# ---------------------------------------------------------------------------

def _canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _add(out: dict[str, dict[str, Any]], name: str, spec: str, origin: str) -> None:
    if not _PKG.match(name or ""):
        return
    key = _canon(name)
    cur = out.setdefault(key, {"name": name, "spec": "", "origins": []})
    if spec and not cur["spec"]:
        cur["spec"] = re.sub(r"\s+", "", spec)
    if origin not in cur["origins"]:
        cur["origins"].append(origin)


def _deps_section(text: str) -> list[str]:
    lines = text.splitlines()
    out: list[str] = []
    on = False
    for ln in lines:
        if re.match(r"^\s{0,3}#{1,4}\s", ln):
            on = bool(re.match(r"^\s{0,3}#{1,4}\s*(dependencies|prerequisites)\b", ln, re.I))
            continue
        if on and ln.strip():
            out.append(ln)
    return out


def detect_requirements(folder: Path) -> dict[str, list[Any]]:
    """What a skill says it needs: {"python": [{name, spec, origins}], "system": [str]}. Reads text only."""
    py: dict[str, dict[str, Any]] = {}
    system: list[str] = []
    files = sorted(p for p in folder.rglob("*") if p.is_file())
    for p in files:
        rel = p.relative_to(folder).as_posix()
        low = p.name.lower()
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if low.startswith("requirements") and low.endswith(".txt"):
            for ln in text.splitlines():
                ln = ln.split("#", 1)[0].strip()
                if not ln or ln.startswith("-"):
                    continue
                m = _REQ_LINE.match(ln)
                if m:
                    _add(py, m.group(1), m.group(3) or "", rel)
        elif p.suffix.lower() == ".md":
            for seg in re.split(r"\s+·\s+|\n", "\n".join(_deps_section(text))):
                seg = seg.strip(" -*\t")
                if not seg:
                    continue
                head, _, tail = seg.partition("(")
                names = re.findall(r"`([^`]+)`", head)
                if "pip" in tail.lower()[:40] and names:
                    for n in names:
                        m = _REQ_LINE.match(n.strip())
                        if m:
                            _add(py, m.group(1), m.group(3) or "", f"{rel} (Dependencies)")
                else:
                    item = re.sub(r"`", "", seg)[:100]
                    if item and item not in system and len(system) < 20:
                        system.append(item)
            for m in re.finditer(r"pip3?\s+install\s+([^\n`|&;]+)", text):
                for tok in m.group(1).split():
                    if tok.startswith("-") or "/" in tok or tok.endswith(".txt"):
                        if tok in ("-r", "--requirement"):
                            break
                        continue
                    mm = _REQ_LINE.match(tok.strip("\"'"))
                    if mm and len(mm.group(1)) > 1:
                        _add(py, mm.group(1), mm.group(3) or "", f"{rel} (pip install)")
        elif p.suffix.lower() == ".py":
            mods: set[str] = set()
            try:
                for node in ast.walk(ast.parse(text)):   # parsing only; nothing is executed
                    if isinstance(node, ast.Import):
                        mods |= {a.name.split(".")[0] for a in node.names}
                    elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                        mods.add(node.module.split(".")[0])
            except (SyntaxError, ValueError):
                mods = set(re.findall(r"^\s*(?:from|import)\s+([A-Za-z_][A-Za-z0-9_]*)", text, re.M))
            for mod in sorted(mods):
                if mod in IMPORT_TO_PYPI:
                    _add(py, IMPORT_TO_PYPI[mod], "", f"{rel} (import {mod})")
    return {"python": sorted(py.values(), key=lambda r: _canon(r["name"])), "system": system}


def _installed_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _windows_support(files: list[dict[str, Any]]) -> str:
    names = [str(f.get("filename") or "") for f in files if not f.get("yanked")]
    if any(n.endswith(".whl") and ("win_amd64" in n or "win32" in n) for n in names):
        return "wheel"
    if any(n.endswith("-none-any.whl") for n in names):
        return "pure"
    if any(n.endswith((".tar.gz", ".zip")) for n in names):
        return "sdist_only"
    return "unknown"


def pin_requirements(reqs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exact versions for each requirement: what is already in this venv, else the newest matching PyPI release."""
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.version import InvalidVersion, Version

    out = []
    for r in reqs[:30]:
        row = {"name": r["name"], "spec": r.get("spec") or "", "origins": r.get("origins") or [], "version": None,
               "installed_version": _installed_version(r["name"]), "action": "unavailable", "windows": "unknown",
               "error": None}
        try:
            spec = SpecifierSet(row["spec"]) if row["spec"] else SpecifierSet()
        except InvalidSpecifier:
            row["error"] = f"unreadable version rule {row['spec']!r}"
            out.append(row)
            continue
        inst = row["installed_version"]
        if inst and (not row["spec"] or spec.contains(inst, prereleases=True)):
            row.update(version=inst, action="already", windows="installed")
            out.append(row)
            continue
        try:
            data = json.loads(pypi_get(f"https://pypi.org/pypi/{quote(r['name'])}/json"))
            releases = data.get("releases") or {}
            latest = str((data.get("info") or {}).get("version") or "")
            cands = []
            for v, files in releases.items():
                try:
                    pv = Version(v)
                except InvalidVersion:
                    continue
                if pv.is_prerelease or (files and all(f.get("yanked") for f in files)) or not files:
                    continue
                if spec.contains(pv):
                    cands.append(pv)
            if not cands and latest and (not row["spec"] or spec.contains(latest)):
                cands = [Version(latest)]
            if not cands:
                row["error"] = "no release on PyPI matches " + (row["spec"] or "")
            else:
                ver = str(max(cands))
                files = releases.get(ver) or (data.get("urls") if ver == latest else []) or []
                if not _VER.match(ver):
                    row["error"] = "unexpected version string"
                else:
                    row.update(version=ver, action="install", windows=_windows_support(files))
        except (CatalogError, ValueError, OSError) as exc:
            row["error"] = str(exc)[:200]
        except Exception as exc:  # network errors: shown on the card, installing it is then refused
            row["error"] = f"{type(exc).__name__}: {str(exc)[:150]}"
        out.append(row)
    return out


def _pip_run(argv: list[str]) -> tuple[int, str]:
    p = subprocess.run(argv, capture_output=True, text=True, timeout=900)
    return p.returncode, (p.stdout + p.stderr)[-3000:]


pip_runner: Callable[[list[str]], tuple[int, str]] = _pip_run


def pip_install(pins: list[dict[str, Any]]) -> list[str]:
    """pip install the approved pins (exact versions) into this venv. Returns the 'name==version' list."""
    specs = []
    for p in pins:
        if p.get("action") != "install":
            continue
        if not (_PKG.match(str(p.get("name") or "")) and _VER.match(str(p.get("version") or ""))):
            raise CatalogError(f"refusing an odd package pin: {p.get('name')!r} {p.get('version')!r}")
        specs.append(f"{p['name']}=={p['version']}")
    if not specs:
        return []
    argv = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--prefer-binary",
            *specs]
    code, output = pip_runner(argv)
    if code != 0:
        raise CatalogError(f"pip could not install {', '.join(specs)}: {output.strip()[-400:]}")
    db.log_event(MODULE, f"python packages installed for a skill: {', '.join(specs)}", level="warning")
    return specs


# ---------------------------------------------------------------------------
# reviews (cards)
# ---------------------------------------------------------------------------

def _reviews() -> dict[str, dict[str, Any]]:
    v = db.kv_get(KV_REVIEWS, {}) or {}
    return v if isinstance(v, dict) else {}


def _save_review(r: dict[str, Any]) -> None:
    with _lock:
        rows = _reviews()
        rows[r["id"]] = r
        if len(rows) > MAX_REVIEWS:
            for old in sorted(rows.values(), key=lambda x: x.get("created_at") or "")[:len(rows) - MAX_REVIEWS]:
                if old["status"] not in OPEN_STATUSES:
                    rows.pop(old["id"], None)
        db.kv_set(KV_REVIEWS, rows)


def get_review(review_id: str) -> dict[str, Any]:
    r = _reviews().get(str(review_id or ""))
    if r is None:
        raise CatalogError(f"no skill review {review_id!r}")
    return r


def list_reviews(status: str | None = None) -> list[dict[str, Any]]:
    rows = [r for r in _reviews().values() if status is None or r["status"] == status]
    return sorted(rows, key=lambda r: r.get("created_at") or "", reverse=True)


def _touches(findings: list[sv.Finding], scripts: int) -> list[str]:
    rules = {f.rule for f in findings}
    out = []
    if scripts:
        out.append(f"ships {scripts} script(s) the agent may run")
    if "network" in rules:
        out.append("network: its files mention web requests")
    if "shell_exec" in rules:
        out.append("runs other programs (subprocess / shell)")
    if "writes_outside" in rules:
        out.append("mentions paths outside its own folder")
    if rules & {"secrets", "exfil_endpoint"}:
        out.append("mentions secrets or data-collection endpoints")
    if not out:
        out.append("text instructions only")
    return out


def review_from_report(rep: sv.VetReport, *, requested_by: str, entry: dict[str, Any] | None = None) -> dict[str, Any]:
    """Turn a quarantined vetting report into a review card (status awaiting_summary)."""
    folder = Path(rep.quarantine) / "files"
    reqs = detect_requirements(folder)
    pins = pin_requirements(reqs["python"]) if reqs["python"] else []
    try:
        src = sv.parse_source(rep.source) if rep.source.startswith("github:") else None
    except sv.VettingError:
        src = None
    repo = (entry or {}).get("repo") or ((src.repo or "").lower() if src else None)
    path = (entry or {}).get("path") if entry else (src.path if src else "")
    scripts = sum(1 for f in rep.files if PurePosixPath(f["rel"]).suffix.lower() in sv.SCRIPT_EXTS)
    name = sv._norm_name(rep.name)
    manifest = sv.read_manifest().get("skills", {})
    r = {"id": rep.id, "name": name, "description": rep.description, "repo": repo, "path": path or "",
         "commit": rep.commit, "tree_sha": (entry or {}).get("tree_sha"), "source": rep.source,
         "trusted": bool(rep.reputation.trusted), "reputation_flags": list(rep.reputation.flags),
         "static_rating": rep.static_rating, "files": len(rep.files), "scripts": scripts,
         "findings": [f"{f.rule} in {f.file}" + (f":{f.line}" if f.line else "") + f": {f.why}"
                      for f in rep.findings][:40],
         "touches": _touches(rep.findings, scripts), "requirements": {"python": pins, "system": reqs["system"]},
         "verdict": None, "status": "awaiting_summary", "created_at": db.now_iso(), "requested_by": requested_by,
         "update_of": name if name in manifest else None, "resolved_at": None, "resolved_via": None,
         "result": None, "error": None, "license": (entry or {}).get("license") or ""}
    if rep.static_rating == "UNSAFE":
        r["status"] = "refused"
    _save_review(r)
    db.log_event(MODULE, f"skill review opened: {name} ({rep.static_rating})", data={"review_id": rep.id,
                                                                                    "by": requested_by})
    return card(r)


def request_install(name: str, repo: str | None = None, *, requested_by: str) -> dict[str, Any]:
    """Quarantine + scan the catalog entry at its pinned commit and open a review card. Never installs."""
    e = find_entry(name, repo)
    for r in list_reviews():
        if r["status"] in OPEN_STATUSES and r["repo"] == e["repo"] and r["path"] == e["path"] \
                and r["commit"] == e["commit"]:
            return card(r)
    if e.get("installed_as") and not e.get("update_available"):
        raise CatalogError(f"{e['name']} is already installed at the latest version")
    rep = sv.vet(f"github:{e['repo']}/{e['path']}@{e['commit']}" if e["path"] else f"github:{e['repo']}@{e['commit']}")
    if rep.commit != e["commit"]:
        raise CatalogError("GitHub returned a different commit than the catalog pinned; refresh and try again")
    return review_from_report(rep, requested_by=requested_by, entry=e)


def submit_summary(review_id: str, rating: str, summary: str, red_flags: list[str] | None = None, *,
                   by: str) -> dict[str, Any]:
    """Cecil's plain-English verdict on a review. Makes the card ready for the owner; never installs."""
    r = get_review(review_id)
    if r["status"] not in ("awaiting_summary", "pending"):
        raise CatalogError(f"review {review_id} is {r['status']}")
    rating = (rating or "").upper().strip()
    if rating not in sv.RATINGS:
        raise CatalogError(f"rating must be one of {', '.join(sv.RATINGS)}")
    summary = " ".join((summary or "").split())[:1500]
    if not summary:
        raise CatalogError("write a plain-English summary of what it does and can touch")
    r["verdict"] = {"rating": rating, "summary": summary, "by": by, "at": db.now_iso(),
                    "red_flags": [" ".join(f.split())[:200] for f in (red_flags or []) if f and f.strip()][:20]}
    r["status"] = "refused" if card(r)["tier"] == "unsafe" else "pending"
    _save_review(r)
    try:
        rep = sv.load_report(review_id)
        rep.verdict = {**r["verdict"], "decision": card(r)["decision"]}
        sv._save(rep)
    except sv.VettingError:
        pass
    return card(r)


def card(r: dict[str, Any]) -> dict[str, Any]:
    """The review plus its tier (one_tap / caution / unsafe), decision and the reasons to show."""
    v = r.get("verdict") or {}
    flags = list(v.get("red_flags") or []) + list(r.get("findings") or []) + list(r.get("reputation_flags") or [])
    reasons = list(flags)
    for p in (r.get("requirements") or {}).get("python") or []:
        if p.get("action") == "unavailable":
            reasons.append(f"Python package {p['name']}: {p.get('error') or 'could not be pinned'}")
        elif p.get("action") == "install" and p.get("windows") == "sdist_only":
            reasons.append(f"Python package {p['name']} {p['version']} has no Windows wheel (may need a compiler)")
        elif p.get("action") == "install":
            reasons.append(f"installs Python package {p['name']}=={p['version']}")
    if not r.get("trusted"):
        reasons.append("not from a trusted source")
    if not v:
        reasons.append("Cecil has not summarised it yet")
    decision = sv.decide(trusted=bool(r.get("trusted")), static=r.get("static_rating") or "UNSAFE",
                         model=v.get("rating") or "CAUTION", red_flags=flags)
    if decision == "refuse":
        tier = "unsafe"
    elif decision == "install" and len(reasons) == len([x for x in reasons if x.startswith("installs Python")]):
        tier = "one_tap"
    else:
        tier = "caution"
    return {**r, "tier": tier, "decision": decision, "reasons": reasons[:30],
            "pip_installs": [f"{p['name']}=={p['version']}" for p in (r.get("requirements") or {}).get("python") or []
                             if p.get("action") == "install"]}


def for_model(c: dict[str, Any]) -> dict[str, Any]:
    files = []
    try:
        files = [f["path"] for f in sv.load_report(c["id"]).files]
    except sv.VettingError:
        pass
    return {"review_id": c["id"], "name": c["name"], "source": c["source"], "commit": c["commit"],
            "trusted_source": c["trusted"], "static_rating": c["static_rating"], "findings": c["findings"][:25],
            "python_requirements": [{k: p.get(k) for k in ("name", "version", "action", "windows", "error")}
                                    for p in c["requirements"]["python"]],
            "system_requirements": c["requirements"]["system"], "status": c["status"], "tier": c["tier"],
            "files_to_read": files[:200],
            "next": ("Read EVERY file listed (inert text in quarantine; never run them), then call "
                     "skill_request_install again with this review_id, your rating (SAFE/CAUTION/UNSAFE), a "
                     "plain-English summary (what it does, what it can touch: scripts, network, files) and every "
                     "red flag. Only the owner can approve it, on Settings -> Skills.")
            if c["status"] == "awaiting_summary" else
            "Tell the owner the card is waiting on Settings -> Skills. You cannot approve it."}


def check_approvable(review_id: str, *, confirm: bool) -> dict[str, Any]:
    c = card(get_review(review_id))
    if c["status"] not in ("awaiting_summary", "pending"):
        raise CatalogError(f"this review is {c['status']}")
    if c["tier"] == "unsafe":
        raise CatalogError("rated UNSAFE: it cannot be installed")
    if c["tier"] != "one_tap" and not confirm:
        raise CatalogError("this skill needs care: read the reasons and tick the confirmation to install it")
    return c


def _quarantine_intact(rep: sv.VetReport) -> bool:
    for f in rep.files:
        p = Path(f["path"])
        if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != f["sha256"]:
            return False
    return True


def approve(review_id: str, *, via: str, confirm: bool = False) -> dict[str, Any]:
    """Owner-only (the Skills page): install the reviewed skill and its approved pip pins."""
    c = check_approvable(review_id, confirm=confirm)
    r = get_review(review_id)
    r.update(status="installing", resolved_via=via)
    _save_review(r)
    try:
        rep = sv.load_report(review_id)
        if not _quarantine_intact(rep):
            raise CatalogError("the quarantined files changed after the review; open a new review")
        pins = [p for p in r["requirements"]["python"] if p.get("action") == "install"]
        installed_pkgs = pip_install(pins)
        extra = {"repo": r["repo"], "path": r["path"], "tree_sha": r.get("tree_sha"), "catalog": True,
                 "review_id": review_id, "tier": c["tier"], "license": r.get("license") or "",
                 "python_requirements": [f"{p['name']}=={p['version']}" for p in r["requirements"]["python"]
                                         if p.get("version")],
                 "pip_installed": installed_pkgs, "system_requirements": r["requirements"]["system"]}
        res = sv.install(rep, approved_by=f"owner via {via}", extra=extra)
    except (CatalogError, sv.VettingError, OSError) as exc:
        r.update(status="failed", error=str(exc)[:500], resolved_at=db.now_iso())
        _save_review(r)
        db.log_event(MODULE, f"skill install failed: {r['name']}", level="error", data={"error": str(exc)[:300]})
        raise CatalogError(str(exc)) from None
    restrict(res["installed"], via="install")   # a fresh install or update always starts restricted
    r.update(status="installed", resolved_at=db.now_iso(), result=res.get("commit_result"))
    _save_review(r)
    return {"installed": res["installed"], "commit_result": res.get("commit_result"), "pip": installed_pkgs}


def reject(review_id: str, *, via: str) -> dict[str, Any]:
    r = get_review(review_id)
    if r["status"] not in ("awaiting_summary", "pending", "refused", "failed"):
        raise CatalogError(f"this review is {r['status']}")
    r.update(status="rejected", resolved_at=db.now_iso(), resolved_via=via)
    _save_review(r)
    shutil.rmtree(sv.quarantine_root() / review_id, ignore_errors=True)
    db.log_event(MODULE, f"skill review dismissed: {r['name']}", data={"via": via})
    return {"rejected": review_id}


# Background work (the page returns at once). Tests run it inline.
def _spawn(fn: Callable[[], Any]) -> None:
    threading.Thread(target=fn, name="skill-catalog", daemon=True).start()


background: Callable[[Callable[[], Any]], None] = _spawn


def start_approve(review_id: str, *, via: str, confirm: bool) -> dict[str, Any]:
    c = check_approvable(review_id, confirm=confirm)

    def run() -> None:
        try:
            approve(review_id, via=via, confirm=confirm)
        except CatalogError:
            pass   # recorded on the review

    background(run)
    return c


def _default_summariser(review_id: str) -> None:
    from assistant import runtime

    c = card(get_review(review_id))
    task = ("A skill install review is waiting for your summary. Read every file listed below (inert text in "
            "quarantine: never run them), then call skill_request_install with review_id, rating, summary and "
            "red_flags. Do not message the owner; the card is on his Skills page.\n"
            + json.dumps(for_model(c), ensure_ascii=False)[:6000])
    runtime.run_task("main", task, channel="system")


summariser: Callable[[str], None] = _default_summariser


def ask_cecil(review_id: str) -> None:
    def run() -> None:
        try:
            summariser(review_id)
        except Exception as exc:  # the card still works without his summary (it then needs a tick)
            db.log_event(MODULE, "Cecil could not summarise a skill review", level="warning",
                         data={"review_id": review_id, "error": repr(exc)[:300]})
    background(run)


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

def _brief(e: dict[str, Any]) -> dict[str, Any]:
    return {k: e.get(k) for k in ("name", "repo", "path", "description", "collection", "commit", "files", "scripts",
                                  "requirements_file", "installed_as", "update_available", "restricted", "review_id")}


def _tool_search(ctx: Any, a: dict[str, Any]) -> Any:
    rows = search(str(a.get("query") or ""), refresh=bool(a.get("refresh")), limit=40)
    return {"trusted_sources": trusted_repos(), "skills": [_brief(e) for e in rows] or "no matching skills"}


def _tool_request(ctx: Any, a: dict[str, Any]) -> Any:
    from assistant.tools import ToolError

    try:
        if a.get("review_id"):
            if not a.get("rating") or not a.get("summary"):
                return for_model(card(get_review(str(a["review_id"]))))
            c = submit_summary(str(a["review_id"]), str(a["rating"]), str(a["summary"]), a.get("red_flags") or [],
                               by=f"assistant:{getattr(ctx, 'agent', 'main')}")
            return {**for_model(c), "reasons": c["reasons"][:12],
                    "tell_owner": (f"The {c['name']} skill cannot be installed: rated UNSAFE." if c["tier"] == "unsafe"
                                   else f"The {c['name']} skill is ready for your OK on Settings, Skills"
                                        + (" (one tap)." if c["tier"] == "one_tap" else "; it needs a careful look."))}
        if not a.get("name"):
            raise ToolError("give the skill name (from skills_catalog_search), or a review_id with your verdict")
        c = request_install(str(a["name"]), a.get("repo"), requested_by=f"assistant:{getattr(ctx, 'agent', 'main')}")
        return for_model(c)
    except (CatalogError, sv.VettingError) as exc:
        raise ToolError(str(exc)) from None


def _tool_installed(ctx: Any, a: dict[str, Any]) -> Any:
    rows = installed()
    return [{k: r[k] for k in ("name", "repo", "path", "commit", "installed_at", "restricted", "update_available",
                               "modified", "python_requirements")} for r in rows] or "no third-party skills installed"


def register_tools(register: Callable[..., None]) -> None:
    from assistant.tools import Tool, _obj

    register(Tool("skills_catalog_search",
                  "Browse/search skills from the owner's trusted sources (anthropics/skills and SKILLS_TRUSTED_REPOS), "
                  "each pinned to a commit, with installed / update-available / review state. Cached 24 h; "
                  "refresh=true re-reads GitHub.",
                  _obj({"query": {"type": "string"}, "refresh": {"type": "boolean"}}), _tool_search, owner=MODULE))
    register(Tool("skill_request_install",
                  "Open an install (or update) review for a catalog skill: name (from skills_catalog_search) -> it is "
                  "fetched into quarantine at its pinned commit and scanned, and you get the files to read. Then call "
                  "again with review_id + rating (SAFE/CAUTION/UNSAFE) + a plain-English summary (what it does, what "
                  "it can touch) + red_flags. That creates the card on Settings -> Skills. You cannot approve it: "
                  "only the owner can, on that page.",
                  _obj({"name": {"type": "string"}, "repo": {"type": "string"}, "review_id": {"type": "string"},
                        "rating": {"type": "string", "enum": list(sv.RATINGS)}, "summary": {"type": "string"},
                        "red_flags": {"type": "array", "items": {"type": "string"}}}),
                  _tool_request, owner=MODULE, side_effects=True))
    register(Tool("skills_installed",
                  "Installed third-party skills: source, pinned commit, restricted or promoted, update available, "
                  "files changed since review, Python packages.", _obj({}), _tool_installed, owner=MODULE))
