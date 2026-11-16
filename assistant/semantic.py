"""Memory search by meaning: local embeddings over the vault, fused with the FTS5 keyword index.

"That thing I said about my budget" should find a note about savings even when no word matches.
Notes are cut into chunks (by heading, then paragraph, ~500-800 chars), each chunk is embedded
with a small static model (model2vec ``minishlab/potion-base-8M``: CPU only, free, ~30 MB downloaded
once from Hugging Face on first use) and stored as a float32 blob in ``as_vault_vec`` (assistant
store v6) with per-note mtime + sha in ``as_vault_vec_notes``, so only changed notes are re-embedded.

* ``ASSISTANT_SEMANTIC_MEMORY=auto|on|off`` (default auto = on when ``model2vec`` is installed).
* :func:`search` never blocks on the model: if it is not loaded yet it starts a background load
  + full :func:`sync` and returns ``[]`` (keyword search still answers). Once loaded, a search
  syncs at most :data:`INLINE_MAX_NOTES` changed notes inline and leaves the rest to the background.
* The job ``assistant_semantic_sync`` (main.assistant_job_specs, every 30 min) keeps it current.
* :func:`vault.search` merges keyword and meaning hits with reciprocal-rank fusion; with this
  feature off or unavailable it returns exactly the keyword results.
* Tests inject a deterministic embedder with :func:`set_embedder` and run background work inline
  with :func:`set_spawner`; nothing here touches the network unless the real model is loaded.
"""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import math
import re
import threading
import time
from array import array
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Callable, Protocol, Sequence

import db
from assistant import store
from assistant.settings import get_assistant_settings

log = logging.getLogger(__name__)

MODEL_ID = "minishlab/potion-base-8M"
CHUNK_TARGET = 700          # aim for chunks of about this many characters
CHUNK_MAX = 800
MIN_SCORE = 0.30            # cosine floor for a meaning hit (keeps noise out of the prompt context)
INLINE_MAX_NOTES = 40       # notes a search may (re-)embed inline; the rest go to the background
RETRY_AFTER_SEC = 1800      # after a failed model load, wait this long before trying again
_ROOT_KEY = "assistant:vault_vec_root"


class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass
class SemanticHit:
    path: str
    heading: str
    snippet: str
    score: float

    @property
    def title(self) -> str:
        return PurePosixPath(self.path).stem


@dataclass
class SyncStats:
    embedded: list[str] = field(default_factory=list)
    unchanged: int = 0
    removed: int = 0
    pending: int = 0
    chunks: int = 0
    skipped: str = ""           # why nothing ran ("" = it ran)


# ---------------------------------------------------------------------------
# backend
# ---------------------------------------------------------------------------

class Model2VecEmbedder:
    """model2vec static embeddings (numpy; no torch). Loads from the local HF cache when present."""

    def __init__(self, model_id: str = MODEL_ID) -> None:
        from model2vec import StaticModel  # heavy/optional: only when actually used

        self._model = StaticModel.from_pretrained(model_id, force_download=False)
        self.name = f"model2vec:{model_id}"
        self.dim = int(self._model.dim)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return self._model.encode(list(texts), use_multiprocessing=False).tolist()


_lock = threading.Lock()
_sync_lock = threading.Lock()
_embedder: Embedder | None = None
_injected = False
_loading = False
_load_error = ""
_load_failed_at = 0.0
_cache: tuple[Any, list[tuple[str, str, str]], Any] | None = None   # (token, rows, matrix)
_loader: Callable[[], Embedder] = Model2VecEmbedder


def _default_spawn(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="semantic-sync", daemon=True).start()


_spawn: Callable[[Callable[[], None]], None] = _default_spawn


def set_embedder(embedder: Embedder | None) -> None:
    """Tests (or another backend): use ``embedder``; ``None`` = back to the real model2vec loader."""
    global _embedder, _injected, _load_error, _load_failed_at, _cache
    with _lock:
        _embedder, _injected = embedder, embedder is not None
        _load_error, _load_failed_at, _cache = "", 0.0, None


def set_spawner(fn: Callable[[Callable[[], None]], None] | None) -> None:
    """Tests: run background work inline (``lambda f: f()``) or drop it (``lambda f: None``)."""
    global _spawn
    _spawn = fn or _default_spawn


def reset() -> None:
    global _loading
    set_embedder(None)
    set_spawner(None)
    _loading = False


def _package_installed() -> bool:
    try:
        return importlib.util.find_spec("model2vec") is not None
    except (ImportError, ValueError):
        return False


def mode() -> str:
    return get_assistant_settings().semantic_memory


def unavailable_reason() -> str:
    """'' when meaning search can run, else why not (shown on status lines)."""
    m = mode()
    if m == "off":
        return "turned off (ASSISTANT_SEMANTIC_MEMORY=off)"
    if _injected:
        return ""
    if not _package_installed():
        return "the model2vec package is not installed (pip install -r requirements.txt)"
    if _load_error and _embedder is None:
        return f"model failed to load: {_load_error}"
    return ""


def enabled() -> bool:
    m = mode()
    return m != "off" and (_injected or _package_installed())


def _get(load: bool) -> Embedder | None:
    """The loaded embedder; with ``load`` load it now (blocking), else ``None`` until loaded."""
    global _embedder, _load_error, _load_failed_at
    if _embedder is not None or not load or not enabled():
        return _embedder
    if _load_error and time.monotonic() - _load_failed_at < RETRY_AFTER_SEC:
        return None
    with _lock:
        if _embedder is None:
            try:
                _embedder = _loader()
                _load_error = ""
                db.log_event("semantic", f"memory model loaded ({_embedder.name}, {_embedder.dim} dims)")
            except Exception as exc:  # noqa: BLE001 - missing download, offline, broken install
                first = not _load_error
                _load_error, _load_failed_at = f"{type(exc).__name__}: {exc}"[:200], time.monotonic()
                if first:
                    db.log_event("semantic", "memory model could not load; keyword search only",
                                 level="warning", data={"error": _load_error})
                return None
    return _embedder


def warm_async() -> bool:
    """Load the model (if needed) and run a full sync in the background. False if not started."""
    global _loading
    if not enabled() or _loading:
        return False
    if _embedder is None and _load_error and time.monotonic() - _load_failed_at < RETRY_AFTER_SEC:
        return False
    _loading = True

    def work() -> None:
        global _loading
        try:
            if _get(load=True) is not None:
                sync()
        except Exception:  # noqa: BLE001 - a background job must never crash the process
            log.exception("semantic background sync failed")
        finally:
            _loading = False

    _spawn(work)
    return True


# ---------------------------------------------------------------------------
# chunking + vectors
# ---------------------------------------------------------------------------

_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


def _split_long(text: str) -> list[str]:
    if len(text) <= CHUNK_MAX:
        return [text]
    out: list[str] = []
    cur = ""
    for piece in re.split(r"(?<=[.!?])\s+|\n", text):
        while len(piece) > CHUNK_MAX:
            if cur:
                out.append(cur)
                cur = ""
            out.append(piece[:CHUNK_MAX])
            piece = piece[CHUNK_MAX:]
        if cur and len(cur) + 1 + len(piece) > CHUNK_TARGET:
            out.append(cur)
            cur = piece
        else:
            cur = f"{cur} {piece}".strip()
    if cur:
        out.append(cur)
    return [p for p in out if p.strip()]


def chunk_note(body: str) -> list[tuple[str, str]]:
    """``[(heading, text)]``: split by heading, then pack paragraphs up to ~CHUNK_TARGET chars."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in body.splitlines():
        m = _HEADING.match(line)
        if m:
            sections.append((m.group(2).strip(), []))
        else:
            sections[-1][1].append(line)
    chunks: list[tuple[str, str]] = []
    for heading, lines in sections:
        paras = [p.strip() for p in re.split(r"\n\s*\n", "\n".join(lines)) if p.strip()]
        cur = ""
        for para in paras:
            for piece in _split_long(para):
                if cur and len(cur) + 2 + len(piece) > CHUNK_TARGET:
                    chunks.append((heading, cur))
                    cur = piece
                else:
                    cur = f"{cur}\n\n{piece}" if cur else piece
        if cur:
            chunks.append((heading, cur))
        elif heading and not paras:
            chunks.append((heading, heading))
    return chunks


def _normalise(vec: Sequence[float]) -> list[float]:
    n = math.sqrt(sum(float(x) * float(x) for x in vec))
    return [float(x) / n for x in vec] if n > 0 else [0.0 for _ in vec]


def _pack(vec: Sequence[float]) -> bytes:
    return array("f", _normalise(vec)).tobytes()


def _unpack(blob: bytes) -> array:
    a = array("f")
    a.frombytes(blob)
    return a


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------

def sync(*, load: bool = True, max_notes: int | None = None) -> SyncStats:
    """Embed new/changed notes, drop deleted ones. Cheap when nothing changed (mtime compare).

    ``load=False`` never loads the model (skips instead); ``max_notes`` bounds the embedding work
    (the remainder is reported as ``pending``). Concurrent callers skip rather than wait.
    """
    global _cache
    from assistant import vault  # late: vault imports this module lazily too

    stats = SyncStats()
    if not enabled():
        stats.skipped = unavailable_reason() or "off"
        return stats
    emb = _get(load)
    if emb is None:
        stats.skipped = unavailable_reason() or "model not loaded yet"
        return stats
    if not _sync_lock.acquire(blocking=False):
        stats.skipped = "busy"
        return stats
    try:
        root = vault.vault_root()
        if not root.is_dir():
            stats.skipped = "no vault"
            return stats
        root_key = str(root.resolve())
        if db.kv_get(_ROOT_KEY) != root_key:  # vault moved: start over
            store.delete_note_vectors()
            db.kv_set(_ROOT_KEY, root_key)
        known = store.vec_note_states()
        on_disk = set(vault.list_notes())
        for rel in sorted(on_disk):
            p = root / rel
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            prev = known.get(rel)
            if prev is not None and prev[2] == emb.name and prev[0] == mtime:
                stats.unchanged += 1
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if prev is not None and prev[2] == emb.name and prev[1] == sha:
                store.set_vec_note_mtime(rel, mtime)
                stats.unchanged += 1
                continue
            if max_notes is not None and len(stats.embedded) >= max_notes:
                stats.pending += 1
                continue
            meta, body = vault.parse_note_text(text)
            tags = meta.get("tags") or []
            if isinstance(tags, list) and tags:
                body = f"{body}\n\n{' '.join(map(str, tags))}"
            pieces = chunk_note(body)
            title = PurePosixPath(rel).stem
            vecs = emb.embed([f"{title}. {h}\n{t}" if h else f"{title}.\n{t}" for h, t in pieces]) if pieces else []
            store.replace_note_vectors(rel, mtime=mtime, sha=sha, model=emb.name,
                                       chunks=[(h, t, _pack(v)) for (h, t), v in zip(pieces, vecs)])
            stats.embedded.append(rel)
            stats.chunks += len(pieces)
        for rel in set(known) - on_disk:
            store.delete_note_vectors(rel)
            stats.removed += 1
    finally:
        _sync_lock.release()
    if stats.embedded or stats.removed:
        _cache = None
    return stats


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

def _matrix() -> tuple[list[tuple[str, str, str]], Any]:
    """Every chunk's (path, heading, text) and its vectors (numpy matrix if numpy, else arrays)."""
    global _cache
    token = store.vector_index_token()
    if _cache is not None and _cache[0] == token:
        return _cache[1], _cache[2]
    raw = store.list_note_vectors()
    rows = [(p, h, t) for p, h, t, _ in raw]
    try:
        import numpy as np

        mat: Any = (np.vstack([np.frombuffer(b, dtype=np.float32) for *_, b in raw])
                    if raw else np.zeros((0, 1), dtype=np.float32))
    except ImportError:
        mat = [_unpack(b) for *_, b in raw]
    _cache = (token, rows, mat)
    return rows, mat


def _scores(mat: Any, q: list[float]) -> list[float]:
    if isinstance(mat, list):
        return [sum(a * b for a, b in zip(v, q)) if len(v) == len(q) else 0.0 for v in mat]
    import numpy as np

    if mat.shape[0] == 0 or mat.shape[1] != len(q):
        return [0.0] * mat.shape[0]
    return (mat @ np.asarray(q, dtype=np.float32)).tolist()


def search(query: str, limit: int = 8, *, load: bool = False, min_score: float = MIN_SCORE) -> list[SemanticHit]:
    """Notes closest in meaning to ``query`` (best chunk per note). ``[]`` when off / not ready.

    Without ``load`` it never waits for the model: a cold start kicks off :func:`warm_async`.
    """
    query = (query or "").strip()
    if not query or not enabled():
        return []
    emb = _get(load)
    if emb is None:
        if not load:
            warm_async()
        return []
    st = sync(load=False, max_notes=None if load else INLINE_MAX_NOTES)
    if st.pending:
        warm_async()
    rows, mat = _matrix()
    if not rows:
        return []
    q = _normalise(emb.embed([query])[0])
    best: dict[str, SemanticHit] = {}
    for (path, heading, text), score in zip(rows, _scores(mat, q)):
        if score < min_score:
            continue
        cur = best.get(path)
        if cur is None or score > cur.score:
            snippet = " ".join(text.split())
            best[path] = SemanticHit(path, heading, snippet[:240] + ("…" if len(snippet) > 240 else ""), float(score))
    return sorted(best.values(), key=lambda h: -h.score)[:max(1, min(limit, 50))]


def fuse(keyword: Sequence[Any], meaning: Sequence[SemanticHit], limit: int, *, k: int = 60) -> list[tuple[str, float]]:
    """Reciprocal-rank fusion of two ranked hit lists -> ``[(path, score)]`` best first.

    Ties keep keyword order first, so a meaning hit only moves ahead by also ranking well.
    """
    score: dict[str, float] = {}
    order: dict[str, int] = {}
    for rank, hit in enumerate(keyword):
        score[hit.path] = score.get(hit.path, 0.0) + 1.0 / (k + rank + 1)
        order.setdefault(hit.path, rank)
    for rank, hit in enumerate(meaning):
        score[hit.path] = score.get(hit.path, 0.0) + 1.0 / (k + rank + 1)
        order.setdefault(hit.path, len(keyword) + rank)
    ranked = sorted(score, key=lambda p: (-score[p], order[p]))
    return [(p, score[p]) for p in ranked[:limit]]


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def reset_vectors() -> None:
    """Forget every stored vector (``main.py assistant reindex``); the next sync re-embeds all."""
    global _cache
    store.delete_note_vectors()
    _cache = None


def status() -> dict[str, Any]:
    try:
        notes, chunks = store.vector_counts()
    except Exception:  # noqa: BLE001 - status must never fail
        notes = chunks = 0
    reason = unavailable_reason()
    return {"mode": mode(), "enabled": enabled() and not reason, "reason": reason,
            "model": _embedder.name if _embedder is not None else f"model2vec:{MODEL_ID}",
            "loaded": _embedder is not None, "loading": _loading, "notes": notes, "chunks": chunks}


def status_line() -> str:
    """One line for ``main.py assistant status`` and the Settings page."""
    s = status()
    if not s["enabled"]:
        return f"Memory search: keywords only ({s['reason']})"
    state = "" if s["loaded"] else (", model loading" if s["loading"] else ", model loads on first search")
    return f"Memory search: keywords + meaning ({s['chunks']} chunks from {s['notes']} notes indexed{state})"


def scheduled_sync() -> str:
    """Job ``assistant_semantic_sync``: load if needed and bring the vectors up to date."""
    st = sync(load=True)
    if st.skipped:
        return f"skipped: {st.skipped}"
    return f"embedded {len(st.embedded)} notes ({st.chunks} chunks), removed {st.removed}, unchanged {st.unchanged}"
