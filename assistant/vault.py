"""The assistant's long-term memory: an Obsidian-compatible markdown vault.

The owner opens the same folder in Obsidian. Plain files, YAML frontmatter,
``[[wikilinks]]``, daily notes at ``Journal/YYYY/YYYY-MM-DD.md``. The vault lives
OUTSIDE git (default ``<data_dir>/vault``; ``data/`` is gitignored) - only the empty
``vault_template/`` is committed.

Frontmatter keys (in this order): ``type, created, updated, source, confidence
(stated|inferred), tags``; extra keys are preserved.

Safety (the LLM calls these as tools):

* every path is relative to the vault root; absolute paths, ``..``, hidden
  components (``.obsidian``...), NUL bytes and symlinks escaping the root are refused
  with :class:`VaultPathError`; only ``.md`` notes are read or written.
* human edits win: :func:`write_note` takes the ``sha`` of the version the caller
  read; if the file changed on disk since, the new text is appended under a
  "Merged" heading instead of overwriting (or :class:`NoteConflict` with
  ``on_conflict="raise"``). Appends re-read the file under a lock just before
  the atomic replace.

Search: an SQLite FTS5 index (``as_vault_fts`` in the assistant store) kept in sync
incrementally by mtime + sha (:func:`sync_index`, run before every search) with a full
:func:`reindex`.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import db
from assistant import store
from assistant.settings import get_assistant_settings

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "vault_template"
FRONTMATTER_ORDER = ("type", "created", "updated", "source", "confidence", "tags")
CONFIDENCE = ("stated", "inferred")
FACTS_HEADING = "Facts"
MAX_NOTE_BYTES = 512_000
MAX_FACT_CHARS = 500
DUPLICATE_THRESHOLD = 0.88

# remember(area=...) -> note. "person:<Name>" and "project:<Name>" file per-entity notes.
AREAS: dict[str, tuple[str, str]] = {
    "about": ("Profile/About.md", "profile"),
    "work": ("Profile/Work.md", "profile"),
    "projects": ("Profile/Projects.md", "profile"),
    "preferences": ("Profile/Preferences.md", "profile"),
    "timeline": ("Profile/Timeline.md", "profile"),
    "open_questions": ("Profile/Open Questions.md", "profile"),
    "finance": ("Finance/Finance.md", "finance"),
    "health": ("Health/Health.md", "health"),
    "meals": ("Health/Meals.md", "health"),
    "decisions": ("Decisions/Decisions.md", "decision"),
    "agent": ("Agent/Lessons/Lessons.md", "lesson"),
    "inbox": ("Inbox/Inbox.md", "inbox"),
}

_lock = threading.RLock()
_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
_FACT_LINE = re.compile(r"^- (?P<fact>.+?)(?: — _(?P<prov>[^_]*)_)?\s*$")


class VaultError(Exception):
    """Base class for vault errors."""


class VaultPathError(VaultError):
    """A path outside the vault, not a note, or otherwise unsafe."""


class NoteNotFound(VaultError):
    pass


class NoteConflict(VaultError):
    """The note changed on disk since the caller read it."""


@dataclass(slots=True)
class Note:
    path: str                      # vault-relative POSIX path, e.g. "Profile/About.md"
    meta: dict[str, Any]
    body: str
    sha: str                       # sha256 of the full file text as read
    mtime: float = 0.0

    @property
    def title(self) -> str:
        return PurePosixPath(self.path).stem


@dataclass(slots=True)
class SearchHit:
    path: str
    title: str
    snippet: str
    score: float


@dataclass(slots=True)
class RememberResult:
    path: str
    added: bool
    duplicate_of: str | None = None


@dataclass(slots=True)
class IndexStats:
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    paths: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------

# Notes only the owner writes (the heartbeat checklist): tool, sub-agent and self-edit writes are refused.
# Sterling proposes changes with request_approval; the approved handler writes with ``owner=True``.
PROTECTED_NOTES = frozenset({"Agent/Heartbeat.md"})


class VaultProtected(VaultError):
    """A write to an owner-only note (see :data:`PROTECTED_NOTES`)."""


def is_protected(rel_posix: str) -> bool:
    return rel_posix.lower() in {p.lower() for p in PROTECTED_NOTES}


def _guard(rel_posix: str, owner: bool) -> None:
    if not owner and is_protected(rel_posix):
        raise VaultProtected(f"{rel_posix} is the owner's own note; propose a change with request_approval instead")


def vault_root() -> Path:
    return get_assistant_settings().vault_dir


def safe_path(rel: str | os.PathLike[str], *, must_exist: bool = False) -> tuple[Path, str]:
    """Validate a vault-relative note path. Returns (absolute path, normalised relative POSIX path).

    ``.md`` is appended when there is no suffix. Raises :class:`VaultPathError`.
    """
    raw = os.fspath(rel)
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw or len(raw) > 300:
        raise VaultPathError("empty or invalid path")
    raw = raw.strip().replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw) or raw.startswith("~"):
        raise VaultPathError(f"absolute paths are not allowed: {raw!r}")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise VaultPathError(f"path escapes the vault: {raw!r}")
    if any(p.startswith(".") for p in parts):
        raise VaultPathError(f"hidden files are off limits: {raw!r}")
    rel_posix = "/".join(parts)
    suffix = PurePosixPath(rel_posix).suffix
    if suffix == "":
        rel_posix += ".md"
    elif suffix.lower() != ".md":
        raise VaultPathError(f"only .md notes are allowed: {raw!r}")
    root = vault_root().resolve()
    target = (root / rel_posix)
    resolved = target.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise VaultPathError(f"path resolves outside the vault: {raw!r}") from None
    # A symlink anywhere on the way is refused even if it points inside (keeps reasoning simple).
    probe = root
    for p in rel_posix.split("/"):
        probe = probe / p
        if probe.is_symlink():
            raise VaultPathError(f"symlinks are not followed: {raw!r}")
    if must_exist and not resolved.is_file():
        raise NoteNotFound(rel_posix)
    return resolved, rel_posix


def ensure_vault() -> Path:
    """Create the vault from ``vault_template/`` on first use (never overwrites existing files)."""
    root = vault_root()
    root.mkdir(parents=True, exist_ok=True)
    if TEMPLATE_DIR.is_dir():
        for src in sorted(TEMPLATE_DIR.rglob("*")):
            rel = src.relative_to(TEMPLATE_DIR)
            dst = root / rel
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
            elif src.name == ".gitkeep":
                dst.parent.mkdir(parents=True, exist_ok=True)
            elif not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                text = src.read_text(encoding="utf-8").replace("{{date}}", db.local_today().isoformat())
                _atomic_write(dst, text)
    return root


# ---------------------------------------------------------------------------
# frontmatter
# ---------------------------------------------------------------------------

def _norm_value(v: Any) -> Any:
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, list):
        return [_norm_value(x) for x in v]
    return v


def parse_note_text(text: str) -> tuple[dict[str, Any], str]:
    """Split ``text`` into (frontmatter dict, body). Broken YAML -> ({}, whole text)."""
    if text.startswith("﻿"):
        text = text[1:]
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return {}, text
    lines = text.splitlines(keepends=True)
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            import yaml

            try:
                meta = yaml.safe_load("".join(lines[1:i])) or {}
            except yaml.YAMLError:
                return {}, text
            if not isinstance(meta, dict):
                return {}, text
            body = "".join(lines[i + 1:])
            return {str(k): _norm_value(v) for k, v in meta.items()}, body.lstrip("\n")
    return {}, text


def _yaml_scalar(v: Any) -> str:
    import yaml

    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) or re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_ ./-]*", s) and s.strip() == s \
            and s.lower() not in ("true", "false", "null", "yes", "no", "on", "off", "~"):
        return s
    return yaml.safe_dump(s, default_flow_style=True, width=10_000).strip().removesuffix("\n...").strip()


def render_note(meta: dict[str, Any], body: str) -> str:
    keys = [k for k in FRONTMATTER_ORDER if k in meta] + [k for k in meta if k not in FRONTMATTER_ORDER]
    out = ["---"]
    for k in keys:
        v = meta[k]
        if isinstance(v, (list, tuple)):
            out.append(f"{k}: [{', '.join(_yaml_scalar(x) for x in v)}]")
        else:
            out.append(f"{k}: {_yaml_scalar(v)}".rstrip())
    out.append("---")
    return "\n".join(out) + "\n\n" + body.lstrip("\n").rstrip() + "\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    if len(text.encode("utf-8")) > MAX_NOTE_BYTES:
        raise VaultError(f"note too large ({len(text)} chars)")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".md", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def read_note(rel: str) -> Note:
    path, rel_posix = safe_path(rel, must_exist=True)
    text = path.read_text(encoding="utf-8", errors="replace")
    meta, body = parse_note_text(text)
    return Note(rel_posix, meta, body, _sha(text), path.stat().st_mtime)


def note_exists(rel: str) -> bool:
    try:
        path, _ = safe_path(rel)
    except VaultPathError:
        return False
    return path.is_file()


def _default_meta(note_type: str, source: str, confidence: str = "stated",
                  tags: list[str] | None = None) -> dict[str, Any]:
    today = db.local_today().isoformat()
    return {"type": note_type, "created": today, "updated": today, "source": source,
            "confidence": confidence, "tags": list(tags or [])}


def write_note(rel: str, body: str, *, meta: dict[str, Any] | None = None, base_sha: str | None = None,
               on_conflict: str = "append", source: str = "assistant", note_type: str = "note",
               owner: bool = False) -> Note:
    """Create or replace a note atomically, stamping ``updated`` (and ``created`` when new).

    ``base_sha`` is the sha of the version the caller read (``None`` = caller did not read
    it). If the note exists and its current sha differs from ``base_sha`` - or the caller
    never read an existing note - the human's version is kept and ``body`` is appended
    under a "Merged" heading (``on_conflict="append"``) or :class:`NoteConflict` is raised
    (``on_conflict="raise"``).
    """
    if on_conflict not in ("append", "raise"):
        raise ValueError("on_conflict must be 'append' or 'raise'")
    path, rel_posix = safe_path(rel)
    _guard(rel_posix, owner)
    with _lock:
        if path.is_file():
            current_text = path.read_text(encoding="utf-8", errors="replace")
            if base_sha is None or _sha(current_text) != base_sha:
                if on_conflict == "raise":
                    raise NoteConflict(f"{rel_posix} changed on disk since it was read")
                stamp = db.local_now().strftime("%Y-%m-%d %H:%M")
                return append_to_note(rel_posix, body, heading=f"Merged {stamp} ({source})", source=source,
                                      owner=owner)
            old_meta, _ = parse_note_text(current_text)
            merged = {**old_meta, **(meta or {})}
        else:
            merged = {**_default_meta(note_type, source), **(meta or {})}
        merged.setdefault("created", db.local_today().isoformat())
        merged["updated"] = db.local_today().isoformat()
        text = render_note(merged, body)
        _atomic_write(path, text)
        _index_one(rel_posix, text, path.stat().st_mtime)
        return Note(rel_posix, merged, parse_note_text(text)[1], _sha(text), path.stat().st_mtime)


def append_to_note(rel: str, text: str, *, heading: str | None = None, source: str = "assistant",
                   note_type: str = "note", tags: list[str] | None = None, owner: bool = False) -> Note:
    """Append ``text`` (at the end, or at the end of the ``## heading`` section, created if missing).

    Creates the note with frontmatter when absent. Always re-reads the file under the lock,
    so a human's concurrent edit is merged, never lost.
    """
    path, rel_posix = safe_path(rel)
    _guard(rel_posix, owner)
    text = text.rstrip()
    if not text:
        raise VaultError("nothing to append")
    with _lock:
        if path.is_file():
            meta, body = parse_note_text(path.read_text(encoding="utf-8", errors="replace"))
            if not meta:
                meta = _default_meta(note_type, source, tags=tags)
        else:
            meta, body = _default_meta(note_type, source, tags=tags), f"# {PurePosixPath(rel_posix).stem}\n"
        body = _insert_under_heading(body, text, heading)
        meta["updated"] = db.local_today().isoformat()
        out = render_note(meta, body)
        _atomic_write(path, out)
        _index_one(rel_posix, out, path.stat().st_mtime)
        return Note(rel_posix, meta, parse_note_text(out)[1], _sha(out), path.stat().st_mtime)


def _insert_under_heading(body: str, text: str, heading: str | None) -> str:
    body = body.rstrip("\n")
    if heading is None:
        return f"{body}\n{text}\n" if body else f"{text}\n"
    lines = body.split("\n")
    target = f"## {heading}".strip()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == target)
    except StopIteration:
        return f"{body}\n\n{target}\n{text}\n"
    end = next((j for j in range(start + 1, len(lines)) if re.match(r"^#{1,2} ", lines[j])), len(lines))
    while end > start + 1 and lines[end - 1].strip() == "":
        end -= 1
    return "\n".join(lines[:end] + text.split("\n") + lines[end:]) + "\n"


def delete_note(rel: str, *, owner: bool = False) -> bool:
    """Delete a note (used by forget/tidy). Returns False if it did not exist."""
    path, rel_posix = safe_path(rel)
    _guard(rel_posix, owner)
    with _lock:
        if not path.is_file():
            return False
        path.unlink()
        _unindex(rel_posix)
        return True


def list_notes(folder: str | None = None) -> list[str]:
    """Vault-relative paths of all notes (optionally under ``folder``), sorted. Hidden dirs skipped."""
    root = vault_root()
    if not root.is_dir():
        return []
    base = root
    if folder:
        if ".." in folder.replace("\\", "/").split("/") or folder.startswith(("/", "~")):
            raise VaultPathError(f"bad folder {folder!r}")
        base = root / folder.strip("/")
        if not base.resolve().is_relative_to(root.resolve()):
            raise VaultPathError(f"bad folder {folder!r}")
    out = []
    for p in base.rglob("*.md"):
        rel = p.relative_to(root)
        if any(part.startswith(".") for part in rel.parts) or p.is_symlink():
            continue
        out.append(rel.as_posix())
    return sorted(out)


def links_in(text: str) -> list[str]:
    return [m.group(1).strip() for m in _WIKILINK.finditer(text)]


def backlinks(rel_or_name: str) -> list[str]:
    """Notes that link to the given note via ``[[Name]]``, ``[[Folder/Name]]`` or ``[[Name|alias]]``."""
    name = PurePosixPath(rel_or_name.replace("\\", "/")).stem if rel_or_name.endswith(".md") else rel_or_name
    stem = PurePosixPath(name).name
    targets = {name.lower(), stem.lower(), name.removesuffix(".md").lower()}
    out = []
    for rel in list_notes():
        if PurePosixPath(rel).stem.lower() == stem.lower() and rel.lower().endswith(f"{name.lower()}.md"):
            continue  # the note itself
        try:
            text = (vault_root() / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for link in links_in(text):
            low = link.lower().removesuffix(".md")
            if low in targets or PurePosixPath(low).name == stem.lower():
                out.append(rel)
                break
    return out


def journal_path(day: date | None = None) -> str:
    d = day or db.local_today()
    return f"Journal/{d.year:04d}/{d.isoformat()}.md"


def append_journal(text: str, day: date | None = None, *, source: str = "assistant") -> Note:
    """Add a timestamped bullet to the daily note (created on first use)."""
    d = day or db.local_today()
    rel = journal_path(d)
    line = " ".join(text.split())
    stamp = db.local_now().strftime("%H:%M") if day is None or day == db.local_today() else ""
    entry = f"- {stamp} {line}".replace("-  ", "- ") if stamp else f"- {line}"
    if not note_exists(rel):
        path, rel_posix = safe_path(rel)
        meta = _default_meta("journal", source, tags=["journal"])
        meta["created"] = meta["updated"] = d.isoformat()
        with _lock:
            if not path.is_file():
                body = f"# {d.isoformat()}\n\nBack to [[Home]].\n\n## Log\n"
                out = render_note(meta, body)
                _atomic_write(path, out)
    return append_to_note(rel, entry, heading="Log", source=source, note_type="journal")


# ---------------------------------------------------------------------------
# remember / forget
# ---------------------------------------------------------------------------

def _normalise_fact(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s$%.:/-]", " ", text.lower()).split()).strip(" .")


def _similar(a: str, b: str) -> float:
    na, nb = _normalise_fact(a), _normalise_fact(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = set(na.split()), set(nb.split())
    jacc = len(ta & tb) / len(ta | tb)
    return max(jacc, difflib.SequenceMatcher(None, na, nb).ratio())


def note_for_area(area: str) -> tuple[str, str]:
    """Map a memory ``area`` to (note path, note type). Raises ValueError for unknown areas."""
    a = area.strip()
    low = a.lower().replace(" ", "_")
    if low in AREAS:
        return AREAS[low]
    for prefix, folder, ntype in (("person:", "People", "person"), ("project:", "Projects", "project")):
        if low.startswith(prefix):
            name = re.sub(r"[^\w .'-]", "", a[len(prefix):]).strip()
            if not name or name.startswith("."):
                raise ValueError(f"bad {prefix[:-1]} name in area {area!r}")
            return f"{folder}/{name}.md", ntype
    raise ValueError(f"unknown memory area {area!r}; use one of {sorted(AREAS)} or person:<Name> / project:<Name>")


def facts_in(body: str) -> list[str]:
    """Bullet facts in a note body (provenance suffix stripped)."""
    out = []
    for ln in body.splitlines():
        m = _FACT_LINE.match(ln.strip())
        if m:
            out.append(m.group("fact").strip())
    return out


def remember(fact: str, *, area: str, source: str, confidence: str = "stated") -> RememberResult:
    """The single front door for saving a memory.

    Files ``fact`` as a bullet under ``## Facts`` in the note for ``area`` with provenance
    (``— _stated, sms, 2026-11-13_``). Near-identical facts already in that note are not
    added again (``added=False, duplicate_of=<existing>``).
    """
    if confidence not in CONFIDENCE:
        raise ValueError(f"confidence must be one of {CONFIDENCE}")
    clean = " ".join(fact.split()).lstrip("#>-* ").strip()
    if not clean:
        raise ValueError("empty fact")
    if len(clean) > MAX_FACT_CHARS:
        clean = clean[:MAX_FACT_CHARS - 1].rstrip() + "…"
    rel, ntype = note_for_area(area)
    src = re.sub(r"[_\n]", " ", source).strip() or "unknown"
    with _lock:
        if note_exists(rel):
            for existing in facts_in(read_note(rel).body):
                if _similar(existing, clean) >= DUPLICATE_THRESHOLD:
                    return RememberResult(rel, False, existing)
        line = f"- {clean} — _{confidence}, {src}, {db.local_today().isoformat()}_"
        append_to_note(rel, line, heading=FACTS_HEADING, source=src, note_type=ntype)
    return RememberResult(rel, True)


def forget(fact: str, *, area: str | None = None, threshold: float = DUPLICATE_THRESHOLD) -> list[str]:
    """Remove fact bullets matching ``fact`` (in ``area``'s note, or in every note). Returns removed lines.

    Nothing is kept elsewhere: forgetting means deleting.
    """
    targets = [note_for_area(area)[0]] if area else [r for r in list_notes() if not is_protected(r)]
    removed: list[str] = []
    with _lock:
        for rel in targets:
            if not note_exists(rel):
                continue
            note = read_note(rel)
            keep, gone = [], []
            for ln in note.body.split("\n"):
                m = _FACT_LINE.match(ln.strip())
                if m and _similar(m.group("fact"), fact) >= threshold:
                    gone.append(ln.strip())
                else:
                    keep.append(ln)
            if gone:
                write_note(rel, "\n".join(keep), base_sha=note.sha, on_conflict="raise")
                removed.extend(gone)
    return removed


# ---------------------------------------------------------------------------
# search index (SQLite FTS5 in the assistant store)
# ---------------------------------------------------------------------------

_ROOT_KEY = "assistant:vault_index_root"


def _index_one(rel: str, text: str, mtime: float, *, conn: Any = None) -> None:
    meta, body = parse_note_text(text)
    title = PurePosixPath(rel).stem
    tags = meta.get("tags") or []
    indexed = f"{body}\n{' '.join(map(str, tags)) if isinstance(tags, list) else tags}"
    with store.transaction(conn) as c:
        c.execute("DELETE FROM as_vault_fts WHERE path = ?", (rel,))
        c.execute("INSERT INTO as_vault_fts (path, title, body) VALUES (?,?,?)", (rel, title, indexed))
        c.execute("INSERT INTO as_vault_index (path, mtime, sha, title, indexed_at) VALUES (?,?,?,?,?) "
                  "ON CONFLICT (path) DO UPDATE SET mtime = excluded.mtime, sha = excluded.sha, "
                  "title = excluded.title, indexed_at = excluded.indexed_at",
                  (rel, mtime, _sha(text), title, db.now_iso()))


def _unindex(rel: str, *, conn: Any = None) -> None:
    with store.transaction(conn) as c:
        c.execute("DELETE FROM as_vault_fts WHERE path = ?", (rel,))
        c.execute("DELETE FROM as_vault_index WHERE path = ?", (rel,))


def sync_index() -> IndexStats:
    """Bring the index up to date with the files on disk (mtime first, sha to confirm)."""
    stats = IndexStats()
    root = vault_root()
    root_key = str(root.resolve())
    with store.transaction() as c:
        if db.kv_get(_ROOT_KEY, conn=c) != root_key:  # vault moved: start over
            c.execute("DELETE FROM as_vault_fts")
            c.execute("DELETE FROM as_vault_index")
            db.kv_set(_ROOT_KEY, root_key, conn=c)
        known = {r["path"]: (r["mtime"], r["sha"]) for r in c.execute("SELECT path, mtime, sha FROM as_vault_index")}
        on_disk = set(list_notes())
        for rel in sorted(on_disk):
            p = root / rel
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            prev = known.get(rel)
            if prev is not None and prev[0] == mtime:
                stats.unchanged += 1
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            if prev is not None and prev[1] == _sha(text):
                c.execute("UPDATE as_vault_index SET mtime = ? WHERE path = ?", (mtime, rel))
                stats.unchanged += 1
                continue
            _index_one(rel, text, mtime, conn=c)
            stats.paths.append(rel)
            if prev is None:
                stats.added += 1
            else:
                stats.updated += 1
        for rel in set(known) - on_disk:
            _unindex(rel, conn=c)
            stats.removed += 1
    return stats


def reindex() -> IndexStats:
    """Drop and rebuild the whole index."""
    with store.transaction() as c:
        c.execute("DELETE FROM as_vault_fts")
        c.execute("DELETE FROM as_vault_index")
    return sync_index()


def _fts_query(query: str) -> str:
    words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 1][:16]
    return " OR ".join(f'"{w}"' for w in words)


def search(query: str, limit: int = 8) -> list[SearchHit]:
    """Full-text search over the vault (BM25 rank, title hits weighted x3)."""
    q = _fts_query(query)
    if not q or not vault_root().is_dir():
        return []
    sync_index()
    with store.transaction() as c:
        rows = c.execute(
            "SELECT path, title, snippet(as_vault_fts, 2, '', '', ' … ', 24) AS snip, "
            "bm25(as_vault_fts, 0.0, 3.0, 1.0) AS rank FROM as_vault_fts WHERE as_vault_fts MATCH ? "
            "ORDER BY rank LIMIT ?", (q, max(1, min(limit, 50)))).fetchall()
    return [SearchHit(r["path"], r["title"], " ".join(r["snip"].split()), -float(r["rank"])) for r in rows]


# ---------------------------------------------------------------------------
# prompt context
# ---------------------------------------------------------------------------

CORE_NOTES = ("Profile/About.md", "Profile/Preferences.md")


def _strip_meta(rel: str, limit: int) -> str:
    try:
        body = read_note(rel).body.strip()
    except VaultError:
        return ""
    return body if len(body) <= limit else body[:limit].rsplit("\n", 1)[0] + "\n…"


def context_for(topic_or_message: str, budget_chars: int = 4000, *, journal_days: int = 3) -> str:
    """Most relevant memory for a prompt: core profile, search hits, recent journal.

    Returned as ``<note path="...">`` blocks. It is untrusted data; the runtime wraps it
    accordingly. Never longer than ``budget_chars``.
    """
    if not vault_root().is_dir():
        return ""
    parts: list[str] = []
    used = 0

    def add(rel: str, text: str) -> bool:
        nonlocal used
        if not text:
            return True
        block = f'<note path="{rel}">\n{text}\n</note>'
        if used + len(block) > budget_chars:
            room = budget_chars - used - len(rel) - 40
            if room < 200:
                return False
            block = f'<note path="{rel}">\n{text[:room]}…\n</note>'
        parts.append(block)
        used += len(block)
        return used < budget_chars

    seen: set[str] = set()
    share = max(400, budget_chars // 4)
    for rel in CORE_NOTES:
        seen.add(rel)
        if not add(rel, _strip_meta(rel, share)):
            return "\n".join(parts)
    for hit in search(topic_or_message, limit=6):
        if hit.path in seen or hit.path.startswith("Journal/"):
            continue
        seen.add(hit.path)
        if not add(hit.path, hit.snippet):
            return "\n".join(parts)
    today = db.local_today()
    for n in range(journal_days):
        d = date.fromordinal(today.toordinal() - n)
        rel = journal_path(d)
        if note_exists(rel) and not add(rel, _strip_meta(rel, share)):
            break
    return "\n".join(parts)


def copy_template_to(dest: Path) -> None:
    """Copy the empty template to ``dest`` (CLI helper / tests)."""
    shutil.copytree(TEMPLATE_DIR, dest, dirs_exist_ok=True)
