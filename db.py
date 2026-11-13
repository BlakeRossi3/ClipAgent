"""SQLite persistence: schema, migrations, time helpers and typed table helpers.

Conventions (see CLAUDE.md):

* Every timestamp column holds UTC ISO-8601 text: ``YYYY-MM-DDTHH:MM:SSZ``
  (see :func:`to_iso`). Such strings sort chronologically. "Day" columns
  (``stats.day``, ``channel_stats.day``) hold a local ``YYYY-MM-DD`` date in the
  configured timezone.
* JSON columns are stored as text and decoded into Python objects by the helpers.
* Each helper accepts an optional ``conn``; without one it opens a short-lived
  connection and commits on success. Pass a connection (e.g. from
  :func:`transaction`) to group several writes atomically.
* :func:`connect` initialises / migrates the schema on first use per path, so
  callers never see "no such table".
* Tests may monkeypatch :func:`utcnow` to freeze time; all helpers go through it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, ClassVar, TypeVar
from zoneinfo import ZoneInfo

from config import get_settings

# ---------------------------------------------------------------------------
# Status vocabularies & state machines
# ---------------------------------------------------------------------------

SOURCE_STATUSES = ("candidate", "fetched", "transcribed", "picked", "done", "rejected", "failed")
SOURCE_TRANSITIONS: dict[str, frozenset[str]] = {
    "candidate": frozenset({"fetched", "rejected", "failed"}),
    "fetched": frozenset({"transcribed", "rejected", "failed"}),
    # transcribed -> done when pick finds nothing worth clipping
    "transcribed": frozenset({"picked", "done", "rejected", "failed"}),
    "picked": frozenset({"done", "failed"}),
    "done": frozenset(),
    "rejected": frozenset({"candidate"}),  # manual re-approval
    "failed": frozenset({"candidate", "fetched", "transcribed"}),  # retry a stage
}

CLIP_STATUSES = ("picked", "rendered", "scheduled", "uploaded", "failed", "skipped")
CLIP_TRANSITIONS: dict[str, frozenset[str]] = {
    "picked": frozenset({"rendered", "failed", "skipped"}),
    # scheduled = rendered and assigned a publish slot, not yet sent to YouTube
    "rendered": frozenset({"scheduled", "uploaded", "failed", "skipped"}),
    "scheduled": frozenset({"uploaded", "rendered", "failed", "skipped"}),
    "uploaded": frozenset(),
    "failed": frozenset({"picked", "rendered"}),
    "skipped": frozenset({"picked"}),
}

UPLOAD_STATUSES = ("queued", "uploaded", "failed", "removed")
DECISION_STATUSES = ("pending", "approved", "rejected", "expired")
DECISION_KINDS = ("new_niche", "new_source_type", "budget_increase", "strategy_change", "other")
RESOLVED_VIA = ("call", "sms", "dashboard", "cli", "timeout")
NOTIFICATION_CHANNELS = ("call", "sms", "log")
NOTIFICATION_STATUSES = ("queued", "sent", "failed", "dry_run", "skipped")
EVENT_LEVELS = ("debug", "info", "warning", "error")

UPLOAD_QUOTA_UNITS = 1600  # YouTube Data API v3 videos.insert cost


class InvalidTransition(ValueError):
    """Raised when a status change is not allowed by the state machine."""


class NotFound(LookupError):
    """Raised when a row referenced by id does not exist."""


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def utcnow() -> datetime:
    """Current time, timezone-aware UTC. Monkeypatch this in tests to freeze time."""
    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    """Format a datetime as canonical UTC text (``2026-11-13T14:05:00Z``).

    Naive datetimes are assumed to be UTC.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return to_iso(utcnow())


def parse_iso(text: str) -> datetime:
    """Parse ISO-8601 text into an aware UTC datetime (naive input = UTC)."""
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def local_tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def local_now() -> datetime:
    return utcnow().astimezone(local_tz())


def local_today() -> date:
    """Today's date in the configured timezone (drives the daily budget and upload cap)."""
    return local_now().date()


def local_day_bounds(day: date | None = None) -> tuple[str, str]:
    """UTC ISO bounds ``[start, end)`` of a local calendar day."""
    day = day or local_today()
    tz = local_tz()
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return to_iso(start), to_iso(end)


def local_date_of(iso: str) -> date:
    """Local calendar date of a stored UTC timestamp."""
    return parse_iso(iso).astimezone(local_tz()).date()


# ---------------------------------------------------------------------------
# Schema & migrations
# ---------------------------------------------------------------------------

_SCHEMA_V1 = """
CREATE TABLE sources (
    id               INTEGER PRIMARY KEY,
    url              TEXT NOT NULL UNIQUE,
    source_type      TEXT NOT NULL,              -- nasa | congress | cc_by_youtube | ...
    platform         TEXT,                       -- youtube | nasa_images | house_gov | ...
    external_id      TEXT,                       -- e.g. YouTube video id
    channel          TEXT,                       -- channel / publisher display name
    channel_id       TEXT,
    title            TEXT,
    description      TEXT,
    duration_sec     REAL,
    published_at     TEXT,
    licence          TEXT,                       -- us_gov_public_domain | cc_by | ...
    licence_evidence TEXT,                       -- JSON: raw metadata that justified the decision
    topic            TEXT,                       -- strategy topic query that found it
    status           TEXT NOT NULL DEFAULT 'candidate'
                     CHECK (status IN ('candidate','fetched','transcribed','picked','done','rejected','failed')),
    status_reason    TEXT,
    video_path       TEXT,
    audio_path       TEXT,
    transcript_path  TEXT,
    metadata         TEXT,                       -- JSON: anything else scout/fetch want to keep
    discovered_at    TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX idx_sources_status ON sources(status);
CREATE INDEX idx_sources_external ON sources(platform, external_id);

CREATE TABLE clips (
    id                 INTEGER PRIMARY KEY,
    source_id          INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    start_sec          REAL NOT NULL,
    end_sec            REAL NOT NULL,
    haiku_score        REAL,
    sonnet_rationale   TEXT,
    title              TEXT NOT NULL DEFAULT '',
    description        TEXT NOT NULL DEFAULT '',
    hashtags           TEXT NOT NULL DEFAULT '[]', -- JSON list of "#tag" strings
    hook_text          TEXT NOT NULL DEFAULT '',
    credit_text        TEXT NOT NULL DEFAULT '',
    licence            TEXT NOT NULL,             -- copied from source for audit
    source_url         TEXT NOT NULL,             -- copied from source for audit
    strategy_version   INTEGER,
    experiment_id      TEXT,
    experiment_variant TEXT,                      -- control | variant | NULL
    render_path        TEXT,
    status             TEXT NOT NULL DEFAULT 'picked'
                       CHECK (status IN ('picked','rendered','scheduled','uploaded','failed','skipped')),
    status_reason      TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    CHECK (end_sec > start_sec)
);
CREATE INDEX idx_clips_status ON clips(status);
CREATE INDEX idx_clips_source ON clips(source_id);

CREATE TABLE uploads (
    id               INTEGER PRIMARY KEY,
    clip_id          INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    youtube_video_id TEXT UNIQUE,
    privacy          TEXT NOT NULL DEFAULT 'private',
    publish_at       TEXT,                       -- scheduled public time (UTC), NULL = immediate
    uploaded_at      TEXT,
    status           TEXT NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued','uploaded','failed','removed')),
    quota_units      INTEGER NOT NULL DEFAULT 0,
    error            TEXT,
    copyright_claim  INTEGER NOT NULL DEFAULT 0,
    dry_run          INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX idx_uploads_clip ON uploads(clip_id);
CREATE INDEX idx_uploads_uploaded_at ON uploads(uploaded_at);

CREATE TABLE stats (
    id                     INTEGER PRIMARY KEY,
    youtube_video_id       TEXT NOT NULL,
    day                    TEXT NOT NULL,        -- local YYYY-MM-DD the snapshot covers
    views                  INTEGER NOT NULL DEFAULT 0,
    watch_minutes          REAL NOT NULL DEFAULT 0,
    avg_view_duration_sec  REAL,
    avg_view_pct           REAL,                 -- 0-100+
    likes                  INTEGER NOT NULL DEFAULT 0,
    comments               INTEGER NOT NULL DEFAULT 0,
    shares                 INTEGER NOT NULL DEFAULT 0,
    subscribers_gained     INTEGER NOT NULL DEFAULT 0,
    subscribers_lost       INTEGER NOT NULL DEFAULT 0,
    retention              TEXT,                 -- JSON list of [elapsed_ratio, audience_watch_ratio]
    fetched_at             TEXT NOT NULL,
    UNIQUE (youtube_video_id, day)
);
CREATE INDEX idx_stats_day ON stats(day);

CREATE TABLE channel_stats (
    day          TEXT PRIMARY KEY,               -- local YYYY-MM-DD
    subscribers  INTEGER,
    total_views  INTEGER,
    video_count  INTEGER,
    fetched_at   TEXT NOT NULL
);

CREATE TABLE decisions (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,                 -- new_niche | new_source_type | budget_increase | strategy_change | other
    summary       TEXT NOT NULL,                 -- speakable text read on the phone call
    payload       TEXT,                          -- JSON: what gets applied on approval
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','approved','rejected','expired')),
    requested_at  TEXT NOT NULL,
    expires_at    TEXT,
    resolved_at   TEXT,
    resolved_via  TEXT,                          -- call | sms | dashboard | cli | timeout
    resolution_note TEXT,
    call_sid      TEXT
);
CREATE INDEX idx_decisions_status ON decisions(status);

CREATE TABLE llm_usage (
    id                 INTEGER PRIMARY KEY,
    ts                 TEXT NOT NULL,
    model              TEXT NOT NULL,
    tier               TEXT,                     -- haiku | sonnet
    purpose            TEXT NOT NULL,            -- e.g. pick.score_window, strategist.review
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd           REAL NOT NULL DEFAULT 0,
    source_id          INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    clip_id            INTEGER REFERENCES clips(id) ON DELETE SET NULL,
    request_id         TEXT,
    fake               INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_llm_usage_ts ON llm_usage(ts);

CREATE TABLE strategy_versions (
    version      INTEGER PRIMARY KEY,
    json         TEXT NOT NULL,
    report       TEXT,
    created_at   TEXT NOT NULL,
    decision_id  INTEGER REFERENCES decisions(id) ON DELETE SET NULL
);

CREATE TABLE notifications (
    id           INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL,
    kind         TEXT NOT NULL,                  -- daily_summary | approval | budget_cap | error | ...
    body         TEXT NOT NULL,
    channel      TEXT NOT NULL,                  -- call | sms | log
    status       TEXT NOT NULL DEFAULT 'queued'
                 CHECK (status IN ('queued','sent','failed','dry_run','skipped')),
    sid          TEXT,
    error        TEXT,
    decision_id  INTEGER REFERENCES decisions(id) ON DELETE SET NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX idx_notifications_status ON notifications(status);

CREATE TABLE events (
    id       INTEGER PRIMARY KEY,
    ts       TEXT NOT NULL,
    level    TEXT NOT NULL DEFAULT 'info',
    module   TEXT NOT NULL,
    message  TEXT NOT NULL,
    data     TEXT                                -- JSON
);
CREATE INDEX idx_events_ts ON events(ts);

CREATE TABLE kv (
    key         TEXT PRIMARY KEY,
    value       TEXT,                            -- JSON
    updated_at  TEXT NOT NULL
);
"""

# Append new migrations; never edit a shipped one. Index i -> schema version i+1.
MIGRATIONS: list[str] = [_SCHEMA_V1]
SCHEMA_VERSION = len(MIGRATIONS)

_initialised: set[str] = set()
_init_lock = threading.Lock()


def _raw_connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(path: Path | str | None = None) -> int:
    """Create / migrate the schema. Idempotent. Returns the schema version."""
    p = Path(path) if path is not None else get_settings().db_path
    conn = _raw_connect(p)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)")
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        current = row["v"] or 0
        conn.isolation_level = None  # explicit BEGIN/COMMIT so DDL is transactional too
        for idx in range(current, len(MIGRATIONS)):
            conn.execute("BEGIN IMMEDIATE")
            try:
                # re-check inside the write lock in case another process migrated meanwhile
                if (conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"] or 0) > idx:
                    conn.execute("COMMIT")
                    continue
                for stmt in _split_sql(MIGRATIONS[idx]):
                    conn.execute(stmt)
                conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (?, ?)", (idx + 1, now_iso()))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        _initialised.add(str(p.resolve()))
        return len(MIGRATIONS)
    finally:
        conn.close()


def _split_sql(script: str) -> list[str]:
    """Split a migration script into statements (our DDL has no ';' inside literals)."""
    out = []
    for chunk in script.split(";"):
        lines = [ln.split("--", 1)[0] for ln in chunk.splitlines()]
        stmt = "\n".join(lines).strip()
        if stmt:
            out.append(stmt)
    return out


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection (WAL, foreign keys on, ``sqlite3.Row`` rows), migrating on first use."""
    p = Path(path) if path is not None else get_settings().db_path
    key = str(p.resolve())
    if key not in _initialised:
        with _init_lock:
            if key not in _initialised:
                init_db(p)
    return _raw_connect(p)


@contextmanager
def transaction(conn: sqlite3.Connection | None = None) -> Iterator[sqlite3.Connection]:
    """Yield a connection inside a transaction; commit on success, roll back on error.

    Without ``conn`` a new connection is opened, committed/rolled back and closed.
    With ``conn`` given it is yielded as-is: the caller owns commit/close. To group
    several helper calls atomically::

        with db.transaction() as c:
            sid = db.add_source(url, "nasa", conn=c)
            db.log_event("scout", "found", data={"id": sid}, conn=c)
    """
    if conn is not None:
        # Caller owns the connection and its transaction; helpers just join it.
        yield conn
        return
    c = connect()
    try:
        with c:
            yield c
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Row <-> dataclass mapping
# ---------------------------------------------------------------------------

def _dumps(value: Any) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True, ensure_ascii=False)


def _loads(text: str | None) -> Any:
    return None if text is None else json.loads(text)


class _Row:
    _JSON: ClassVar[tuple[str, ...]] = ()
    _BOOL: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def from_row(cls, row: sqlite3.Row):
        kwargs: dict[str, Any] = {}
        keys = row.keys()
        for f in fields(cls):  # type: ignore[arg-type]
            if f.name not in keys:
                continue
            v = row[f.name]
            if f.name in cls._JSON:
                v = _loads(v)
            elif f.name in cls._BOOL:
                v = bool(v)
            kwargs[f.name] = v
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}  # type: ignore[arg-type]


@dataclass(slots=True)
class Source(_Row):
    id: int
    url: str
    source_type: str
    platform: str | None
    external_id: str | None
    channel: str | None
    channel_id: str | None
    title: str | None
    description: str | None
    duration_sec: float | None
    published_at: str | None
    licence: str | None
    licence_evidence: Any
    topic: str | None
    status: str
    status_reason: str | None
    video_path: str | None
    audio_path: str | None
    transcript_path: str | None
    metadata: Any
    discovered_at: str
    updated_at: str
    _JSON: ClassVar[tuple[str, ...]] = ("licence_evidence", "metadata")


@dataclass(slots=True)
class Clip(_Row):
    id: int
    source_id: int
    start_sec: float
    end_sec: float
    haiku_score: float | None
    sonnet_rationale: str | None
    title: str
    description: str
    hashtags: list[str]
    hook_text: str
    credit_text: str
    licence: str
    source_url: str
    strategy_version: int | None
    experiment_id: str | None
    experiment_variant: str | None
    render_path: str | None
    status: str
    status_reason: str | None
    created_at: str
    updated_at: str
    _JSON: ClassVar[tuple[str, ...]] = ("hashtags",)

    @property
    def duration_sec(self) -> float:
        return self.end_sec - self.start_sec


@dataclass(slots=True)
class Upload(_Row):
    id: int
    clip_id: int
    youtube_video_id: str | None
    privacy: str
    publish_at: str | None
    uploaded_at: str | None
    status: str
    quota_units: int
    error: str | None
    copyright_claim: bool
    dry_run: bool
    created_at: str
    updated_at: str
    _BOOL: ClassVar[tuple[str, ...]] = ("copyright_claim", "dry_run")


@dataclass(slots=True)
class Stat(_Row):
    id: int
    youtube_video_id: str
    day: str
    views: int
    watch_minutes: float
    avg_view_duration_sec: float | None
    avg_view_pct: float | None
    likes: int
    comments: int
    shares: int
    subscribers_gained: int
    subscribers_lost: int
    retention: Any
    fetched_at: str
    _JSON: ClassVar[tuple[str, ...]] = ("retention",)


@dataclass(slots=True)
class ChannelStat(_Row):
    day: str
    subscribers: int | None
    total_views: int | None
    video_count: int | None
    fetched_at: str


@dataclass(slots=True)
class Decision(_Row):
    id: int
    kind: str
    summary: str
    payload: Any
    status: str
    requested_at: str
    expires_at: str | None
    resolved_at: str | None
    resolved_via: str | None
    resolution_note: str | None
    call_sid: str | None
    _JSON: ClassVar[tuple[str, ...]] = ("payload",)


@dataclass(slots=True)
class LLMUsageRow(_Row):
    id: int
    ts: str
    model: str
    tier: str | None
    purpose: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    source_id: int | None
    clip_id: int | None
    request_id: str | None
    fake: bool
    _BOOL: ClassVar[tuple[str, ...]] = ("fake",)


@dataclass(slots=True)
class StrategyVersion(_Row):
    version: int
    json: Any
    report: str | None
    created_at: str
    decision_id: int | None
    _JSON: ClassVar[tuple[str, ...]] = ("json",)


@dataclass(slots=True)
class Notification(_Row):
    id: int
    ts: str
    kind: str
    body: str
    channel: str
    status: str
    sid: str | None
    error: str | None
    decision_id: int | None
    updated_at: str


@dataclass(slots=True)
class Event(_Row):
    id: int
    ts: str
    level: str
    module: str
    message: str
    data: Any
    _JSON: ClassVar[tuple[str, ...]] = ("data",)


T = TypeVar("T", bound=_Row)


def _one(conn: sqlite3.Connection, cls: type[T], sql: str, params: Sequence[Any] = ()) -> T | None:
    row = conn.execute(sql, params).fetchone()
    return None if row is None else cls.from_row(row)


def _many(conn: sqlite3.Connection, cls: type[T], sql: str, params: Sequence[Any] = ()) -> list[T]:
    return [cls.from_row(r) for r in conn.execute(sql, params).fetchall()]


def _insert(conn: sqlite3.Connection, table: str, values: dict[str, Any]) -> int:
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
    return int(cur.lastrowid)  # type: ignore[arg-type]


def _update(
    conn: sqlite3.Connection,
    table: str,
    row_id: int,
    values: dict[str, Any],
    *,
    allowed: frozenset[str],
    json_cols: Sequence[str] = (),
    bool_cols: Sequence[str] = (),
    pk: str = "id",
    touch: bool = True,
) -> None:
    bad = set(values) - allowed
    if bad:
        raise ValueError(f"cannot update {table} columns: {sorted(bad)}")
    enc: dict[str, Any] = {}
    for k, v in values.items():
        if k in json_cols:
            v = _dumps(v)
        elif k in bool_cols:
            v = int(bool(v))
        elif isinstance(v, Path):
            v = str(v)
        enc[k] = v
    if touch:
        enc["updated_at"] = now_iso()
    if not enc:
        return
    sets = ", ".join(f"{k} = ?" for k in enc)
    cur = conn.execute(f"UPDATE {table} SET {sets} WHERE {pk} = ?", (*enc.values(), row_id))
    if cur.rowcount == 0:
        raise NotFound(f"{table} {pk}={row_id}")


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------

_SOURCE_UPDATABLE = frozenset({
    "source_type", "platform", "external_id", "channel", "channel_id", "title", "description",
    "duration_sec", "published_at", "licence", "licence_evidence", "topic", "video_path",
    "audio_path", "transcript_path", "metadata", "status_reason",
})


def add_source(
    url: str,
    source_type: str,
    *,
    platform: str | None = None,
    external_id: str | None = None,
    channel: str | None = None,
    channel_id: str | None = None,
    title: str | None = None,
    description: str | None = None,
    duration_sec: float | None = None,
    published_at: str | None = None,
    topic: str | None = None,
    licence: str | None = None,
    licence_evidence: Any = None,
    metadata: Any = None,
    conn: sqlite3.Connection | None = None,
) -> int | None:
    """Insert a ``candidate`` source. Returns the new id, or ``None`` if the URL is already known."""
    now = now_iso()
    with transaction(conn) as c:
        cur = c.execute(
            """INSERT OR IGNORE INTO sources (url, source_type, platform, external_id, channel, channel_id,
                   title, description, duration_sec, published_at, topic, licence, licence_evidence,
                   metadata, status, discovered_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'candidate',?,?)""",
            (url, source_type, platform, external_id, channel, channel_id, title, description,
             duration_sec, published_at, topic, licence, _dumps(licence_evidence), _dumps(metadata), now, now),
        )
        return int(cur.lastrowid) if cur.rowcount else None  # type: ignore[arg-type]


def get_source(source_id: int, *, conn: sqlite3.Connection | None = None) -> Source | None:
    with transaction(conn) as c:
        return _one(c, Source, "SELECT * FROM sources WHERE id = ?", (source_id,))


def require_source(source_id: int, *, conn: sqlite3.Connection | None = None) -> Source:
    s = get_source(source_id, conn=conn)
    if s is None:
        raise NotFound(f"source {source_id}")
    return s


def get_source_by_url(url: str, *, conn: sqlite3.Connection | None = None) -> Source | None:
    with transaction(conn) as c:
        return _one(c, Source, "SELECT * FROM sources WHERE url = ?", (url,))


def list_sources(
    status: str | Sequence[str] | None = None,
    *,
    source_type: str | None = None,
    limit: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[Source]:
    """Sources oldest-discovered first, optionally filtered."""
    where, params = [], []
    if status is not None:
        sts = [status] if isinstance(status, str) else list(status)
        where.append(f"status IN ({','.join('?' * len(sts))})")
        params += sts
    if source_type is not None:
        where.append("source_type = ?")
        params.append(source_type)
    sql = "SELECT * FROM sources" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY discovered_at, id"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    with transaction(conn) as c:
        return _many(c, Source, sql, params)


def update_source(source_id: int, *, conn: sqlite3.Connection | None = None, **values: Any) -> None:
    """Update non-status columns (paths, licence, metadata...). Use :func:`set_source_status` for status."""
    with transaction(conn) as c:
        _update(c, "sources", source_id, values, allowed=_SOURCE_UPDATABLE,
                json_cols=Source._JSON)


def set_source_status(
    source_id: int,
    status: str,
    reason: str | None = None,
    *,
    force: bool = False,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Move a source along its state machine (``SOURCE_TRANSITIONS``).

    Setting the current status again is a no-op apart from ``reason``.
    ``force=True`` bypasses the transition check (manual repair only).
    """
    if status not in SOURCE_STATUSES:
        raise ValueError(f"unknown source status {status!r}")
    with transaction(conn) as c:
        cur = require_source(source_id, conn=c).status
        if not force and status != cur and status not in SOURCE_TRANSITIONS[cur]:
            raise InvalidTransition(f"source {source_id}: {cur} -> {status}")
        c.execute("UPDATE sources SET status = ?, status_reason = ?, updated_at = ? WHERE id = ?",
                  (status, reason, now_iso(), source_id))


def count_sources_by_status(*, conn: sqlite3.Connection | None = None) -> dict[str, int]:
    with transaction(conn) as c:
        rows = c.execute("SELECT status, COUNT(*) AS n FROM sources GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


# ---------------------------------------------------------------------------
# clips
# ---------------------------------------------------------------------------

_CLIP_UPDATABLE = frozenset({
    "start_sec", "end_sec", "haiku_score", "sonnet_rationale", "title", "description", "hashtags",
    "hook_text", "credit_text", "strategy_version", "experiment_id", "experiment_variant",
    "render_path", "status_reason",
})


def add_clip(
    source_id: int,
    start_sec: float,
    end_sec: float,
    *,
    title: str = "",
    description: str = "",
    hashtags: Sequence[str] = (),
    hook_text: str = "",
    credit_text: str = "",
    haiku_score: float | None = None,
    sonnet_rationale: str | None = None,
    strategy_version: int | None = None,
    experiment_id: str | None = None,
    experiment_variant: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Insert a ``picked`` clip. Licence and source URL are copied from the source for audit.

    Raises ``ValueError`` if the source has no recorded licence (never clip unlicensed footage).
    """
    if end_sec <= start_sec:
        raise ValueError("end_sec must be greater than start_sec")
    now = now_iso()
    with transaction(conn) as c:
        src = require_source(source_id, conn=c)
        if not src.licence:
            raise ValueError(f"source {source_id} has no licence recorded; refusing to create a clip")
        return _insert(c, "clips", {
            "source_id": source_id, "start_sec": float(start_sec), "end_sec": float(end_sec),
            "haiku_score": haiku_score, "sonnet_rationale": sonnet_rationale, "title": title,
            "description": description, "hashtags": _dumps(list(hashtags)), "hook_text": hook_text,
            "credit_text": credit_text, "licence": src.licence, "source_url": src.url,
            "strategy_version": strategy_version, "experiment_id": experiment_id,
            "experiment_variant": experiment_variant, "status": "picked",
            "created_at": now, "updated_at": now,
        })


def get_clip(clip_id: int, *, conn: sqlite3.Connection | None = None) -> Clip | None:
    with transaction(conn) as c:
        return _one(c, Clip, "SELECT * FROM clips WHERE id = ?", (clip_id,))


def require_clip(clip_id: int, *, conn: sqlite3.Connection | None = None) -> Clip:
    clip = get_clip(clip_id, conn=conn)
    if clip is None:
        raise NotFound(f"clip {clip_id}")
    return clip


def list_clips(
    status: str | Sequence[str] | None = None,
    *,
    source_id: int | None = None,
    limit: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[Clip]:
    """Clips oldest first, optionally filtered by status(es) and source."""
    where, params = [], []
    if status is not None:
        sts = [status] if isinstance(status, str) else list(status)
        where.append(f"status IN ({','.join('?' * len(sts))})")
        params += sts
    if source_id is not None:
        where.append("source_id = ?")
        params.append(source_id)
    sql = "SELECT * FROM clips" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at, id"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    with transaction(conn) as c:
        return _many(c, Clip, sql, params)


def update_clip(clip_id: int, *, conn: sqlite3.Connection | None = None, **values: Any) -> None:
    """Update non-status clip columns. Use :func:`set_clip_status` for status."""
    if "hashtags" in values and values["hashtags"] is not None:
        values["hashtags"] = list(values["hashtags"])
    with transaction(conn) as c:
        _update(c, "clips", clip_id, values, allowed=_CLIP_UPDATABLE, json_cols=Clip._JSON)


def set_clip_status(
    clip_id: int,
    status: str,
    reason: str | None = None,
    *,
    force: bool = False,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Move a clip along ``CLIP_TRANSITIONS`` (same rules as :func:`set_source_status`)."""
    if status not in CLIP_STATUSES:
        raise ValueError(f"unknown clip status {status!r}")
    with transaction(conn) as c:
        cur = require_clip(clip_id, conn=c).status
        if not force and status != cur and status not in CLIP_TRANSITIONS[cur]:
            raise InvalidTransition(f"clip {clip_id}: {cur} -> {status}")
        c.execute("UPDATE clips SET status = ?, status_reason = ?, updated_at = ? WHERE id = ?",
                  (status, reason, now_iso(), clip_id))


# ---------------------------------------------------------------------------
# uploads
# ---------------------------------------------------------------------------

_UPLOAD_UPDATABLE = frozenset({
    "youtube_video_id", "privacy", "publish_at", "uploaded_at", "status", "quota_units", "error",
    "copyright_claim", "dry_run",
})


def add_upload(
    clip_id: int,
    *,
    privacy: str,
    publish_at: str | None = None,
    dry_run: bool = False,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Create a ``queued`` upload row before calling the YouTube API."""
    if privacy not in ("private", "unlisted", "public"):
        raise ValueError(f"bad privacy {privacy!r}")
    now = now_iso()
    with transaction(conn) as c:
        require_clip(clip_id, conn=c)
        return _insert(c, "uploads", {
            "clip_id": clip_id, "privacy": privacy, "publish_at": publish_at, "status": "queued",
            "dry_run": int(dry_run), "created_at": now, "updated_at": now,
        })


def mark_upload_succeeded(
    upload_id: int,
    youtube_video_id: str,
    *,
    quota_units: int = UPLOAD_QUOTA_UNITS,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Record a successful videos.insert (also used for dry runs with a fake id)."""
    with transaction(conn) as c:
        _update(c, "uploads", upload_id, {
            "youtube_video_id": youtube_video_id, "status": "uploaded", "uploaded_at": now_iso(),
            "quota_units": quota_units, "error": None,
        }, allowed=_UPLOAD_UPDATABLE)


def mark_upload_failed(
    upload_id: int, error: str, *, quota_units: int = 0, conn: sqlite3.Connection | None = None
) -> None:
    with transaction(conn) as c:
        _update(c, "uploads", upload_id, {"status": "failed", "error": error, "quota_units": quota_units},
                allowed=_UPLOAD_UPDATABLE)


def update_upload(upload_id: int, *, conn: sqlite3.Connection | None = None, **values: Any) -> None:
    with transaction(conn) as c:
        _update(c, "uploads", upload_id, values, allowed=_UPLOAD_UPDATABLE, bool_cols=Upload._BOOL)


def get_upload(upload_id: int, *, conn: sqlite3.Connection | None = None) -> Upload | None:
    with transaction(conn) as c:
        return _one(c, Upload, "SELECT * FROM uploads WHERE id = ?", (upload_id,))


def get_upload_by_video(youtube_video_id: str, *, conn: sqlite3.Connection | None = None) -> Upload | None:
    with transaction(conn) as c:
        return _one(c, Upload, "SELECT * FROM uploads WHERE youtube_video_id = ?", (youtube_video_id,))


def list_uploads(
    status: str | Sequence[str] | None = None,
    *,
    clip_id: int | None = None,
    include_dry_run: bool = True,
    limit: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[Upload]:
    """Uploads newest first."""
    where, params = [], []
    if status is not None:
        sts = [status] if isinstance(status, str) else list(status)
        where.append(f"status IN ({','.join('?' * len(sts))})")
        params += sts
    if clip_id is not None:
        where.append("clip_id = ?")
        params.append(clip_id)
    if not include_dry_run:
        where.append("dry_run = 0")
    sql = "SELECT * FROM uploads" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at DESC, id DESC"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    with transaction(conn) as c:
        return _many(c, Upload, sql, params)


def count_uploads_on(day: date | None = None, *, conn: sqlite3.Connection | None = None) -> int:
    """Successful uploads (dry runs included) whose ``uploaded_at`` falls on a local day.

    This is what the daily upload cap counts.
    """
    start, end = local_day_bounds(day)
    with transaction(conn) as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM uploads WHERE status = 'uploaded' AND uploaded_at >= ? AND uploaded_at < ?",
            (start, end)).fetchone()
    return int(row["n"])


def quota_used_on(day: date | None = None, *, conn: sqlite3.Connection | None = None) -> int:
    """YouTube quota units recorded by uploads created on a local day (dry runs excluded)."""
    start, end = local_day_bounds(day)
    with transaction(conn) as c:
        row = c.execute(
            "SELECT COALESCE(SUM(quota_units), 0) AS q FROM uploads WHERE dry_run = 0 AND created_at >= ? AND created_at < ?",
            (start, end)).fetchone()
    return int(row["q"])


def publish_times_between(start_iso: str, end_iso: str, *, conn: sqlite3.Connection | None = None) -> list[str]:
    """``publish_at`` values already taken in ``[start, end)`` (queued or uploaded), ascending."""
    with transaction(conn) as c:
        rows = c.execute(
            "SELECT publish_at FROM uploads WHERE status IN ('queued','uploaded') AND publish_at >= ? AND publish_at < ? "
            "ORDER BY publish_at", (start_iso, end_iso)).fetchall()
    return [r["publish_at"] for r in rows]


def live_video_ids(*, conn: sqlite3.Connection | None = None) -> list[str]:
    """YouTube ids of real (non-dry-run) uploaded videos - what analytics should poll."""
    with transaction(conn) as c:
        rows = c.execute(
            "SELECT youtube_video_id FROM uploads WHERE status = 'uploaded' AND dry_run = 0 "
            "AND youtube_video_id IS NOT NULL ORDER BY uploaded_at").fetchall()
    return [r["youtube_video_id"] for r in rows]


def set_copyright_claim(youtube_video_id: str, claimed: bool = True, *, conn: sqlite3.Connection | None = None) -> None:
    with transaction(conn) as c:
        cur = c.execute("UPDATE uploads SET copyright_claim = ?, updated_at = ? WHERE youtube_video_id = ?",
                        (int(claimed), now_iso(), youtube_video_id))
        if cur.rowcount == 0:
            raise NotFound(f"upload video {youtube_video_id}")


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

def upsert_stat(
    youtube_video_id: str,
    day: date | str,
    *,
    views: int = 0,
    watch_minutes: float = 0.0,
    avg_view_duration_sec: float | None = None,
    avg_view_pct: float | None = None,
    likes: int = 0,
    comments: int = 0,
    shares: int = 0,
    subscribers_gained: int = 0,
    subscribers_lost: int = 0,
    retention: Any = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Insert or replace the snapshot for (video, day)."""
    day_s = day.isoformat() if isinstance(day, date) else day
    with transaction(conn) as c:
        c.execute(
            """INSERT INTO stats (youtube_video_id, day, views, watch_minutes, avg_view_duration_sec, avg_view_pct,
                   likes, comments, shares, subscribers_gained, subscribers_lost, retention, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (youtube_video_id, day) DO UPDATE SET
                   views=excluded.views, watch_minutes=excluded.watch_minutes,
                   avg_view_duration_sec=excluded.avg_view_duration_sec, avg_view_pct=excluded.avg_view_pct,
                   likes=excluded.likes, comments=excluded.comments, shares=excluded.shares,
                   subscribers_gained=excluded.subscribers_gained, subscribers_lost=excluded.subscribers_lost,
                   retention=excluded.retention, fetched_at=excluded.fetched_at""",
            (youtube_video_id, day_s, views, watch_minutes, avg_view_duration_sec, avg_view_pct, likes, comments,
             shares, subscribers_gained, subscribers_lost, _dumps(retention), now_iso()),
        )


def get_stats(youtube_video_id: str, *, conn: sqlite3.Connection | None = None) -> list[Stat]:
    """All daily snapshots for a video, oldest day first."""
    with transaction(conn) as c:
        return _many(c, Stat, "SELECT * FROM stats WHERE youtube_video_id = ? ORDER BY day", (youtube_video_id,))


def stats_between(start_day: date | str, end_day: date | str, *, conn: sqlite3.Connection | None = None) -> list[Stat]:
    """Snapshots with ``start_day <= day <= end_day`` (inclusive)."""
    s = start_day.isoformat() if isinstance(start_day, date) else start_day
    e = end_day.isoformat() if isinstance(end_day, date) else end_day
    with transaction(conn) as c:
        return _many(c, Stat, "SELECT * FROM stats WHERE day >= ? AND day <= ? ORDER BY day, youtube_video_id", (s, e))


def upsert_channel_stat(
    day: date | str,
    *,
    subscribers: int | None = None,
    total_views: int | None = None,
    video_count: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    day_s = day.isoformat() if isinstance(day, date) else day
    with transaction(conn) as c:
        c.execute(
            """INSERT INTO channel_stats (day, subscribers, total_views, video_count, fetched_at) VALUES (?,?,?,?,?)
               ON CONFLICT (day) DO UPDATE SET subscribers=excluded.subscribers, total_views=excluded.total_views,
                   video_count=excluded.video_count, fetched_at=excluded.fetched_at""",
            (day_s, subscribers, total_views, video_count, now_iso()))


def list_channel_stats(limit: int = 90, *, conn: sqlite3.Connection | None = None) -> list[ChannelStat]:
    """Most recent ``limit`` days, oldest first."""
    with transaction(conn) as c:
        rows = _many(c, ChannelStat, "SELECT * FROM channel_stats ORDER BY day DESC LIMIT ?", (limit,))
    return list(reversed(rows))


def clip_performance(since_day: date | str | None = None, *, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """One row per uploaded (non-dry-run) clip with lifetime totals from ``stats``.

    Keys: clip_id, source_id, source_type, channel, title, hook_text, start_sec, end_sec,
    haiku_score, strategy_version, experiment_id, experiment_variant, youtube_video_id,
    publish_at, uploaded_at, copyright_claim, views, watch_minutes, likes, comments, shares,
    subscribers_gained, subscribers_lost, avg_view_pct (mean of daily values), days_tracked.
    ``since_day`` filters on local upload day >= since_day (approximated on UTC timestamp).
    """
    params: list[Any] = []
    where = "u.status = 'uploaded' AND u.dry_run = 0"
    if since_day is not None:
        d = since_day if isinstance(since_day, date) else date.fromisoformat(since_day)
        where += " AND u.uploaded_at >= ?"
        params.append(local_day_bounds(d)[0])
    sql = f"""
        SELECT c.id AS clip_id, c.source_id, s.source_type, s.channel, c.title, c.hook_text, c.start_sec, c.end_sec,
               c.haiku_score, c.strategy_version, c.experiment_id, c.experiment_variant,
               u.youtube_video_id, u.publish_at, u.uploaded_at, u.copyright_claim,
               COALESCE(SUM(st.views), 0) AS views, COALESCE(SUM(st.watch_minutes), 0) AS watch_minutes,
               COALESCE(SUM(st.likes), 0) AS likes, COALESCE(SUM(st.comments), 0) AS comments,
               COALESCE(SUM(st.shares), 0) AS shares,
               COALESCE(SUM(st.subscribers_gained), 0) AS subscribers_gained,
               COALESCE(SUM(st.subscribers_lost), 0) AS subscribers_lost,
               AVG(st.avg_view_pct) AS avg_view_pct, COUNT(st.id) AS days_tracked
        FROM uploads u
        JOIN clips c ON c.id = u.clip_id
        JOIN sources s ON s.id = c.source_id
        LEFT JOIN stats st ON st.youtube_video_id = u.youtube_video_id
        WHERE {where}
        GROUP BY u.id
        ORDER BY u.uploaded_at"""
    with transaction(conn) as c:
        rows = c.execute(sql, params).fetchall()
    out = [dict(r) for r in rows]
    for r in out:
        r["copyright_claim"] = bool(r["copyright_claim"])
    return out


# ---------------------------------------------------------------------------
# decisions
# ---------------------------------------------------------------------------

def add_decision(
    kind: str,
    summary: str,
    payload: Any = None,
    *,
    expires_at: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Create a ``pending`` approval request. ``summary`` must be speakable (it is read aloud)."""
    if kind not in DECISION_KINDS:
        raise ValueError(f"unknown decision kind {kind!r}; add it to db.DECISION_KINDS")
    with transaction(conn) as c:
        return _insert(c, "decisions", {
            "kind": kind, "summary": summary, "payload": _dumps(payload), "status": "pending",
            "requested_at": now_iso(), "expires_at": expires_at,
        })


def get_decision(decision_id: int, *, conn: sqlite3.Connection | None = None) -> Decision | None:
    with transaction(conn) as c:
        return _one(c, Decision, "SELECT * FROM decisions WHERE id = ?", (decision_id,))


def list_decisions(
    status: str | None = None, *, limit: int | None = None, conn: sqlite3.Connection | None = None
) -> list[Decision]:
    """Decisions newest first."""
    sql, params = "SELECT * FROM decisions", []
    if status is not None:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY requested_at DESC, id DESC"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    with transaction(conn) as c:
        return _many(c, Decision, sql, params)


def resolve_decision(
    decision_id: int,
    status: str,
    *,
    via: str,
    note: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Resolve a *pending* decision. Returns False if it was already resolved (first answer wins)."""
    if status not in ("approved", "rejected", "expired"):
        raise ValueError(f"bad resolution status {status!r}")
    if via not in RESOLVED_VIA:
        raise ValueError(f"bad resolved_via {via!r}")
    with transaction(conn) as c:
        cur = c.execute(
            "UPDATE decisions SET status = ?, resolved_at = ?, resolved_via = ?, resolution_note = ? "
            "WHERE id = ? AND status = 'pending'", (status, now_iso(), via, note, decision_id))
        if cur.rowcount == 0 and get_decision(decision_id, conn=c) is None:
            raise NotFound(f"decision {decision_id}")
        return cur.rowcount == 1


def set_decision_call_sid(decision_id: int, call_sid: str, *, conn: sqlite3.Connection | None = None) -> None:
    with transaction(conn) as c:
        _update(c, "decisions", decision_id, {"call_sid": call_sid}, allowed=frozenset({"call_sid"}), touch=False)


def get_decision_by_call_sid(call_sid: str, *, conn: sqlite3.Connection | None = None) -> Decision | None:
    with transaction(conn) as c:
        return _one(c, Decision, "SELECT * FROM decisions WHERE call_sid = ?", (call_sid,))


def expire_decisions(*, conn: sqlite3.Connection | None = None) -> int:
    """Mark pending decisions whose ``expires_at`` has passed as expired. Returns how many."""
    now = now_iso()
    with transaction(conn) as c:
        cur = c.execute(
            "UPDATE decisions SET status = 'expired', resolved_at = ?, resolved_via = 'timeout' "
            "WHERE status = 'pending' AND expires_at IS NOT NULL AND expires_at <= ?", (now, now))
        return cur.rowcount


# ---------------------------------------------------------------------------
# llm_usage (written via budget.record_usage)
# ---------------------------------------------------------------------------

def insert_llm_usage(
    *,
    model: str,
    purpose: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cost_usd: float,
    tier: str | None = None,
    source_id: int | None = None,
    clip_id: int | None = None,
    request_id: str | None = None,
    fake: bool = False,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Low-level insert. Other modules should call ``budget.record_usage`` instead."""
    with transaction(conn) as c:
        return _insert(c, "llm_usage", {
            "ts": now_iso(), "model": model, "tier": tier, "purpose": purpose,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens, "cache_write_tokens": cache_write_tokens,
            "cost_usd": cost_usd, "source_id": source_id, "clip_id": clip_id,
            "request_id": request_id, "fake": int(fake),
        })


def llm_spend_between(start_iso: str, end_iso: str, *, conn: sqlite3.Connection | None = None) -> float:
    with transaction(conn) as c:
        row = c.execute("SELECT COALESCE(SUM(cost_usd), 0) AS s FROM llm_usage WHERE ts >= ? AND ts < ?",
                        (start_iso, end_iso)).fetchone()
    return float(row["s"])


def llm_usage_summary(day: date | None = None, *, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """Per (model, purpose) totals for a local day: calls, tokens, cost_usd."""
    start, end = local_day_bounds(day)
    with transaction(conn) as c:
        rows = c.execute(
            """SELECT model, purpose, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens,
                      SUM(output_tokens) AS output_tokens, SUM(cache_read_tokens) AS cache_read_tokens,
                      SUM(cache_write_tokens) AS cache_write_tokens, SUM(cost_usd) AS cost_usd
               FROM llm_usage WHERE ts >= ? AND ts < ? GROUP BY model, purpose ORDER BY cost_usd DESC""",
            (start, end)).fetchall()
    return [dict(r) for r in rows]


def list_llm_usage(limit: int = 100, *, conn: sqlite3.Connection | None = None) -> list[LLMUsageRow]:
    with transaction(conn) as c:
        return _many(c, LLMUsageRow, "SELECT * FROM llm_usage ORDER BY ts DESC, id DESC LIMIT ?", (limit,))


# ---------------------------------------------------------------------------
# strategy_versions (written via strategy.save_strategy)
# ---------------------------------------------------------------------------

def add_strategy_version(
    version: int,
    strategy_json: Any,
    *,
    report: str | None = None,
    decision_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    with transaction(conn) as c:
        c.execute(
            "INSERT INTO strategy_versions (version, json, report, created_at, decision_id) VALUES (?,?,?,?,?)",
            (version, _dumps(strategy_json), report, now_iso(), decision_id))


def get_strategy_version(version: int, *, conn: sqlite3.Connection | None = None) -> StrategyVersion | None:
    with transaction(conn) as c:
        return _one(c, StrategyVersion, "SELECT * FROM strategy_versions WHERE version = ?", (version,))


def latest_strategy_version(*, conn: sqlite3.Connection | None = None) -> StrategyVersion | None:
    with transaction(conn) as c:
        return _one(c, StrategyVersion, "SELECT * FROM strategy_versions ORDER BY version DESC LIMIT 1")


def list_strategy_versions(limit: int = 20, *, conn: sqlite3.Connection | None = None) -> list[StrategyVersion]:
    """Newest first."""
    with transaction(conn) as c:
        return _many(c, StrategyVersion, "SELECT * FROM strategy_versions ORDER BY version DESC LIMIT ?", (limit,))


# ---------------------------------------------------------------------------
# notifications
# ---------------------------------------------------------------------------

def add_notification(
    kind: str,
    body: str,
    channel: str,
    *,
    status: str = "queued",
    sid: str | None = None,
    error: str | None = None,
    decision_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    if channel not in NOTIFICATION_CHANNELS:
        raise ValueError(f"bad channel {channel!r}")
    if status not in NOTIFICATION_STATUSES:
        raise ValueError(f"bad notification status {status!r}")
    now = now_iso()
    with transaction(conn) as c:
        return _insert(c, "notifications", {
            "ts": now, "kind": kind, "body": body, "channel": channel, "status": status, "sid": sid,
            "error": error, "decision_id": decision_id, "updated_at": now,
        })


def update_notification(
    notification_id: int,
    *,
    status: str,
    sid: str | None = None,
    error: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    if status not in NOTIFICATION_STATUSES:
        raise ValueError(f"bad notification status {status!r}")
    with transaction(conn) as c:
        _update(c, "notifications", notification_id, {"status": status, "sid": sid, "error": error},
                allowed=frozenset({"status", "sid", "error"}))


def list_notifications(
    status: str | None = None, *, limit: int | None = 100, conn: sqlite3.Connection | None = None
) -> list[Notification]:
    """Newest first (``status='queued'`` lists are oldest first, for sending in order)."""
    sql, params = "SELECT * FROM notifications", []
    if status is not None:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY ts, id" if status == "queued" else " ORDER BY ts DESC, id DESC"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    with transaction(conn) as c:
        return _many(c, Notification, sql, params)


# ---------------------------------------------------------------------------
# events (audit / run log)
# ---------------------------------------------------------------------------

def log_event(
    module: str,
    message: str,
    *,
    level: str = "info",
    data: Any = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Append to the pipeline audit log shown on the dashboard."""
    if level not in EVENT_LEVELS:
        raise ValueError(f"bad level {level!r}")
    with transaction(conn) as c:
        return _insert(c, "events", {"ts": now_iso(), "level": level, "module": module,
                                     "message": message, "data": _dumps(data)})


def list_events(
    *,
    limit: int = 100,
    level: str | None = None,
    module: str | None = None,
    since: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[Event]:
    """Events newest first."""
    where, params = [], []
    if level is not None:
        where.append("level = ?")
        params.append(level)
    if module is not None:
        where.append("module = ?")
        params.append(module)
    if since is not None:
        where.append("ts >= ?")
        params.append(since)
    sql = "SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY ts DESC, id DESC LIMIT ?"
    params.append(limit)
    with transaction(conn) as c:
        return _many(c, Event, sql, params)


# ---------------------------------------------------------------------------
# kv (small persistent flags: pause switch, once-per-day alerts, scout cursors)
# ---------------------------------------------------------------------------

def kv_get(key: str, default: Any = None, *, conn: sqlite3.Connection | None = None) -> Any:
    with transaction(conn) as c:
        row = c.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return default if row is None else _loads(row["value"])


def kv_set(key: str, value: Any, *, conn: sqlite3.Connection | None = None) -> None:
    with transaction(conn) as c:
        c.execute("INSERT INTO kv (key, value, updated_at) VALUES (?,?,?) "
                  "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                  (key, _dumps(value), now_iso()))


def kv_set_if_absent(key: str, value: Any, *, conn: sqlite3.Connection | None = None) -> bool:
    """Atomically set ``key`` only if missing. Returns True if this call set it."""
    with transaction(conn) as c:
        cur = c.execute("INSERT OR IGNORE INTO kv (key, value, updated_at) VALUES (?,?,?)",
                        (key, _dumps(value), now_iso()))
        return cur.rowcount == 1


def kv_delete(key: str, *, conn: sqlite3.Connection | None = None) -> None:
    with transaction(conn) as c:
        c.execute("DELETE FROM kv WHERE key = ?", (key,))
