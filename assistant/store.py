"""The assistant's tables, in the SAME SQLite database as the pipeline.

``db.py`` is not touched: this module keeps its own migration list (``MIGRATIONS``)
and records its version in the phase-1 ``kv`` table under ``assistant:schema_version``.
Connections and time helpers come from ``db`` (``db.transaction``, ``db.now_iso``,
``db.local_today``...). Every helper takes keyword ``conn=`` to join a caller's
transaction, exactly like ``db``.

Tables (all prefixed ``as_``):

==================  ===================================================================
as_messages         conversation log with the owner (every inbound/outbound message)
as_checkins         scheduled questions to the owner and their answers
as_jobs             the owner's paid jobs (hourly rate, pay schedule, tax estimate)
as_work_log         hours per job per local week (week_start = Monday), upserted
as_income           money in: job pay, reels revenue, other; idempotent on (source, ref)
as_meals            meal suggestions and the owner's feedback
as_goals            goals with a metric/target/current value
as_agent_runs       audit trail: one row per agent or sub-agent run (cost, status)
as_lessons          self-improvement memory: proposed -> adopted -> retired
as_vault_index      vault search index bookkeeping (path, mtime, sha) + FTS5 table
==================  ===================================================================

Assistant LLM spend is written to the phase-1 ``llm_usage`` table with
``purpose LIKE 'assistant.%'`` and ``tier = 'assistant'``; :func:`assistant_spend_between`
sums only those rows.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import date, timedelta
from typing import Any, ClassVar, Iterator, Sequence, TypeVar

import config
import db

VERSION_KEY = "assistant:schema_version"

DIRECTIONS = ("in", "out")
CHANNELS = ("sms", "dashboard", "call", "system")
CHECKIN_STATUSES = ("pending", "answered", "skipped", "expired")
PAY_SCHEDULES = ("weekly", "biweekly", "semimonthly", "monthly")
INCOME_SOURCES = ("job", "reels", "other")
MEAL_SLOTS = ("breakfast", "lunch", "dinner")
MEAL_STATUSES = ("suggested", "accepted", "declined", "eaten")
GOAL_STATUSES = ("active", "done", "dropped")
RUN_STATUSES = ("running", "ok", "error", "budget", "denied")
LESSON_STATUSES = ("proposed", "adopted", "retired")
ASSISTANT_PURPOSE_PREFIX = db.ASSISTANT_PURPOSE_PREFIX
ASSISTANT_TIER = "assistant"


def _in(col: str, values: Sequence[str]) -> str:
    return f"CHECK ({col} IN ({', '.join(repr(v) for v in values)}))"


# Each migration is a list of single statements (no splitting on ';'). Append only.
MIGRATIONS: list[list[str]] = [
    [
        f"""CREATE TABLE as_messages (
            id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
            direction TEXT NOT NULL {_in('direction', DIRECTIONS)},
            channel TEXT NOT NULL {_in('channel', CHANNELS)},
            body TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'chat', agent TEXT,
            cost_usd REAL NOT NULL DEFAULT 0,
            reply_to INTEGER REFERENCES as_messages(id) ON DELETE SET NULL,
            meta TEXT)""",
        "CREATE INDEX idx_as_messages_ts ON as_messages(ts)",
        f"""CREATE TABLE as_checkins (
            id INTEGER PRIMARY KEY, kind TEXT NOT NULL, scheduled_for TEXT NOT NULL, sent_at TEXT,
            question TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' {_in('status', CHECKIN_STATUSES)},
            answer_text TEXT, parsed TEXT, answered_at TEXT,
            message_id INTEGER REFERENCES as_messages(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        "CREATE INDEX idx_as_checkins_due ON as_checkins(status, scheduled_for)",
        f"""CREATE TABLE as_jobs (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, hourly_rate REAL NOT NULL DEFAULT 0,
            pay_schedule TEXT NOT NULL DEFAULT 'biweekly' {_in('pay_schedule', PAY_SCHEDULES)},
            est_tax_rate REAL NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        """CREATE TABLE as_work_log (
            id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES as_jobs(id) ON DELETE CASCADE,
            week_start TEXT NOT NULL, hours REAL NOT NULL, source TEXT NOT NULL DEFAULT 'sms', note TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE (job_id, week_start))""",
        f"""CREATE TABLE as_income (
            id INTEGER PRIMARY KEY, day TEXT NOT NULL,
            source TEXT NOT NULL {_in('source', INCOME_SOURCES)},
            amount_usd REAL NOT NULL, note TEXT, ref TEXT, created_at TEXT NOT NULL,
            UNIQUE (source, ref))""",
        "CREATE INDEX idx_as_income_day ON as_income(day)",
        f"""CREATE TABLE as_meals (
            id INTEGER PRIMARY KEY, day TEXT NOT NULL, slot TEXT NOT NULL {_in('slot', MEAL_SLOTS)},
            suggestion TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'suggested' {_in('status', MEAL_STATUSES)},
            feedback TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        "CREATE INDEX idx_as_meals_day ON as_meals(day)",
        f"""CREATE TABLE as_goals (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, metric TEXT, target REAL, current REAL,
            due TEXT, status TEXT NOT NULL DEFAULT 'active' {_in('status', GOAL_STATUSES)},
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        f"""CREATE TABLE as_agent_runs (
            id INTEGER PRIMARY KEY, ts TEXT NOT NULL, agent TEXT NOT NULL, task TEXT NOT NULL,
            model TEXT NOT NULL, duration_ms INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL {_in('status', RUN_STATUSES)}, summary TEXT,
            parent_id INTEGER REFERENCES as_agent_runs(id) ON DELETE SET NULL,
            tool_calls TEXT, fake INTEGER NOT NULL DEFAULT 0)""",
        "CREATE INDEX idx_as_agent_runs_ts ON as_agent_runs(ts)",
        f"""CREATE TABLE as_lessons (
            id INTEGER PRIMARY KEY, ts TEXT NOT NULL, area TEXT NOT NULL, lesson TEXT NOT NULL,
            evidence TEXT, status TEXT NOT NULL DEFAULT 'proposed' {_in('status', LESSON_STATUSES)},
            updated_at TEXT NOT NULL)""",
        """CREATE TABLE as_vault_index (
            path TEXT PRIMARY KEY, mtime REAL NOT NULL, sha TEXT NOT NULL, title TEXT NOT NULL,
            indexed_at TEXT NOT NULL)""",
        "CREATE VIRTUAL TABLE as_vault_fts USING fts5(path UNINDEXED, title, body, tokenize = 'porter unicode61')",
    ],
]
SCHEMA_VERSION = len(MIGRATIONS)

_migrated: set[str] = set()
_lock = threading.Lock()


def init_store(conn: sqlite3.Connection | None = None) -> int:
    """Create / migrate the assistant tables. Idempotent. Returns the assistant schema version."""
    c = conn or db.connect()
    try:
        prev_iso = c.isolation_level
        c.isolation_level = None
        try:
            for idx in range(int(db.kv_get(VERSION_KEY, 0, conn=c) or 0), SCHEMA_VERSION):
                c.execute("BEGIN IMMEDIATE")
                try:
                    if int(db.kv_get(VERSION_KEY, 0, conn=c) or 0) > idx:  # another process got there first
                        c.execute("COMMIT")
                        continue
                    for stmt in MIGRATIONS[idx]:
                        c.execute(stmt)
                    db.kv_set(VERSION_KEY, idx + 1, conn=c)
                    c.execute("COMMIT")
                except BaseException:
                    c.execute("ROLLBACK")
                    raise
        finally:
            c.isolation_level = prev_iso
    finally:
        if conn is None:
            c.close()
    return SCHEMA_VERSION


def schema_version() -> int:
    return int(db.kv_get(VERSION_KEY, 0) or 0)


@contextmanager
def transaction(conn: sqlite3.Connection | None = None) -> Iterator[sqlite3.Connection]:
    """``db.transaction`` that first makes sure the assistant tables exist."""
    if conn is None:
        key = str(config.get_settings().db_path.resolve())
        if key not in _migrated:
            with _lock:
                if key not in _migrated:
                    init_store()
                    _migrated.add(key)
    with db.transaction(conn) as c:
        yield c


def _reset_migration_cache() -> None:
    """Tests: forget which databases were migrated in this process."""
    _migrated.clear()


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------

class _Row:
    _JSON: ClassVar[tuple[str, ...]] = ()
    _BOOL: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def from_row(cls, row: sqlite3.Row):
        keys = row.keys()
        kw: dict[str, Any] = {}
        for f in fields(cls):  # type: ignore[arg-type]
            if f.name in keys:
                v = row[f.name]
                if f.name in cls._JSON and v is not None:
                    v = json.loads(v)
                elif f.name in cls._BOOL:
                    v = bool(v)
                kw[f.name] = v
        return cls(**kw)

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}  # type: ignore[arg-type]


R = TypeVar("R", bound=_Row)


def _one(c: sqlite3.Connection, cls: type[R], sql: str, params: Sequence[Any] = ()) -> R | None:
    row = c.execute(sql, params).fetchone()
    return None if row is None else cls.from_row(row)


def _many(c: sqlite3.Connection, cls: type[R], sql: str, params: Sequence[Any] = ()) -> list[R]:
    return [cls.from_row(r) for r in c.execute(sql, params).fetchall()]


def _insert(c: sqlite3.Connection, table: str, values: dict[str, Any]) -> int:
    cols = ", ".join(values)
    cur = c.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' for _ in values)})",
                    tuple(values.values()))
    return int(cur.lastrowid)  # type: ignore[arg-type]


def _update(c: sqlite3.Connection, table: str, row_id: int, values: dict[str, Any], allowed: set[str],
            *, json_cols: Sequence[str] = (), touch: bool = True) -> None:
    bad = set(values) - allowed
    if bad:
        raise ValueError(f"cannot update {table} columns: {sorted(bad)}")
    enc = {k: (json.dumps(v, sort_keys=True) if k in json_cols and v is not None else v) for k, v in values.items()}
    if touch:
        enc["updated_at"] = db.now_iso()
    if not enc:
        return
    cur = c.execute(f"UPDATE {table} SET {', '.join(f'{k} = ?' for k in enc)} WHERE id = ?", (*enc.values(), row_id))
    if cur.rowcount == 0:
        raise db.NotFound(f"{table} id={row_id}")


def _check(value: str, allowed: Sequence[str], what: str) -> str:
    if value not in allowed:
        raise ValueError(f"invalid {what} {value!r}; expected one of {allowed}")
    return value


def _day(d: date | str | None) -> str:
    if d is None:
        return db.local_today().isoformat()
    return d.isoformat() if isinstance(d, date) else date.fromisoformat(d).isoformat()


def week_start_of(day: date | str | None = None) -> date:
    """Local Monday of the week containing ``day`` (default: local today)."""
    d = date.fromisoformat(_day(day))
    return d - timedelta(days=d.weekday())


@dataclass(slots=True)
class Message(_Row):
    _JSON = ("meta",)
    id: int
    ts: str
    direction: str
    channel: str
    body: str
    kind: str = "chat"
    agent: str | None = None
    cost_usd: float = 0.0
    reply_to: int | None = None
    meta: Any = None


@dataclass(slots=True)
class Checkin(_Row):
    _JSON = ("parsed",)
    id: int
    kind: str
    scheduled_for: str
    question: str
    status: str
    sent_at: str | None = None
    answer_text: str | None = None
    parsed: Any = None
    answered_at: str | None = None
    message_id: int | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class Job(_Row):
    _BOOL = ("active",)
    id: int
    name: str
    hourly_rate: float
    pay_schedule: str
    est_tax_rate: float
    active: bool
    created_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class WorkLog(_Row):
    id: int
    job_id: int
    week_start: str
    hours: float
    source: str
    note: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class Income(_Row):
    id: int
    day: str
    source: str
    amount_usd: float
    note: str | None = None
    ref: str | None = None
    created_at: str = ""


@dataclass(slots=True)
class Meal(_Row):
    id: int
    day: str
    slot: str
    suggestion: str
    status: str
    feedback: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class Goal(_Row):
    id: int
    title: str
    status: str
    metric: str | None = None
    target: float | None = None
    current: float | None = None
    due: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class AgentRun(_Row):
    _JSON = ("tool_calls",)
    _BOOL = ("fake",)
    id: int
    ts: str
    agent: str
    task: str
    model: str
    duration_ms: int
    cost_usd: float
    status: str
    summary: str | None = None
    parent_id: int | None = None
    tool_calls: Any = None
    fake: bool = False


@dataclass(slots=True)
class Lesson(_Row):
    id: int
    ts: str
    area: str
    lesson: str
    status: str
    evidence: str | None = None
    updated_at: str = ""


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------

def add_message(direction: str, channel: str, body: str, *, kind: str = "chat", agent: str | None = None,
                cost_usd: float = 0.0, reply_to: int | None = None, meta: Any = None,
                conn: sqlite3.Connection | None = None) -> int:
    _check(direction, DIRECTIONS, "direction")
    _check(channel, CHANNELS, "channel")
    with transaction(conn) as c:
        return _insert(c, "as_messages", {
            "ts": db.now_iso(), "direction": direction, "channel": channel, "body": body, "kind": kind,
            "agent": agent, "cost_usd": float(cost_usd), "reply_to": reply_to,
            "meta": None if meta is None else json.dumps(meta, sort_keys=True)})


def get_message(message_id: int, *, conn: sqlite3.Connection | None = None) -> Message | None:
    with transaction(conn) as c:
        return _one(c, Message, "SELECT * FROM as_messages WHERE id = ?", (message_id,))


def list_messages(limit: int = 50, *, channel: str | None = None, direction: str | None = None,
                  since: str | None = None, conn: sqlite3.Connection | None = None) -> list[Message]:
    """Newest first."""
    where, params = ["1=1"], []
    for col, val in (("channel", channel), ("direction", direction)):
        if val is not None:
            where.append(f"{col} = ?")
            params.append(val)
    if since is not None:
        where.append("ts >= ?")
        params.append(since)
    with transaction(conn) as c:
        return _many(c, Message, f"SELECT * FROM as_messages WHERE {' AND '.join(where)} "
                                 "ORDER BY ts DESC, id DESC LIMIT ?", (*params, limit))


def count_outbound_between(start_iso: str, end_iso: str, *, channel: str = "sms", kinds_excluded: Sequence[str] = ("reply",),
                           conn: sqlite3.Connection | None = None) -> int:
    """Outbound messages on ``channel`` in [start, end) whose kind is not in ``kinds_excluded``."""
    marks = ", ".join("?" for _ in kinds_excluded) or "''"
    with transaction(conn) as c:
        row = c.execute(f"SELECT COUNT(*) AS n FROM as_messages WHERE direction = 'out' AND channel = ? "
                        f"AND ts >= ? AND ts < ? AND kind NOT IN ({marks})",
                        (channel, start_iso, end_iso, *kinds_excluded)).fetchone()
    return int(row["n"])


# ---------------------------------------------------------------------------
# check-ins
# ---------------------------------------------------------------------------

def add_checkin(kind: str, question: str, scheduled_for: str, *, conn: sqlite3.Connection | None = None) -> int:
    db.parse_iso(scheduled_for)  # validates the timestamp format
    now = db.now_iso()
    with transaction(conn) as c:
        return _insert(c, "as_checkins", {"kind": kind, "question": question, "scheduled_for": scheduled_for,
                                          "status": "pending", "created_at": now, "updated_at": now})


def get_checkin(checkin_id: int, *, conn: sqlite3.Connection | None = None) -> Checkin | None:
    with transaction(conn) as c:
        return _one(c, Checkin, "SELECT * FROM as_checkins WHERE id = ?", (checkin_id,))


def list_checkins(status: str | None = None, *, kind: str | None = None, limit: int = 100,
                  conn: sqlite3.Connection | None = None) -> list[Checkin]:
    """Ordered by scheduled_for (oldest first)."""
    where, params = ["1=1"], []
    if status is not None:
        where.append("status = ?")
        params.append(_check(status, CHECKIN_STATUSES, "status"))
    if kind is not None:
        where.append("kind = ?")
        params.append(kind)
    with transaction(conn) as c:
        return _many(c, Checkin, f"SELECT * FROM as_checkins WHERE {' AND '.join(where)} "
                                 "ORDER BY scheduled_for, id LIMIT ?", (*params, limit))


def due_checkins(now_iso: str | None = None, *, conn: sqlite3.Connection | None = None) -> list[Checkin]:
    """Pending, not yet sent, scheduled at or before now."""
    with transaction(conn) as c:
        return _many(c, Checkin, "SELECT * FROM as_checkins WHERE status = 'pending' AND sent_at IS NULL "
                                 "AND scheduled_for <= ? ORDER BY scheduled_for, id", (now_iso or db.now_iso(),))


def open_checkin(kind: str | None = None, *, conn: sqlite3.Connection | None = None) -> Checkin | None:
    """Most recently SENT check-in still awaiting an answer (the one an inbound reply most likely answers)."""
    sql = "SELECT * FROM as_checkins WHERE status = 'pending' AND sent_at IS NOT NULL"
    params: list[Any] = []
    if kind is not None:
        sql += " AND kind = ?"
        params.append(kind)
    with transaction(conn) as c:
        return _one(c, Checkin, sql + " ORDER BY sent_at DESC, id DESC LIMIT 1", params)


def mark_checkin_sent(checkin_id: int, *, message_id: int | None = None, conn: sqlite3.Connection | None = None) -> None:
    with transaction(conn) as c:
        _update(c, "as_checkins", checkin_id, {"sent_at": db.now_iso(), "message_id": message_id},
                {"sent_at", "message_id"})


def answer_checkin(checkin_id: int, answer_text: str, parsed: Any = None, *,
                   conn: sqlite3.Connection | None = None) -> None:
    with transaction(conn) as c:
        _update(c, "as_checkins", checkin_id, {"status": "answered", "answer_text": answer_text, "parsed": parsed,
                                               "answered_at": db.now_iso()},
                {"status", "answer_text", "parsed", "answered_at"}, json_cols=("parsed",))


def set_checkin_status(checkin_id: int, status: str, *, conn: sqlite3.Connection | None = None) -> None:
    _check(status, CHECKIN_STATUSES, "status")
    with transaction(conn) as c:
        _update(c, "as_checkins", checkin_id, {"status": status}, {"status"})


# ---------------------------------------------------------------------------
# jobs, hours, income
# ---------------------------------------------------------------------------

def upsert_job(name: str, *, hourly_rate: float, pay_schedule: str = "biweekly", est_tax_rate: float = 0.0,
               active: bool = True, conn: sqlite3.Connection | None = None) -> int:
    """Create or update the job called ``name``; returns its id."""
    _check(pay_schedule, PAY_SCHEDULES, "pay_schedule")
    if not 0 <= est_tax_rate < 1:
        raise ValueError("est_tax_rate is a fraction in [0, 1)")
    now = db.now_iso()
    with transaction(conn) as c:
        c.execute("INSERT INTO as_jobs (name, hourly_rate, pay_schedule, est_tax_rate, active, created_at, updated_at) "
                  "VALUES (?,?,?,?,?,?,?) ON CONFLICT (name) DO UPDATE SET hourly_rate = excluded.hourly_rate, "
                  "pay_schedule = excluded.pay_schedule, est_tax_rate = excluded.est_tax_rate, "
                  "active = excluded.active, updated_at = excluded.updated_at",
                  (name, float(hourly_rate), pay_schedule, float(est_tax_rate), int(active), now, now))
        return int(c.execute("SELECT id FROM as_jobs WHERE name = ?", (name,)).fetchone()["id"])


def get_job(job_id: int, *, conn: sqlite3.Connection | None = None) -> Job | None:
    with transaction(conn) as c:
        return _one(c, Job, "SELECT * FROM as_jobs WHERE id = ?", (job_id,))


def get_job_by_name(name: str, *, conn: sqlite3.Connection | None = None) -> Job | None:
    with transaction(conn) as c:
        return _one(c, Job, "SELECT * FROM as_jobs WHERE name = ? COLLATE NOCASE", (name,))


def list_jobs(*, active_only: bool = True, conn: sqlite3.Connection | None = None) -> list[Job]:
    with transaction(conn) as c:
        return _many(c, Job, "SELECT * FROM as_jobs" + (" WHERE active = 1" if active_only else "") + " ORDER BY id")


def log_hours(job_id: int, hours: float, *, week_start: date | str | None = None, source: str = "sms",
              note: str | None = None, conn: sqlite3.Connection | None = None) -> int:
    """Upsert the hours for (job, week). ``week_start`` is normalised to its Monday. Returns the row id."""
    if not 0 <= hours <= 168:
        raise ValueError(f"hours must be within a week (0-168), got {hours}")
    ws = week_start_of(week_start).isoformat()
    now = db.now_iso()
    with transaction(conn) as c:
        c.execute("INSERT INTO as_work_log (job_id, week_start, hours, source, note, created_at, updated_at) "
                  "VALUES (?,?,?,?,?,?,?) ON CONFLICT (job_id, week_start) DO UPDATE SET hours = excluded.hours, "
                  "source = excluded.source, note = excluded.note, updated_at = excluded.updated_at",
                  (job_id, ws, float(hours), source, note, now, now))
        return int(c.execute("SELECT id FROM as_work_log WHERE job_id = ? AND week_start = ?",
                             (job_id, ws)).fetchone()["id"])


def get_work_log(job_id: int, week_start: date | str | None = None, *,
                 conn: sqlite3.Connection | None = None) -> WorkLog | None:
    with transaction(conn) as c:
        return _one(c, WorkLog, "SELECT * FROM as_work_log WHERE job_id = ? AND week_start = ?",
                    (job_id, week_start_of(week_start).isoformat()))


def list_work_log(*, job_id: int | None = None, since_week: date | str | None = None, limit: int = 200,
                  conn: sqlite3.Connection | None = None) -> list[WorkLog]:
    """Newest week first."""
    where, params = ["1=1"], []
    if job_id is not None:
        where.append("job_id = ?")
        params.append(job_id)
    if since_week is not None:
        where.append("week_start >= ?")
        params.append(week_start_of(since_week).isoformat())
    with transaction(conn) as c:
        return _many(c, WorkLog, f"SELECT * FROM as_work_log WHERE {' AND '.join(where)} "
                                 "ORDER BY week_start DESC, job_id LIMIT ?", (*params, limit))


def add_income(amount_usd: float, *, source: str, day: date | str | None = None, note: str | None = None,
               ref: str | None = None, conn: sqlite3.Connection | None = None) -> int | None:
    """Record money in. With ``ref`` the call is idempotent: a repeat (source, ref) returns None."""
    _check(source, INCOME_SOURCES, "source")
    with transaction(conn) as c:
        cur = c.execute("INSERT OR IGNORE INTO as_income (day, source, amount_usd, note, ref, created_at) "
                        "VALUES (?,?,?,?,?,?)", (_day(day), source, float(amount_usd), note, ref, db.now_iso()))
        return int(cur.lastrowid) if cur.rowcount else None  # type: ignore[arg-type]


def set_income(amount_usd: float, *, source: str, ref: str, day: date | str | None = None, note: str | None = None,
               conn: sqlite3.Connection | None = None) -> int:
    """Insert or replace the income row keyed by (source, ref) - e.g. one week's pay that is
    re-logged with corrected hours. Returns the row id."""
    _check(source, INCOME_SOURCES, "source")
    if not ref:
        raise ValueError("set_income needs a ref")
    with transaction(conn) as c:
        c.execute("INSERT INTO as_income (day, source, amount_usd, note, ref, created_at) VALUES (?,?,?,?,?,?) "
                  "ON CONFLICT (source, ref) DO UPDATE SET day = excluded.day, amount_usd = excluded.amount_usd, "
                  "note = excluded.note", (_day(day), source, float(amount_usd), note, ref, db.now_iso()))
        return int(c.execute("SELECT id FROM as_income WHERE source = ? AND ref = ?", (source, ref)).fetchone()["id"])


def list_income(start_day: date | str | None = None, end_day: date | str | None = None, *,
                source: str | None = None, conn: sqlite3.Connection | None = None) -> list[Income]:
    """Inclusive day range, oldest first."""
    where, params = ["1=1"], []
    if start_day is not None:
        where.append("day >= ?")
        params.append(_day(start_day))
    if end_day is not None:
        where.append("day <= ?")
        params.append(_day(end_day))
    if source is not None:
        where.append("source = ?")
        params.append(_check(source, INCOME_SOURCES, "source"))
    with transaction(conn) as c:
        return _many(c, Income, f"SELECT * FROM as_income WHERE {' AND '.join(where)} ORDER BY day, id", params)


def income_total(start_day: date | str | None = None, end_day: date | str | None = None, *,
                 source: str | None = None, conn: sqlite3.Connection | None = None) -> float:
    return round(sum(i.amount_usd for i in list_income(start_day, end_day, source=source, conn=conn)), 2)


# ---------------------------------------------------------------------------
# meals
# ---------------------------------------------------------------------------

def add_meal(slot: str, suggestion: str, *, day: date | str | None = None,
             conn: sqlite3.Connection | None = None) -> int:
    _check(slot, MEAL_SLOTS, "slot")
    now = db.now_iso()
    with transaction(conn) as c:
        return _insert(c, "as_meals", {"day": _day(day), "slot": slot, "suggestion": suggestion,
                                       "status": "suggested", "created_at": now, "updated_at": now})


def update_meal(meal_id: int, *, status: str | None = None, feedback: str | None = None,
                conn: sqlite3.Connection | None = None) -> None:
    vals: dict[str, Any] = {}
    if status is not None:
        vals["status"] = _check(status, MEAL_STATUSES, "status")
    if feedback is not None:
        vals["feedback"] = feedback
    with transaction(conn) as c:
        _update(c, "as_meals", meal_id, vals, {"status", "feedback"})


def get_meal(meal_id: int, *, conn: sqlite3.Connection | None = None) -> Meal | None:
    with transaction(conn) as c:
        return _one(c, Meal, "SELECT * FROM as_meals WHERE id = ?", (meal_id,))


def list_meals(*, since_day: date | str | None = None, slot: str | None = None, status: str | None = None,
               limit: int = 100, conn: sqlite3.Connection | None = None) -> list[Meal]:
    """Newest first."""
    where, params = ["1=1"], []
    if since_day is not None:
        where.append("day >= ?")
        params.append(_day(since_day))
    if slot is not None:
        where.append("slot = ?")
        params.append(_check(slot, MEAL_SLOTS, "slot"))
    if status is not None:
        where.append("status = ?")
        params.append(_check(status, MEAL_STATUSES, "status"))
    with transaction(conn) as c:
        return _many(c, Meal, f"SELECT * FROM as_meals WHERE {' AND '.join(where)} ORDER BY day DESC, id DESC LIMIT ?",
                     (*params, limit))


# ---------------------------------------------------------------------------
# goals
# ---------------------------------------------------------------------------

_GOAL_COLS = {"title", "metric", "target", "current", "due", "status"}


def add_goal(title: str, *, metric: str | None = None, target: float | None = None, current: float | None = None,
             due: str | None = None, conn: sqlite3.Connection | None = None) -> int:
    now = db.now_iso()
    with transaction(conn) as c:
        return _insert(c, "as_goals", {"title": title, "metric": metric, "target": target, "current": current,
                                       "due": due, "status": "active", "created_at": now, "updated_at": now})


def update_goal(goal_id: int, *, conn: sqlite3.Connection | None = None, **values: Any) -> None:
    if "status" in values:
        _check(values["status"], GOAL_STATUSES, "status")
    with transaction(conn) as c:
        _update(c, "as_goals", goal_id, values, _GOAL_COLS)


def get_goal(goal_id: int, *, conn: sqlite3.Connection | None = None) -> Goal | None:
    with transaction(conn) as c:
        return _one(c, Goal, "SELECT * FROM as_goals WHERE id = ?", (goal_id,))


def list_goals(status: str | None = "active", *, conn: sqlite3.Connection | None = None) -> list[Goal]:
    with transaction(conn) as c:
        if status is None:
            return _many(c, Goal, "SELECT * FROM as_goals ORDER BY id")
        return _many(c, Goal, "SELECT * FROM as_goals WHERE status = ? ORDER BY id",
                     (_check(status, GOAL_STATUSES, "status"),))


# ---------------------------------------------------------------------------
# agent runs (audit) and spend
# ---------------------------------------------------------------------------

def record_agent_run(agent: str, task: str, *, model: str, duration_ms: int, cost_usd: float, status: str,
                     summary: str | None = None, parent_id: int | None = None, tool_calls: Any = None,
                     fake: bool = False, conn: sqlite3.Connection | None = None) -> int:
    _check(status, RUN_STATUSES, "status")
    with transaction(conn) as c:
        return _insert(c, "as_agent_runs", {
            "ts": db.now_iso(), "agent": agent, "task": task[:2000], "model": model, "duration_ms": int(duration_ms),
            "cost_usd": float(cost_usd), "status": status, "summary": (summary or "")[:2000] or None,
            "parent_id": parent_id, "tool_calls": None if tool_calls is None else json.dumps(tool_calls, sort_keys=True),
            "fake": int(fake)})


def finish_agent_run(run_id: int, *, status: str, duration_ms: int, cost_usd: float, summary: str | None = None,
                     tool_calls: Any = None, conn: sqlite3.Connection | None = None) -> None:
    """Close a run opened with ``record_agent_run(status="running")``."""
    _check(status, RUN_STATUSES, "status")
    with transaction(conn) as c:
        _update(c, "as_agent_runs", run_id, {
            "status": status, "duration_ms": int(duration_ms), "cost_usd": float(cost_usd),
            "summary": (summary or "")[:2000] or None,
            "tool_calls": None if tool_calls is None else json.dumps(tool_calls, sort_keys=True)},
            {"status", "duration_ms", "cost_usd", "summary", "tool_calls"}, touch=False)


def get_agent_run(run_id: int, *, conn: sqlite3.Connection | None = None) -> AgentRun | None:
    with transaction(conn) as c:
        return _one(c, AgentRun, "SELECT * FROM as_agent_runs WHERE id = ?", (run_id,))


def list_agent_runs(limit: int = 50, *, agent: str | None = None, since: str | None = None,
                    conn: sqlite3.Connection | None = None) -> list[AgentRun]:
    """Newest first."""
    where, params = ["1=1"], []
    if agent is not None:
        where.append("agent = ?")
        params.append(agent)
    if since is not None:
        where.append("ts >= ?")
        params.append(since)
    with transaction(conn) as c:
        return _many(c, AgentRun, f"SELECT * FROM as_agent_runs WHERE {' AND '.join(where)} "
                                  "ORDER BY ts DESC, id DESC LIMIT ?", (*params, limit))


def assistant_spend_between(start_iso: str, end_iso: str, *, conn: sqlite3.Connection | None = None) -> float:
    """Assistant LLM spend (``llm_usage`` rows with purpose ``assistant.*``) in [start, end)."""
    return db.llm_spend_between(start_iso, end_iso, scope="assistant", conn=conn)


def assistant_spent_today(day: date | None = None) -> float:
    start, end = db.local_day_bounds(day)
    return assistant_spend_between(start, end)


# ---------------------------------------------------------------------------
# lessons (self-improvement memory)
# ---------------------------------------------------------------------------

def add_lesson(area: str, lesson: str, *, evidence: str | None = None, status: str = "proposed",
               conn: sqlite3.Connection | None = None) -> int:
    _check(status, LESSON_STATUSES, "status")
    now = db.now_iso()
    with transaction(conn) as c:
        return _insert(c, "as_lessons", {"ts": now, "area": area, "lesson": lesson, "evidence": evidence,
                                         "status": status, "updated_at": now})


def set_lesson_status(lesson_id: int, status: str, *, conn: sqlite3.Connection | None = None) -> None:
    _check(status, LESSON_STATUSES, "status")
    with transaction(conn) as c:
        _update(c, "as_lessons", lesson_id, {"status": status}, {"status"})


def list_lessons(status: str | None = None, *, area: str | None = None, limit: int = 200,
                 conn: sqlite3.Connection | None = None) -> list[Lesson]:
    """Newest first."""
    where, params = ["1=1"], []
    if status is not None:
        where.append("status = ?")
        params.append(_check(status, LESSON_STATUSES, "status"))
    if area is not None:
        where.append("area = ?")
        params.append(area)
    with transaction(conn) as c:
        return _many(c, Lesson, f"SELECT * FROM as_lessons WHERE {' AND '.join(where)} ORDER BY ts DESC, id DESC LIMIT ?",
                     (*params, limit))
