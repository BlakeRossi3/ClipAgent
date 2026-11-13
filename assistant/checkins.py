"""The proactive side: a small declarative routine, sent politely, answered in plain code.

Routine (editable by the owner in the vault note ``Agent/Routine.md``, section ``## Schedule``;
one line per kind, local time, ``off`` disables; kinds he deletes keep their defaults)::

    - morning_brief: 08:05 daily        yesterday's Shorts, money this week, agenda, a breakfast idea
    - dinner_idea: 16:45 weekdays       one dinner suggestion (only once his tastes are known)
    - weekly_hours: 18:40 fri           "How many hours did you work this week, sir?"
    - wellbeing: 19:15 tue,thu          a light "how are you holding up?" (never re-asked)
    - evening_wrap: 21:05 daily         the day in one text + a journal summary
    - weekly_review: 18:20 sun          the week in numbers (briefer agent when live)
    - onboarding: 12:10 daily           one profile gap per day (pay rate first), until none are left
    - lunch_idea / breakfast_idea: off

Core behaviour:

* :func:`plan_day` materialises the day's ``as_checkins`` rows (idempotent per day + kind).
* :func:`send_due` sends what is due through ``tools.send_to_owner``: never in quiet hours or while
  snoozed/stopped; stale items are skipped, not sent late; one open question at a time; when the
  daily SMS cap is tight, questions awaiting data win over briefs, briefs over nudges (slots are
  held back for higher-priority items planned later the same day). Answers to his texts are
  replies and never compete for the cap. Unanswered questions are re-asked at most once, then expire.
* :func:`handle_reply` answers an inbound text in plain code when it plausibly answers the open
  check-in (hours -> ledger, meal feedback -> meals, wellbeing -> journal, onboarding answers ->
  ledger / vault) or is a control word (``SNOOZE [2h]``, ``QUIET`` (rest of today),
  ``STOP CHECK-INS``, ``START CHECK-INS``); otherwise it returns False and the message goes to
  the conversational agent.

Message-type check-ins (briefs, meal ideas) are marked ``answered`` with ``parsed={"delivered": true}``
once sent (meal ideas stay ``pending`` for a few hours so "had it" can be recorded). Everything sent
or answered is also written to the day's journal note.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Callable

import db
from assistant import store
from assistant.settings import get_assistant_settings

MODULE = "assistant"
ROUTINE_NOTE = "Agent/Routine.md"
SCHEDULE_HEADING = "Schedule"
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class KindSpec:
    type: str                         # question | brief | nudge
    lateness: timedelta               # skip (do not send) when this late
    reask_after: timedelta | None = None
    expire_after: timedelta | None = None

    @property
    def priority(self) -> int:
        return {"question": 1, "brief": 2, "nudge": 3}[self.type]


KINDS: dict[str, KindSpec] = {
    "weekly_hours": KindSpec("question", timedelta(hours=14), timedelta(hours=15), timedelta(hours=40)),
    "onboarding": KindSpec("question", timedelta(hours=6), None, timedelta(hours=20)),
    "morning_brief": KindSpec("brief", timedelta(hours=3)),
    "evening_wrap": KindSpec("brief", timedelta(hours=1, minutes=30)),
    "weekly_review": KindSpec("brief", timedelta(hours=4)),
    "wellbeing": KindSpec("nudge", timedelta(hours=2), None, timedelta(hours=8)),
    "breakfast_idea": KindSpec("nudge", timedelta(hours=1, minutes=30), None, timedelta(hours=5)),
    "lunch_idea": KindSpec("nudge", timedelta(hours=1, minutes=30), None, timedelta(hours=5)),
    "dinner_idea": KindSpec("nudge", timedelta(hours=1, minutes=30), None, timedelta(hours=5)),
}
_OTHER = KindSpec("question", timedelta(hours=6), None, timedelta(hours=24))  # agent-scheduled reminders etc.

DEFAULT_ROUTINE: dict[str, str] = {
    "morning_brief": "08:05 daily",
    "onboarding": "12:10 daily",
    "breakfast_idea": "off",
    "lunch_idea": "off",
    "dinner_idea": "16:45 weekdays",
    "weekly_hours": "18:40 fri",
    "wellbeing": "19:15 tue,thu",
    "weekly_review": "18:20 sun",
    "evening_wrap": "21:05 daily",
}


def spec_for(kind: str) -> KindSpec:
    return KINDS.get(kind.split(":", 1)[0], _OTHER)


# ---------------------------------------------------------------------------
# routine
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RoutineItem:
    kind: str
    at: dtime | None                  # None = off
    days: frozenset[int]              # 0 = Monday

    def runs_on(self, day: date) -> bool:
        return self.at is not None and day.weekday() in self.days


def parse_days(text: str) -> frozenset[int]:
    t = (text or "").strip().lower().replace(" ", "")
    if t in ("", "daily", "everyday", "all"):
        return frozenset(range(7))
    if t == "weekdays":
        return frozenset(range(5))
    if t == "weekends":
        return frozenset({5, 6})
    out: set[int] = set()
    for part in t.split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            ia, ib = DAY_NAMES.index(a[:3]), DAY_NAMES.index(b[:3])
            out.update(range(ia, ib + 1) if ia <= ib else [*range(ia, 7), *range(0, ib + 1)])
        elif part:
            out.add(DAY_NAMES.index(part[:3]))
    return frozenset(out)


def parse_item(kind: str, value: str) -> RoutineItem:
    """``value`` like '08:05 daily', '18:40 fri', '19:15 tue,thu' or 'off'. Raises ValueError."""
    v = value.strip().lower()
    if v.startswith("off") or not v:
        return RoutineItem(kind, None, frozenset())
    m = re.fullmatch(r"(\d{1,2}):(\d{2})\s*(.*)", v)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"bad routine entry for {kind}: {value!r}")
    return RoutineItem(kind, dtime(int(m.group(1)), int(m.group(2))), parse_days(m.group(3)))


def routine_note_text() -> str:
    lines = ["# Routine", "",
             "When I check in with you. Edit freely: one line per kind, local time, then days",
             "(daily, weekdays, weekends, fri, tue,thu, mon-fri). Write `off` to stop one.",
             "Changes apply from the next day. Text SNOOZE, QUIET (rest of today), STOP CHECK-INS",
             "or START CHECK-INS at any time.", "", f"## {SCHEDULE_HEADING}", ""]
    lines += [f"- {k}: {v}" for k, v in DEFAULT_ROUTINE.items()]
    return "\n".join(lines) + "\n"


def ensure_routine_note() -> str:
    """Create ``Agent/Routine.md`` with the defaults if it does not exist (never overwrites)."""
    from assistant import vault

    vault.ensure_vault()
    if not vault.note_exists(ROUTINE_NOTE):
        vault.write_note(ROUTINE_NOTE, routine_note_text(), meta=None, source="assistant", note_type="agent")
    return ROUTINE_NOTE


def load_routine() -> dict[str, RoutineItem]:
    """Defaults, overridden by the lines in the vault note (bad lines are logged and ignored)."""
    items = {k: parse_item(k, v) for k, v in DEFAULT_ROUTINE.items()}
    try:
        from assistant import vault

        ensure_routine_note()
        body = vault.read_note(ROUTINE_NOTE).body
    except Exception as exc:
        db.log_event(MODULE, "routine note unreadable; using defaults", level="warning",
                     data={"error": repr(exc)[:200]})
        return items
    for ln in body.splitlines():
        m = re.match(r"^\s*[-*]\s*`?([a-z_]+)`?\s*:\s*(.+?)\s*$", ln)
        if not m or m.group(1) not in DEFAULT_ROUTINE:
            continue
        try:
            items[m.group(1)] = parse_item(m.group(1), m.group(2).strip("` "))
        except ValueError as exc:
            db.log_event(MODULE, "routine line ignored", level="warning", data={"line": ln[:120], "error": str(exc)})
    return items


# ---------------------------------------------------------------------------
# onboarding: generic gaps, asked one per day (pay rate first)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Gap:
    key: str
    question: str
    open: Callable[[], bool]


def _rated_jobs() -> list[store.Job]:
    return [j for j in store.list_jobs() if j.hourly_rate > 0]


def _gaps() -> list[Gap]:
    from assistant import meals

    return [
        Gap("pay_rate", "What is your hourly pay at work, {h}, before tax? I will use it to keep your earnings "
                        "up to date.", lambda: not _rated_jobs()),
        Gap("tax", "Roughly what share of your pay should I set aside for tax, {h}? A rough percentage is fine.",
            lambda: bool(_rated_jobs()) and all(j.est_tax_rate <= 0 for j in _rated_jobs())),
        Gap("pay_schedule", "Are you paid weekly, every two weeks, twice a month or monthly, {h}?",
            lambda: bool(_rated_jobs())),
        Gap("meals", meals.PREFS_QUESTION, lambda: not meals.preferences()),
        Gap("goal", "Is there a savings or income goal you would like me to track, {h}? An amount and a date "
                    "will do, or say no.", lambda: not store.list_goals("active")),
    ]


def _open_questions() -> list[Gap]:
    """Owner-specific gaps listed (as fact bullets) in the vault's ``Profile/Open Questions.md``."""
    try:
        from assistant import vault

        rel = vault.AREAS["open_questions"][0]
        facts = vault.facts_in(vault.read_note(rel).body) if vault.note_exists(rel) else []
    except Exception:
        return []
    out = []
    for f in facts:
        key = "open:" + hashlib.sha1(f.encode()).hexdigest()[:10]
        q = f if f.rstrip().endswith("?") else f"{f.rstrip('.')}?"
        out.append(Gap(key, "A question for you, {h}: " + q, lambda: True))
    return out


def next_gap() -> Gap | None:
    for g in _gaps() + _open_questions():
        if db.kv_get(f"checkins:onboard_done:{g.key}"):
            continue
        if int(db.kv_get(f"checkins:onboard:{g.key}", 0) or 0) >= 2:   # asked twice: let it rest
            continue
        try:
            if g.open():
                return g
        except Exception:
            continue
    return None


def _onboard_done(key: str) -> None:
    db.kv_set(f"checkins:onboard_done:{key}", db.now_iso())


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------

def _stopped() -> bool:
    return db.kv_get("checkins:stopped") is not None


def _local_dt(day: date, at: dtime) -> datetime:
    return datetime.combine(day, at, tzinfo=db.local_tz())


def _hours_question(h: str) -> str:
    jobs = store.list_jobs()
    if len(jobs) > 1:
        names = " and at ".join(j.name for j in jobs)
        return f"How many hours did you work at {names} this week, {h}?"
    return f"How many hours did you work this week, {h}?"


_WELLBEING = ("How are you holding up this week, {h}?", "How are you feeling today, {h}? No need for detail.",
              "Anything on your mind I can take off your plate, {h}?")


def plan_day(day: date | None = None) -> list[int]:
    """Create the day's check-ins from the routine (idempotent). Returns new check-in ids."""
    d = day or db.local_today()
    if _stopped():
        return []
    h = get_assistant_settings().owner_honorific
    gentle = db.kv_get(f"checkins:gentle:{d.isoformat()}") is not None
    now = db.utcnow()
    created: list[int] = []
    for item in load_routine().values():
        if not item.runs_on(d) or item.at is None:
            continue
        spec = spec_for(item.kind)
        when = _local_dt(d, item.at)
        if when + spec.lateness < now:             # e.g. planned at startup in the afternoon
            continue
        if gentle and spec.type == "nudge":
            continue
        kind, question = item.kind, f"({item.kind.replace('_', ' ')})"
        if item.kind == "weekly_hours":
            question = _hours_question(h)
        elif item.kind == "wellbeing":
            recent = [c for c in store.list_checkins(kind="wellbeing", limit=500)
                      if c.sent_at and db.parse_iso(c.sent_at) > now - timedelta(days=2)]
            if recent:
                continue
            question = _WELLBEING[d.toordinal() % len(_WELLBEING)].format(h=h)
        elif item.kind == "onboarding":
            if gentle:
                continue
            gap = next_gap()
            if gap is None:
                continue
            kind, question = f"onboarding:{gap.key}", gap.question.format(h=h)
        if not db.kv_set_if_absent(f"checkins:planned:{d.isoformat()}:{item.kind}", db.now_iso()):
            continue
        if kind.startswith("onboarding:"):
            db.kv_incr(f"checkins:onboard:{kind.split(':', 1)[1]}")
        created.append(store.add_checkin(kind, question, db.to_iso(when)))
    if created:
        db.log_event(MODULE, f"planned {len(created)} check-ins for {d.isoformat()}", data={"ids": created})
    return created


def planned(day: date | None = None) -> list[store.Checkin]:
    """Check-ins scheduled on a local day (any status), in time order."""
    start, end = db.local_day_bounds(day)
    return [c for c in store.list_checkins(limit=5000) if start <= c.scheduled_for < end]


# ---------------------------------------------------------------------------
# composing messages (plain code: cheap and predictable)
# ---------------------------------------------------------------------------

def _fit(parts: list[str], limit: int = 320) -> str:
    out: list[str] = []
    for p in parts:
        if p and len(" ".join(out + [p])) <= limit:
            out.append(p)
    return " ".join(out)


def _shorts_line(day: date, *, label: str) -> str:
    posted = db.count_uploads_on(day)
    views = sum(s.views for s in db.stats_between(day - timedelta(days=7), day))
    bits = [f"Shorts: {posted} posted {label}"]
    if views:
        bits.append(f"{views:,} views over the past week")
    return ", ".join(bits) + "."


def _agenda(today: date) -> list[str]:
    out = []
    for g in store.list_goals("active"):
        try:
            due = date.fromisoformat((g.due or "")[:10])
        except ValueError:
            continue
        if today <= due <= today + timedelta(days=7):
            out.append(f"Due {'today' if due == today else due.strftime('%a')}: {g.title}.")
    try:
        from assistant import vault

        rel = vault.journal_path(today)
        if vault.note_exists(rel):
            body = vault.read_note(rel).body
            sec = re.search(r"^## Agenda\s*$(.*?)(?=^## |\Z)", body, re.M | re.S)
            if sec:
                out += [ln.strip("-* ").strip() for ln in sec.group(1).splitlines() if ln.strip().startswith(("-", "*"))][:2]
    except Exception:
        pass
    return out


def compose_morning_brief(h: str) -> str:
    from assistant import ledger, meals

    today = db.local_today()
    parts = [f"Good morning, {h}."]
    pending = db.list_decisions("pending", limit=5)
    if pending:
        parts.append("Waiting on you: " + ", ".join(f"approval #{d.id}" for d in pending[:3]) + ".")
    parts += _agenda(today)
    parts.append(_shorts_line(today - timedelta(days=1), label="yesterday"))
    parts.append(ledger.brief_line())
    try:
        idea = meals.suggest(today, slots=("breakfast",))
    except Exception:
        idea = []
    if idea:
        parts.append(f"Breakfast idea: {idea[0]['suggestion']}.")
    return _fit(parts)


def compose_evening_wrap(h: str) -> str:
    from assistant import ledger, vault

    today = db.local_today()
    posted = db.count_uploads_on(today)
    money = ledger.brief_line()
    parts = [f"Evening, {h}."]
    if posted:
        parts.append(f"{posted} Short{'s' if posted != 1 else ''} went out today.")
    if money:
        parts.append(money)
    nxt = _agenda(today + timedelta(days=1))
    if nxt:
        parts.append("Tomorrow: " + nxt[0])
    if len(parts) == 1:
        parts = [f"A quiet day, {h}. Nothing needs you tonight."]
    text = _fit(parts)
    try:
        vault.append_journal(f"Evening wrap: {text}", source="checkins")
    except Exception:
        pass
    return text


def compose_weekly_review(h: str) -> str:
    from assistant import ledger

    s = ledger.summary(weeks=2)
    lw = s["this_week"]
    today = db.local_today()
    views = sum(x.views for x in db.stats_between(today - timedelta(days=7), today))
    posted = sum(db.count_uploads_on(today - timedelta(days=k)) for k in range(7))
    parts = [f"Your week, {h}:"]
    if s["jobs"]:
        parts.append(f"{ledger.fmt_hours(lw['hours'])} hours worked, {ledger.fmt_usd(lw['job_gross'])} from work.")
    parts.append(f"Shorts: {posted} posted, {views:,} views, AI costs {ledger.fmt_usd(lw['ai_costs']['total'], whole=False)}.")
    if s["missing"]:
        parts.append("Still missing: " + s["missing"][0] + ".")
    plain = _fit(parts)
    if get_assistant_settings().fake:
        return plain
    from assistant import runtime

    res = runtime.run_task("briefer", "Write the Sunday weekly review following the weekly-review skill, in one "
                           "SMS. These figures are already computed; use them:\n" + plain, channel="sms",
                           kind="weekly_review")
    return res.text if res.status == "ok" and len(res.text) > 20 else plain


def compose_meal_idea(slot: str, h: str) -> str | None:
    from assistant import meals

    idea = meals.suggest(slots=(slot,))
    if not idea:
        return None
    return f"A thought for {slot}, {h}: {idea[0]['suggestion']}. Tell me if you have it."


def compose(chk: store.Checkin) -> str | None:
    h = get_assistant_settings().owner_honorific
    base = chk.kind.split(":", 1)[0]
    if base == "morning_brief":
        return compose_morning_brief(h)
    if base == "evening_wrap":
        return compose_evening_wrap(h)
    if base == "weekly_review":
        return compose_weekly_review(h)
    if base.endswith("_idea"):
        return compose_meal_idea(base.removesuffix("_idea"), h)
    return chk.question


def reask_text(chk: store.Checkin) -> str:
    h = get_assistant_settings().owner_honorific
    if chk.kind == "weekly_hours":
        return f"When you have a moment, {h}: roughly how many hours did you work last week?"
    return f"When you have a moment, {h}: {chk.question[0].lower() + chk.question[1:]}"


# ---------------------------------------------------------------------------
# sending
# ---------------------------------------------------------------------------

def _journal(text: str) -> None:
    try:
        from assistant import vault

        vault.ensure_vault()
        vault.append_journal(text, source="checkins")
    except Exception as exc:  # the journal is a record, not a dependency
        db.log_event(MODULE, "journal write failed", level="warning", data={"error": repr(exc)[:200]})


def _snoozed_until() -> datetime | None:
    v = db.kv_get("checkins:snooze_until")
    if not v:
        return None
    until = db.parse_iso(v)
    return until if until > db.utcnow() else None


def _deliver(chk: store.Checkin, text: str, *, reask: bool = False) -> str:
    """Send one text; returns 'sent' | 'dry_run' | 'queued' | 'skipped'."""
    from assistant import tools

    base = chk.kind.split(":", 1)[0]
    try:
        res = tools.send_to_owner(text, kind=f"checkin_{base}", agent="checkins")
    except tools.ToolError as exc:  # the assistant's own daily cap
        db.log_event(MODULE, f"check-in #{chk.id} not sent: {exc}", level="info")
        return "skipped"
    if res["status"] in ("skipped", "failed"):  # global notification cap / Twilio failure
        return "skipped"
    if not reask:
        store.mark_checkin_sent(chk.id, message_id=res["message_id"])
        if spec_for(chk.kind).type == "brief":
            store.answer_checkin(chk.id, "", {"delivered": True})
    _journal(("Re-asked" if reask else "Sent") + f" {chk.kind.replace('_', ' ')}: {text}")
    return res["status"]


def send_due(now: datetime | None = None) -> dict[str, int]:
    """Send due check-ins (run every few minutes). Returns counts by outcome."""
    from assistant import tools

    out: dict[str, int] = {"sent": 0, "skipped": 0, "expired": 0, "reasked": 0, "waiting": 0}
    s = get_assistant_settings()
    if not s.enabled or _stopped():
        return out
    import notify

    now = now or db.utcnow()
    now_iso = db.to_iso(now)
    quiet = notify.in_quiet_hours() or _snoozed_until() is not None

    candidates: list[tuple[int, str, store.Checkin, bool]] = []
    open_questions: list[store.Checkin] = []
    for chk in store.list_checkins("pending", limit=5000):
        if not chk.sent_at:
            continue
        spec, age = spec_for(chk.kind), now - db.parse_iso(chk.sent_at)
        if spec.expire_after and age >= spec.expire_after:
            store.set_checkin_status(chk.id, "expired")
            out["expired"] += 1
            if spec.type != "nudge":
                _journal(f"No answer to: {chk.question}")
            continue
        if spec.type != "nudge":
            open_questions.append(chk)
        if spec.reask_after and age >= spec.reask_after and not db.kv_get(f"checkins:reasked:{chk.id}"):
            candidates.append((spec.priority, chk.scheduled_for, chk, True))

    for chk in store.due_checkins(now_iso):
        if now - db.parse_iso(chk.scheduled_for) > spec_for(chk.kind).lateness:
            store.set_checkin_status(chk.id, "skipped")
            out["skipped"] += 1
            db.log_event(MODULE, f"check-in #{chk.id} ({chk.kind}) skipped: too late to be useful")
            continue
        candidates.append((spec_for(chk.kind).priority, chk.scheduled_for, chk, False))
    if quiet or not candidates:
        out["waiting"] += len(candidates)
        return out

    day_end = db.local_day_bounds()[1]
    later = [c for c in store.list_checkins("pending", limit=5000)
             if not c.sent_at and now_iso < c.scheduled_for < day_end]
    remaining = s.max_sms_per_day - tools.outbound_sms_today()
    for prio, _, chk, reask in sorted(candidates, key=lambda t: (t[0], t[1], t[2].id)):
        spec = spec_for(chk.kind)
        reserved = sum(1 for c in later if spec_for(c.kind).priority < prio)
        if remaining - reserved <= 0:
            if not reask:
                store.set_checkin_status(chk.id, "skipped")
            db.log_event(MODULE, f"check-in #{chk.id} ({chk.kind}) skipped: daily message cap",
                         data={"remaining": remaining, "reserved": reserved})
            out["skipped"] += 1
            continue
        if not reask and spec.type != "brief" and any(q.id != chk.id for q in open_questions):
            out["waiting"] += 1      # one open question at a time; it is retried until stale
            continue
        text = reask_text(chk) if reask else compose(chk)
        if not text:
            store.set_checkin_status(chk.id, "skipped")
            out["skipped"] += 1
            continue
        status = _deliver(chk, text, reask=reask)
        if reask:
            db.kv_set(f"checkins:reasked:{chk.id}", now_iso)
        if status == "skipped":
            if not reask:
                store.set_checkin_status(chk.id, "skipped")
            out["skipped"] += 1
            continue
        remaining -= 1
        out["reasked" if reask else "sent"] += 1
        if spec.type != "brief" and not reask:
            open_questions.append(chk)
    return out


# ---------------------------------------------------------------------------
# inbound replies
# ---------------------------------------------------------------------------

_CONTROL = re.compile(r"^\s*(snooze|quiet|stop check-?ins?|start check-?ins?|check-?ins? (?:off|on)|"
                      r"resume check-?ins?)\b\s*(.*)$", re.I)
_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*(m|min|mins|minutes?|h|hr|hrs|hours?)\b", re.I)
_NEGATIVE = re.compile(r"\b(stress\w*|tired|exhausted|sick|ill|rough|awful|terrible|bad|down|anxious|"
                       r"overwhelmed|not (?:great|good|well)|struggling|burn(?:ed|t)? out)\b", re.I)
_NO = re.compile(r"^\s*(no|nope|nah|none|not now|not really|no thanks|maybe later|skip)\b", re.I)


def _reply(text: str, in_id: int) -> None:
    from assistant import tools

    try:
        tools.send_to_owner(text, kind="reply", agent="checkins", reply_to=in_id)
    except tools.ToolError as exc:
        db.log_event(MODULE, "check-in reply not delivered", level="warning", data={"error": str(exc)})


def _skip_unsent_today() -> int:
    start, end = db.local_day_bounds()
    n = 0
    for c in store.list_checkins("pending", limit=5000):
        if not c.sent_at and c.scheduled_for < end:
            store.set_checkin_status(c.id, "skipped")
            n += 1
    return n


def _control(word: str, rest: str, h: str) -> str:
    w = word.lower().replace("-", "")
    if w == "snooze":
        m = _DURATION.search(rest or "")
        delta = timedelta(hours=3)
        if m:
            n = float(m.group(1))
            delta = timedelta(minutes=n) if m.group(2).lower().startswith("m") else timedelta(hours=n)
        delta = min(delta, timedelta(hours=24))
        until = db.utcnow() + delta
        db.kv_set("checkins:snooze_until", db.to_iso(until))
        return f"Snoozed until {until.astimezone(db.local_tz()).strftime('%H:%M')}, {h}."
    if w == "quiet":
        db.kv_set(f"checkins:quiet:{db.local_today().isoformat()}", db.now_iso())
        _skip_unsent_today()
        return f"Understood, {h}. Nothing more from me today unless you write."
    if w.startswith("stop") or w.endswith("off"):
        db.kv_set("checkins:stopped", db.now_iso())
        n = _skip_unsent_today()
        db.log_event(MODULE, "check-ins stopped by the owner", data={"skipped": n})
        return f"Check-ins are off, {h}. Text START CHECK-INS to bring them back."
    db.kv_delete("checkins:stopped")
    db.kv_delete("checkins:snooze_until")
    db.log_event(MODULE, "check-ins resumed by the owner")
    return f"Check-ins are back on, {h}."


def _money(text: str) -> float | None:
    from assistant.ledger import _normalise

    t = _normalise(text).replace(",", "")
    nums = re.findall(r"\$?\s*(\d+(?:\.\d{1,2})?)\s*(k)?\b", t)
    vals = {float(n) * (1000 if k else 1) for n, k in nums}
    return vals.pop() if len(vals) == 1 else None


def _percent(text: str) -> float | None:
    t = text.lower()
    if re.search(r"\b(none|nothing|zero|no tax)\b", t):
        return 0.0
    from assistant.ledger import _normalise

    nums = {float(n) for n in re.findall(r"(\d+(?:\.\d+)?)", _normalise(t))}
    if len(nums) != 1:
        return None
    v = nums.pop()
    v = v / 100 if v >= 1 else v
    return v if 0 <= v <= 0.6 else None


def _schedule(text: str) -> str | None:
    t = text.lower()
    for rx, val in ((r"every (?:two|2|other) weeks?|bi-?weekly|fortnight", "biweekly"),
                    (r"twice a month|semi-?monthly|1st and (?:the )?15th|15th and", "semimonthly"),
                    (r"\bweekly\b|every week|each week|once a week", "weekly"),
                    (r"monthly|once a month|every month", "monthly")):
        if re.search(rx, t):
            return val
    return None


def _answer(chk: store.Checkin, text: str, h: str) -> str | None:
    """Plain-code handling of an answer to ``chk``; None = not plausibly an answer."""
    from assistant import ledger, meals

    base, _, key = chk.kind.partition(":")
    if base == "weekly_hours":
        week = db.local_date_of(chk.scheduled_for)
        hours = ledger.parse_hours(text, last_week=ledger.last_week_hours(before=week))
        if hours is None or len(store.list_jobs()) > 1:
            return None
        res = ledger.log_hours(hours, week=week, source="sms", note=f"his words: {text.strip()[:120]}")
        store.answer_checkin(chk.id, text, {"hours": hours, "gross": res["gross"]})
        _journal(f"Hours for week of {res['week_start']}: {ledger.fmt_hours(hours)}"
                 + (f" ({ledger.fmt_usd(res['gross'], whole=False)} gross)" if res["gross"] is not None else ""))
        return ledger.confirmation(res, h)
    if base == "onboarding":
        return _answer_onboarding(chk, key, text, h)
    if base == "wellbeing":
        store.answer_checkin(chk.id, text, {"negative": bool(_NEGATIVE.search(text))})
        _journal(f"How he is doing: \"{text.strip()[:300]}\"")
        if _NEGATIVE.search(text):
            db.kv_set(f"checkins:gentle:{(db.local_today() + timedelta(days=1)).isoformat()}", db.now_iso())
            return f"Sorry to hear it, {h}. I will keep things light tomorrow."
        return f"Thank you, {h}. Noted."
    if base.endswith("_idea"):
        status = meals.infer_status(text)
        if status is None:
            return None
        meals.record_feedback(text, status=status)
        store.answer_checkin(chk.id, text, {"status": status})
        return {"eaten": f"Good to hear, {h}. Noted.", "accepted": f"Very good, {h}.",
                "declined": f"Noted, {h}. I will suggest something different next time."}[status]
    return None


def _answer_onboarding(chk: store.Checkin, key: str, text: str, h: str) -> str | None:
    from assistant import ledger, meals, vault

    no = bool(_NO.match(text))
    if key == "pay_rate":
        rate = _money(text)
        if rate is None or not 1 <= rate <= 1000:
            return None
        job = ledger.set_job((store.list_jobs() or [None])[0].name if store.list_jobs() else ledger.DEFAULT_JOB_NAME,
                             hourly_rate=rate)
        reply = f"Noted, {h}: {ledger.fmt_usd(rate, whole=False)} an hour. I will use it for your weekly earnings."
        parsed: Any = {"hourly_rate": rate, "job": job.name}
    elif key == "tax":
        pct = _percent(text)
        if pct is None:
            return None
        for j in _rated_jobs():
            ledger.set_job(j.name, est_tax_rate=pct)
        reply = f"Noted, {h}: I will set aside about {round(pct * 100)} percent for tax in the estimates."
        parsed = {"est_tax_rate": pct}
    elif key == "pay_schedule":
        sched = _schedule(text)
        if sched is None:
            return None
        for j in _rated_jobs():
            ledger.set_job(j.name, pay_schedule=sched)
        reply, parsed = f"Noted, {h}: paid {sched}.", {"pay_schedule": sched}
    elif key == "meals":
        if no and len(text.split()) <= 3:
            reply, parsed = f"Very good, {h}. I will keep suggestions simple.", {"declined": True}
            vault.remember("No particular meal preferences given when asked", area="meals", source="sms")
        else:
            meals.record_preferences(text)
            reply, parsed = f"Thank you, {h}. I will keep that in mind for meal ideas.", {"recorded": True}
    elif key == "goal":
        if no:
            reply, parsed = f"Very good, {h}. Say the word if that changes.", {"declined": True}
        else:
            amount = _money(text)
            if amount is None:
                return None
            gid = store.add_goal(f"Savings goal: {' '.join(text.split())[:100]}", metric="savings", target=amount)
            reply, parsed = (f"Noted, {h}: a goal of {ledger.fmt_usd(amount)}. I will track it.",
                             {"goal_id": gid, "target": amount})
    elif key.startswith("open:"):
        q = chk.question.split(": ", 1)[-1]
        vault.remember(f"{q.rstrip('?')}: {' '.join(text.split())}", area="about", source="sms")
        try:
            vault.forget(q.rstrip("?"), area="open_questions")
        except Exception:
            pass
        reply, parsed = f"Thank you, {h}. Noted.", {"remembered": True}
    else:
        return None
    _onboard_done(key)
    store.answer_checkin(chk.id, text, parsed)
    _journal(f"Answered onboarding question ({key}): \"{text.strip()[:200]}\"")
    return reply


def handle_reply(text: str) -> bool:
    """Handle an inbound owner text in plain code if it is a control word or plausibly answers
    the open check-in. Returns False to hand it to the conversational agent."""
    body = (text or "").strip()
    if not body:
        return False
    h = get_assistant_settings().owner_honorific
    m = _CONTROL.match(body)
    if m and len(body) <= 40:
        in_id = store.add_message("in", "sms", body, kind="checkin_control")
        _reply(_control(m.group(1), m.group(2), h), in_id)
        return True
    chk = store.open_checkin()
    if chk is None or "?" in body or len(body) > 240:
        return False
    try:
        reply = _answer(chk, body, h)
    except Exception as exc:  # never lose his message: the agent gets it instead
        db.log_event(MODULE, f"check-in #{chk.id} answer handling failed", level="error",
                     data={"error": repr(exc)[:300]})
        return False
    if reply is None:
        return False
    in_id = store.add_message("in", "sms", body, kind="checkin_answer", meta={"checkin_id": chk.id})
    _reply(reply, in_id)
    db.log_event(MODULE, f"check-in #{chk.id} ({chk.kind}) answered")
    return True


def status_lines(day: date | None = None) -> list[str]:
    """Human-readable plan for a day (CLI ``assistant plan``)."""
    tz = db.local_tz()
    out = []
    for c in planned(day):
        when = db.parse_iso(c.scheduled_for).astimezone(tz).strftime("%H:%M")
        sent = f" sent {db.parse_iso(c.sent_at).astimezone(tz):%H:%M}" if c.sent_at else ""
        out.append(f"  #{c.id:<4} {when}  {c.kind:<22} {c.status:<8}{sent}  {c.question[:70]}")
    flags = []
    if _stopped():
        flags.append("check-ins STOPPED (text START CHECK-INS)")
    if _snoozed_until():
        flags.append(f"snoozed until {_snoozed_until().astimezone(tz):%H:%M}")  # type: ignore[union-attr]
    return flags + (out or ["  nothing planned"])
