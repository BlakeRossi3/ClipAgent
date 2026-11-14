"""The owner's money picture, in plain code (no LLM).

* Jobs: hourly rate, pay schedule, estimated tax set-aside (``store.as_jobs``).
* :func:`log_hours` records a week's hours for a job (upsert per job + local Monday) and writes
  the matching ``as_income`` row (source ``job``, ref ``hours:<job_id>:<week>``, dated the week's
  Monday) so a correction replaces the old figure instead of adding to it. :func:`log_days` takes
  hours per day ("Monday 9, Tuesday 8") for any past week, merges them with days already logged and
  keeps the week's total = their sum. :func:`parse_week` reads "last week" / "week of November 2"
  and refuses when the two disagree (ask him once).
* :func:`sync_job_income` repairs pay on read: every logged week of a job with a rate has its job
  income row at hours x rate (weeks logged before the rate was known, or before a rate change).
* Reels: YouTube ``stats.estimated_revenue`` (phase 1) minus all LLM spend (pipeline + assistant)
  = reels profit. Revenue is NULL until the monetary Analytics scope is granted.
* :func:`summary` - this week, last week, month and year to date, weekly series for charts,
  hours trend, effective hourly rate, goal progress and a ``missing`` list so callers ask
  rather than assume.

Money is :class:`~decimal.Decimal` internally (rounded to cents only for output); hours are
Decimal too. Week = local Monday..Sunday; job income is attributed to the week's Monday.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable

import db
from assistant import store

CENT = Decimal("0.01")
MAX_WEEK_HOURS = 100
DEFAULT_JOB_NAME = "Job"


class LedgerError(ValueError):
    """A request the ledger cannot honour as given (the message is safe to show)."""


def _dec(x: Any) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x if x is not None else 0))


def _cents(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def _f(x: Decimal) -> float:
    return float(_cents(x))


def fmt_usd(amount: Any, *, whole: bool | None = None) -> str:
    """'$1,234' (whole dollars from $100 up, unless ``whole`` says otherwise) or '$42.50'."""
    d = _dec(amount)
    if whole is None:
        whole = abs(d) >= 100
    sign = "-" if d < 0 else ""
    d = abs(d)
    return f"{sign}${d.quantize(Decimal(1), rounding=ROUND_HALF_UP):,}" if whole else f"{sign}${_cents(d):,}"


def fmt_hours(h: Any) -> str:
    d = _dec(h).normalize()
    return f"{d:f}" if d != d.to_integral() else str(int(d))


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------

def set_job(name: str = DEFAULT_JOB_NAME, *, hourly_rate: float | None = None, pay_schedule: str | None = None,
            est_tax_rate: float | None = None) -> store.Job:
    """Create or update a job, keeping fields that are not given."""
    name = " ".join((name or DEFAULT_JOB_NAME).split())
    cur = store.get_job_by_name(name)
    if hourly_rate is not None and not 0 <= float(hourly_rate) <= 10_000:
        raise LedgerError("hourly rate must be between 0 and 10,000")
    if est_tax_rate is not None and est_tax_rate >= 1:
        est_tax_rate = est_tax_rate / 100  # "22" means 22 %
    jid = store.upsert_job(cur.name if cur else name,
                           hourly_rate=float(hourly_rate if hourly_rate is not None else (cur.hourly_rate if cur else 0)),
                           pay_schedule=pay_schedule or (cur.pay_schedule if cur else "biweekly"),
                           est_tax_rate=float(est_tax_rate if est_tax_rate is not None
                                              else (cur.est_tax_rate if cur else 0.0)))
    job = store.get_job(jid)
    assert job is not None
    if hourly_rate is not None and (cur is None or cur.hourly_rate != job.hourly_rate):
        sync_job_income()          # weeks logged before the rate was known (or at the old rate)
    db.log_event("assistant", f"ledger: job {job.name} set", data={"hourly_rate": job.hourly_rate,
                                                                   "pay_schedule": job.pay_schedule})
    return job


def resolve_job(name: str | None = None, *, create: bool = False) -> store.Job:
    """The named job, or the only active one. ``create`` makes a rate-less placeholder when none exists."""
    if name:
        job = store.get_job_by_name(name.strip())
        if job is None:
            jobs = store.list_jobs()
            match = [j for j in jobs if name.strip().lower() in j.name.lower()]
            if len(match) == 1:
                return match[0]
            raise LedgerError(f"no job called {name!r}; known: {', '.join(j.name for j in jobs) or 'none'}")
        return job
    jobs = store.list_jobs()
    if len(jobs) == 1:
        return jobs[0]
    if not jobs:
        if create:
            return set_job(DEFAULT_JOB_NAME)
        raise LedgerError("no job set up yet (pay rate unknown)")
    raise LedgerError("several jobs: say which one (" + ", ".join(j.name for j in jobs) + ")")


# ---------------------------------------------------------------------------
# hours
# ---------------------------------------------------------------------------

def _week(week: date | str | None) -> date:
    return store.week_start_of(week)


DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_KEYS = {**{d: i for i, d in enumerate(DAY_NAMES)},
             **{d: i for i, d in enumerate(("monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
                                            "sunday"))},
             "tues": 1, "weds": 2, "thur": 3, "thurs": 3}
_MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                           "nov", "dec"))}
_ORD = {w: i + 1 for i, w in enumerate(
    "first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth fourteenth "
    "fifteenth sixteenth seventeenth eighteenth nineteenth twentieth".split())}
_ORD.update({f"twenty {w}": 20 + i for w, i in _ORD.items() if i < 10})
_ORD.update({"thirtieth": 30, "thirty first": 31})


def parse_week(week: date | str | None) -> date:
    """The Monday of the week meant by ``week``: a date or any day in it ("2026-11-02"), "this week",
    "last week", "2 weeks ago", "week of November 2(nd)". Raises :class:`LedgerError` (a question to
    ask him once) when the words and the date point at different weeks, or nothing can be read."""
    if week is None or isinstance(week, date):
        return _week(week)
    text = " ".join(re.sub(r"(?<=[a-z])-(?=[a-z])", " ", str(week).lower()).split())
    if not text:
        return _week(None)
    today = db.local_today()
    this = _week(today)
    rel: date | None = None
    if re.search(r"\b(this|current) week\b", text):
        rel = this
    elif re.search(r"\b(last|previous|past) week\b|\bweek before this\b", text):
        rel = this - timedelta(days=7)
    else:
        m = re.search(r"\b(\d+|two|three|four) weeks? ago\b", text)
        if m:
            n = {"two": 2, "three": 3, "four": 4}.get(m.group(1)) or int(m.group(1))
            rel = this - timedelta(days=7 * n)
    explicit: date | None = None
    m = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if m:
        try:
            explicit = date.fromisoformat(m.group(0))
        except ValueError:
            raise LedgerError(f"not a date: {m.group(0)!r}") from None
    else:
        m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?:the\s+)?"
                      r"(\d{1,2})?(?:st|nd|rd|th)?\s*((?:twenty |thirty )?[a-z]+)?", text)
        day_n = None
        if m:
            day_n = int(m.group(2)) if m.group(2) else _ORD.get((m.group(3) or "").strip()) or next(
                (v for k, v in _ORD.items() if (m.group(3) or "").startswith(k)), None)
        if m and day_n:
            try:
                explicit = date(today.year, _MONTHS[m.group(1)], day_n)
            except ValueError:
                raise LedgerError(f"not a date: {str(week)!r}") from None
            if explicit > today:
                explicit = explicit.replace(year=today.year - 1)
    if explicit is not None and rel is not None and _week(explicit) != rel:
        raise LedgerError(f"ambiguous week: \"{week}\" - last/this week is the week of {rel.isoformat()} but "
                          f"{explicit.isoformat()} is in the week of {_week(explicit).isoformat()}. Ask him once "
                          "which week he means.")
    found = explicit or rel
    if found is None:
        raise LedgerError(f"cannot tell which week {str(week)!r} means; ask him once (e.g. the week of November 2)")
    ws = _week(found)
    if ws > this:
        raise LedgerError(f"the week of {ws.isoformat()} has not started yet")
    return ws


def week_pay(job: store.Job, hours: Any) -> tuple[Decimal, Decimal]:
    """(gross, estimated net) for ``hours`` at ``job``'s rate and tax set-aside."""
    gross = _dec(hours) * _dec(job.hourly_rate)
    net = gross * (Decimal(1) - _dec(job.est_tax_rate))
    return _cents(gross), _cents(net)


def log_hours(hours: float | str | Decimal, *, week: date | str | None = None, job: str | None = None,
              source: str = "sms", note: str | None = None) -> dict[str, Any]:
    """Record ``hours`` for ``job`` in the local week containing ``week`` (default this week).

    Upserts the work log and the week's ``job`` income row (idempotent; a correction replaces it).
    Without any job a rate-less placeholder job is created so the hours are not lost; gross is
    then None and the result's ``missing`` says the pay rate is needed.
    """
    try:
        h = _dec(hours)
    except Exception:
        raise LedgerError(f"not a number of hours: {hours!r}") from None
    if not (0 <= h <= MAX_WEEK_HOURS):
        raise LedgerError(f"{fmt_hours(h)} hours is outside 0-{MAX_WEEK_HOURS}; please confirm")
    ws = parse_week(week)
    j = resolve_job(job, create=True)
    store.log_hours(j.id, float(h), week_start=ws, source=source, note=note)
    missing: list[str] = []
    gross = net = None
    ref = f"hours:{j.id}:{ws.isoformat()}"
    if j.hourly_rate > 0:
        gross, net = week_pay(j, h)
        store.set_income(float(gross), source="job", ref=ref, day=ws,
                         note=f"{fmt_hours(h)} h x {fmt_usd(j.hourly_rate, whole=False)} ({j.name})")
    else:
        missing.append(f"no pay rate set for {j.name}")
    db.log_event("assistant", f"ledger: {fmt_hours(h)} hours logged for week of {ws.isoformat()}",
                 data={"job": j.name, "hours": float(h), "gross": None if gross is None else float(gross),
                       "source": source})
    today = db.local_today()
    month = period_totals(today.replace(day=1), today)
    return {"job": j.name, "week_start": ws.isoformat(), "hours": float(h),
            "hourly_rate": j.hourly_rate, "gross": None if gross is None else float(gross),
            "net_estimate": None if net is None else float(net), "est_tax_rate": j.est_tax_rate,
            "month_to_date_gross": month["job_gross"], "missing": missing}


def log_days(days: dict[str, Any], *, week: date | str | None, job: str | None = None, source: str = "sms",
             note: str | None = None) -> dict[str, Any]:
    """Hours per day for one week (``{"mon": 9, "tue": 8}``; keys mon..sun, full names or ISO dates),
    merged with days already logged for that week (a day given again is replaced). The week's total
    becomes the sum of its days and pay is recomputed (:func:`log_hours`). ``week`` is required: a
    list of weekdays without a week is ambiguous, so the caller asks once."""
    if not days:
        raise LedgerError("no days given")
    if week is None or (isinstance(week, str) and not week.strip()):
        raise LedgerError("which week are these days in? Ask him once (this week, last week, or the week of a date)")
    ws = parse_week(week)
    today = db.local_today()
    parsed: dict[date, Decimal] = {}
    for k, v in days.items():
        key = str(k).strip().lower().rstrip(".")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", key):
            d = date.fromisoformat(key)
            if _week(d) != ws:
                raise LedgerError(f"{key} is not in the week of {ws.isoformat()}")
        elif key in _DAY_KEYS:
            d = ws + timedelta(days=_DAY_KEYS[key])
        else:
            raise LedgerError(f"not a day of the week: {k!r}")
        try:
            h = _dec(v)
        except Exception:
            raise LedgerError(f"not a number of hours for {k}: {v!r}") from None
        if not 0 <= h <= 24:
            raise LedgerError(f"{fmt_hours(h)} hours on {DAY_NAMES[d.weekday()]} is outside 0-24; please confirm")
        if d > today:
            raise LedgerError(f"{d.isoformat()} has not happened yet")
        parsed[d] = h
    j = resolve_job(job, create=True)
    before = store.list_work_days(j.id, ws)
    old_week = store.get_work_log(j.id, ws)
    for d, h in sorted(parsed.items()):
        store.set_work_day(j.id, d, float(h), source=source, note=note)
    rows = store.list_work_days(j.id, ws)
    total = sum((_dec(r["hours"]) for r in rows), Decimal(0))
    res = log_hours(total, week=ws, job=j.name, source=source, note=note)
    res["days"] = {DAY_NAMES[date.fromisoformat(r["day"]).weekday()]: r["hours"] for r in rows}
    res["already_logged"] = [DAY_NAMES[date.fromisoformat(r["day"]).weekday()] for r in before
                             if date.fromisoformat(r["day"]) not in parsed]
    if old_week is not None and not before and float(total) != old_week.hours:
        res["replaced_week_total"] = old_week.hours    # an earlier total without days: tell him it changed
    res["confirmation"] = one_line(res)
    return res


def one_line(res: dict[str, Any], honorific: str = "sir") -> str:
    """One sentence confirming a log: total, the days if known, and pay."""
    ws = date.fromisoformat(res["week_start"])
    when = f"the week of {ws.strftime('%B')} {ws.day}"
    days = res.get("days") or {}
    detail = (" (" + ", ".join(f"{d.capitalize()} {fmt_hours(h)}" for d, h in days.items()) + ")") if days else ""
    if res.get("gross") is None:
        return f"Logged, {honorific}: {fmt_hours(res['hours'])} hours for {when}{detail}; I need your hourly rate for the pay."
    pay = f"about {fmt_usd(res['gross'])} before tax"
    if res.get("est_tax_rate"):
        pay += f" and {fmt_usd(res['net_estimate'])} take-home"
    return f"Logged, {honorific}: {fmt_hours(res['hours'])} hours for {when}{detail}, {pay}."


def sync_job_income() -> int:
    """Recompute on read (and the backfill): each logged week of a job with an hourly rate gets its job
    income row at hours x rate. Fixes weeks logged before the rate was known (pay read zero) and
    weeks logged before a rate change. Returns how many rows were written."""
    jobs = {j.id: j for j in store.list_jobs(active_only=False)}
    have = {i.ref: i for i in store.list_income(source="job") if i.ref}
    n = 0
    for w in store.list_work_log(limit=100_000):
        j = jobs.get(w.job_id)
        if j is None or j.hourly_rate <= 0:
            continue
        gross = week_pay(j, w.hours)[0]
        ref = f"hours:{j.id}:{w.week_start}"
        cur = have.get(ref)
        if cur is None or _cents(_dec(cur.amount_usd)) != gross:
            store.set_income(float(gross), source="job", ref=ref, day=w.week_start,
                             note=f"{fmt_hours(w.hours)} h x {fmt_usd(j.hourly_rate, whole=False)} ({j.name})")
            n += 1
    if n:
        db.log_event("assistant", f"ledger: job pay recomputed for {n} week(s)")
    return n


def split_rate(text: str) -> tuple[float | None, str]:
    """("41 hours at $55/h") -> (55.0, "41 hours "): a pay rate stated alongside the hours, and the rest."""
    t = (text or "").lower()
    m = _RATE_RE.search(t)
    if not m:
        return None, text
    return float(m.group(1) or m.group(2)), t[:m.start()] + " " + t[m.end():]


_RATE_RE = re.compile(r"(?:\bat\s+|@\s*)?\$\s*(\d+(?:\.\d+)?)(?:\s*(?:/\s*h(?:ou)?r?\b|an hour|per hour|a hour|hourly))?"
                      r"|(?:\bat\s+|@\s*)?(\d+(?:\.\d+)?)\s*(?:dollars?|bucks)?\s*(?:/\s*h(?:ou)?r?\b|an hour|per hour|"
                      r"a hour|hourly)")


def last_week_hours(job: str | None = None, *, before: date | str | None = None) -> float | None:
    """Hours logged for the week before ``before``'s week (default this week), or None."""
    try:
        j = resolve_job(job)
    except LedgerError:
        return None
    row = store.get_work_log(j.id, _week(before) - timedelta(days=7))
    return None if row is None else row.hours


def confirmation(res: dict[str, Any], honorific: str = "sir") -> str:
    """The SMS confirming a log, e.g. 'Logged, sir: 38.5 hours, roughly $962 before tax. ...'."""
    h = fmt_hours(res["hours"])
    if res.get("gross") is None:
        return (f"Logged, {honorific}: {h} hours for the week of {res['week_start']}. "
                "What is your hourly rate, so I can work out the pay?")
    text = f"Logged, {honorific}: {h} hours, roughly {fmt_usd(res['gross'])} before tax"
    if res.get("est_tax_rate"):
        text += f", about {fmt_usd(res['net_estimate'])} after the set-aside"
    text += f". That brings the month to {fmt_usd(res['month_to_date_gross'])}."
    return text


# ---------------------------------------------------------------------------
# parsing how a person texts hours
# ---------------------------------------------------------------------------

_ONES = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                    "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if i > 1}
_NUM = r"\d+(?:\.\d+)?"
_ZERO_RE = re.compile(r"\b(none|nothing|zero|nada|no hours|no work|didn'?t work|did not work|not working|"
                      r"day off|week off|off (?:all|this|the whole) week|on (?:leave|vacation|holiday)|off sick|"
                      r"was sick|been sick|unemployed)\b")
_SAME_RE = re.compile(r"\b(same as (?:last|the last|previous|usual)(?: week)?|same again|the same|as usual|"
                      r"same hours)\b")
_UNSURE_RE = re.compile(r"\b(between|or so maybe|not sure|no idea|don'?t know|dunno)\b|\?|" + _NUM + r"\s*(?:-|to|–|or)\s*" + _NUM)
# (pattern, True when the first group is the number of days)
_PER_DAY: list[tuple[re.Pattern[str], bool]] = [
    (re.compile(rf"({_NUM})\s*(?:h|hrs?|hours?)?\s*(?:a|per|each|/|every)\s*day\b\D{{0,25}}?(\d+)\s*days?\b"), False),
    (re.compile(rf"(\d+)\s*days?\b\D{{0,12}}?({_NUM})\s*(?:h|hrs?|hours?)?\s*(?:a|per|each)\s*day\b"), True),
    (re.compile(rf"(\d+)\s*days?\b\s*(?:at|of|x|×|@|for)\s*({_NUM})\s*(?:h|hrs?|hours?)?\b"), True),
    (re.compile(rf"\b({_NUM})\s*(?:x|×|\*)\s*({_NUM})\b"), True),
]


def _words_to_digits(t: str) -> str:
    toks = re.split(r"(\s+)", t)
    out: list[str] = []
    i = 0
    while i < len(toks):
        w = toks[i]
        if w in _TENS:
            val = _TENS[w]
            if i + 2 < len(toks) and toks[i + 2] in _ONES and 0 < _ONES[toks[i + 2]] < 10:
                val += _ONES[toks[i + 2]]
                i += 2
            out.append(str(val))
        elif w in _ONES:
            out.append(str(_ONES[w]))
        else:
            out.append(w)
        i += 1
    return "".join(out)


def _normalise(text: str) -> str:
    t = (text or "").lower().replace("½", ".5").replace("’", "'")
    t = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", t)            # thirty-eight -> thirty eight
    t = re.sub(r"[,;!]", " ", t)
    t = _words_to_digits(t)
    t = re.sub(rf"(\d+)\s*(?:and|&)\s*(?:a\s+)?half\b", lambda m: f"{m.group(1)}.5", t)
    t = re.sub(rf"(\d+)\s*(?:and|&)\s*(?:a\s+)?quarter\b", lambda m: f"{m.group(1)}.25", t)
    t = re.sub(r"(\d)\s*(?:ish)\b", r"\1", t)
    return t


def parse_hours(text: str, *, last_week: float | None = None) -> float | None:
    """Hours in a texted reply, or None when unsure (never guesses).

    Handles "38.5", "about 40", "32 hrs", "thirty eight and a half", "8 a day, 4 days",
    "4 days at 10", "none this week" (0), "same as last week" (``last_week``, None if unknown).
    Ranges, questions, several unrelated numbers or values over 100 -> None.
    """
    t = _normalise(text)
    if not t.strip():
        return None
    if _UNSURE_RE.search(t):
        return None
    for rx, days_first in _PER_DAY:
        m = rx.search(t)
        if m:
            a, b = Decimal(m.group(1)), Decimal(m.group(2))
            days, per_day = (a, b) if days_first else (b, a)
            if days > 7 or per_day > 24:
                return None
            if re.search(_NUM, t[:m.start()] + " " + t[m.end():]):  # other numbers around: unsure
                return None
            v = days * per_day
            return float(v) if v <= MAX_WEEK_HOURS else None
    nums = {Decimal(n) for n in re.findall(_NUM, t)}
    if _SAME_RE.search(t) and not nums:
        return None if last_week is None else float(last_week)
    if not nums:
        return 0.0 if _ZERO_RE.search(t) else None
    if len(nums) != 1:
        return None
    v = nums.pop()
    return float(v) if v <= MAX_WEEK_HOURS else None


# ---------------------------------------------------------------------------
# totals and summary
# ---------------------------------------------------------------------------

def _spend(start_day: date, end_day: date, scope: str) -> Decimal:
    a = db.local_day_bounds(start_day)[0]
    b = db.local_day_bounds(end_day)[1]
    return _dec(db.llm_spend_between(a, b, scope=scope))


def period_totals(start_day: date, end_day: date) -> dict[str, Any]:
    """Money and hours for local days [start_day, end_day] (inclusive)."""
    jobs = {j.id: j for j in store.list_jobs(active_only=False)}
    hours = Decimal(0)
    net = Decimal(0)
    for w in store.list_work_log(since_week=start_day, limit=10_000):
        ws = date.fromisoformat(w.week_start)
        if start_day <= ws <= end_day:
            hours += _dec(w.hours)
            j = jobs.get(w.job_id)
            if j is not None:
                net += week_pay(j, w.hours)[1]
    gross = sum((_dec(i.amount_usd) for i in store.list_income(start_day, end_day, source="job")), Decimal(0))
    other = sum((_dec(i.amount_usd) for i in store.list_income(start_day, end_day, source="other")), Decimal(0))
    revs = [s.estimated_revenue for s in db.stats_between(start_day, end_day) if s.estimated_revenue is not None]
    revenue = sum((_dec(r) for r in revs), Decimal(0))
    pipeline = _spend(start_day, end_day, "pipeline")
    assistant = _spend(start_day, end_day, "assistant")
    reels_profit = revenue - pipeline - assistant
    return {
        "start": start_day.isoformat(), "end": end_day.isoformat(),
        "hours": float(hours), "job_gross": _f(gross), "job_net_estimate": _f(net), "other_income": _f(other),
        "reels_revenue": _f(revenue), "reels_revenue_known": bool(revs),
        "ai_costs": {"pipeline": round(float(pipeline), 4), "assistant": round(float(assistant), 4),
                     "total": round(float(pipeline + assistant), 4)},
        "reels_profit": _f(reels_profit),
        "net": _f(net + other + reels_profit),
    }


def summary(*, weeks: int = 8) -> dict[str, Any]:
    """The whole money picture as plain data (dashboard, briefs, the ledger agent)."""
    weeks = max(1, min(int(weeks), 104))
    sync_job_income()
    today = db.local_today()
    this_ws = _week(today)
    last_ws = this_ws - timedelta(days=7)
    jobs = store.list_jobs()
    series = []
    for k in range(weeks - 1, -1, -1):
        ws = this_ws - timedelta(days=7 * k)
        p = period_totals(ws, min(ws + timedelta(days=6), today))
        series.append({"week_start": ws.isoformat(), "hours": p["hours"], "job_gross": p["job_gross"],
                       "job_net_estimate": p["job_net_estimate"], "reels_revenue": p["reels_revenue"],
                       "ai_costs": p["ai_costs"]["total"], "reels_profit": p["reels_profit"], "net": p["net"]})
    worked = [s for s in series if s["hours"] > 0 and s["job_gross"] > 0]
    eff = (_dec(sum(s["job_gross"] for s in worked)) / _dec(sum(s["hours"] for s in worked))) if worked else None
    ytd = period_totals(today.replace(month=1, day=1), today)

    missing: list[str] = []
    if not jobs:
        missing.append("no job or pay rate set")
    missing += [f"no pay rate set for {j.name}" for j in jobs if j.hourly_rate <= 0]
    if jobs and all(j.est_tax_rate <= 0 for j in jobs):
        missing.append("no tax set-aside rate set (take-home shown equal to gross)")
    if jobs:
        logged = {w.week_start for w in store.list_work_log(since_week=last_ws)}
        if last_ws.isoformat() not in logged:
            missing.append(f"no hours logged for week of {last_ws.isoformat()}")
        if today.weekday() >= 4 and this_ws.isoformat() not in logged:
            missing.append(f"no hours logged for week of {this_ws.isoformat()}")

    goals = []
    for g in store.list_goals("active"):
        metric = (g.metric or "").lower()
        if not any(k in metric or k in g.title.lower() for k in ("saving", "income", "earn", "money", "$")):
            continue
        current = g.current
        if current is None and any(k in metric for k in ("income", "earn")):
            current = ytd["job_gross"] + ytd["other_income"] + ytd["reels_profit"]
        pct = round(100 * current / g.target, 1) if (current is not None and g.target) else None
        goals.append({"id": g.id, "title": g.title, "metric": g.metric, "target": g.target, "current": current,
                      "pct": pct, "due": g.due})

    return {
        "as_of": today.isoformat(), "currency": "USD",
        "jobs": [{"name": j.name, "hourly_rate": j.hourly_rate, "pay_schedule": j.pay_schedule,
                  "est_tax_rate": j.est_tax_rate} for j in jobs],
        "this_week": period_totals(this_ws, today),
        "last_week": period_totals(last_ws, last_ws + timedelta(days=6)),
        "month_to_date": period_totals(today.replace(day=1), today),
        "year_to_date": ytd,
        "weekly": series,
        "hours_trend": [{"week_start": s["week_start"], "hours": s["hours"]} for s in series],
        "effective_hourly_rate": None if eff is None else _f(eff),
        "goals": goals,
        "missing": missing,
        "notes": "Job pay is dated to the week's Monday. Take-home is an estimate from the tax set-aside. "
                 "Reels profit = YouTube estimated revenue minus all AI spend (pipeline + assistant).",
    }


def brief_line(s: dict[str, Any] | None = None) -> str:
    """One plain sentence for briefs: 'This week: 38.5 h, $962 from work; month $3,100.'"""
    s = s or summary(weeks=1)
    tw, mtd = s["this_week"], s["month_to_date"]
    if not s["jobs"]:
        return ""
    parts = [f"This week: {fmt_hours(tw['hours'])} h, {fmt_usd(tw['job_gross'])} from work" if tw["hours"]
             else "No hours logged yet this week"]
    parts.append(f"month to date {fmt_usd(mtd['job_gross'])}")
    if mtd["reels_revenue_known"]:
        parts.append(f"Shorts {fmt_usd(mtd['reels_profit'])} after AI costs")
    return "; ".join(parts) + "."


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

def _tool_log_hours(ctx: Any, a: dict[str, Any]) -> Any:
    from assistant.tools import ToolError

    src = getattr(ctx, "source", "assistant") or "assistant"
    try:
        if a.get("days"):
            return log_days(a["days"], week=a.get("week"), job=a.get("job") or None, source=src, note=a.get("note"))
        if a.get("hours") is None:
            raise LedgerError("give hours (the week's total) or days ({\"mon\": 9, ...})")
        res = log_hours(a["hours"], week=a.get("week") or None, job=a.get("job") or None, source=src,
                        note=a.get("note"))
        res["confirmation"] = one_line(res)
        return res
    except (LedgerError, ValueError) as exc:
        raise ToolError(str(exc)) from None


def _tool_summary(ctx: Any, a: dict[str, Any]) -> Any:
    s = summary(weeks=int(a.get("weeks", 8)))
    return s


def register_tools(register: Callable[..., None]) -> None:
    from assistant.tools import Tool, _obj

    register(Tool("ledger_log_hours",
                  "Record the hours the owner worked, for this or any past week, and get gross pay, estimated "
                  "take-home and a one-sentence 'confirmation' to say back. Either hours = the week's total, or "
                  "days = hours per day, e.g. {\"mon\": 9, \"tue\": 8}: days merge with days already logged "
                  "that week (a day given again is replaced) and the total becomes their sum. week: \"this week\", "
                  "\"last week\" or any date in it (YYYY-MM-DD or \"November 2\"); required with days. If the "
                  "tool says the week is ambiguous, ask him once. job: when he has several. Only log hours he "
                  "stated or clearly implied.",
                  _obj({"hours": {"type": "number", "minimum": 0, "maximum": MAX_WEEK_HOURS},
                        "days": {"type": "object", "properties": {d: {"type": "number"} for d in DAY_NAMES}},
                        "week": {"type": "string"}, "job": {"type": "string"}, "note": {"type": "string"}}),
                  _tool_log_hours, owner="assistant.ledger", side_effects=True))
    register(Tool("ledger_summary",
                  "The owner's money picture: this week, last week, month and year to date (job pay, take-home "
                  "estimate, Shorts revenue minus AI costs), weekly series, hours trend, effective hourly rate, "
                  "goal progress, and 'missing' data you should ask him for rather than assume.",
                  _obj({"weeks": {"type": "integer", "minimum": 1, "maximum": 52}}), _tool_summary,
                  owner="assistant.ledger"))
