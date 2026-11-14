import pytest

import db
from assistant import ledger, store
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")


@pytest.mark.parametrize("text,expected", [
    ("38.5", 38.5), ("about 40", 40.0), ("32 hrs", 32.0), ("8 a day, 4 days", 32.0), ("4 days at 10", 40.0),
    ("none this week", 0.0), ("thirty eight and a half", 38.5), ("40ish", 40.0), ("38½", 38.5),
    ("38-40", None), ("38 at the warehouse, 6 at the cafe", None), ("I'm tired", None), ("120", None),
    ("same as last week", None), ("no", None),
])
def test_parse_hours(text, expected):
    assert ledger.parse_hours(text) == expected


def test_same_as_last_week_and_no_job_placeholder(clock):
    assert ledger.parse_hours("same as last week", last_week=37) == 37.0
    res = ledger.log_hours(30)
    assert res["gross"] is None and res["missing"] == ["no pay rate set for Job"]
    assert ledger.summary()["missing"][0] == "no pay rate set for Job"
    with pytest.raises(ledger.LedgerError):
        ledger.log_hours(101)


def test_summary_money_exact(clock):
    ledger.set_job("Cafe", hourly_rate=17.35, est_tax_rate=0.1)
    for _ in range(3):
        ledger.log_hours(33.3)                      # idempotent; Decimal math, no float drift
    s = ledger.summary(weeks=3)
    assert s["this_week"]["job_gross"] == 577.76 and s["this_week"]["job_net_estimate"] == 519.98
    assert len(store.list_income()) == 1 and len(s["weekly"]) == 3
    assert s["effective_hourly_rate"] == 17.35
    db.insert_llm_usage(model="m", purpose="pick.finalize", input_tokens=1, output_tokens=1, cost_usd=0.5)
    db.insert_llm_usage(model="m", purpose="assistant.main", input_tokens=1, output_tokens=1, cost_usd=0.25)
    db.upsert_stat("vid1", db.local_today().isoformat(), views=10, estimated_revenue=2.0)
    t = ledger.summary()["this_week"]
    assert t["ai_costs"] == {"pipeline": 0.5, "assistant": 0.25, "total": 0.75} and t["reels_profit"] == 1.25
    store.add_goal("Savings", metric="income", target=1000)
    assert ledger.summary()["goals"][0]["pct"] is not None


def _log_tool(**args):
    from assistant import tools

    tools.load_extensions()
    ctx = tools.ToolContext(channel="voice", agent="main", source="voice")
    return tools.dispatch("ledger_log_hours", args, ctx, allowed={"ledger_log_hours"})


def test_per_day_hours_for_a_past_week_merge_with_friday(clock):
    import json

    ledger.set_job("Wealthspire", hourly_rate=55)
    first = ledger.log_days({"fri": 8}, week="2026-11-06")             # Friday was logged earlier
    assert first["week_start"] == "2026-11-02" and first["hours"] == 8
    res = _log_tool(days={"mon": 9, "tue": 8, "wed": 8, "thu": 8}, week="last week, the week of November second")
    assert res.ok, res.content
    out = json.loads(res.content)
    assert out["hours"] == 41 and out["gross"] == 2255 and out["already_logged"] == ["fri"]
    assert out["days"] == {"mon": 9, "tue": 8, "wed": 8, "thu": 8, "fri": 8}
    assert out["confirmation"] == ("Logged, sir: 41 hours for the week of November 2 (Mon 9, Tue 8, Wed 8, Thu 8, "
                                   "Fri 8), about $2,255 before tax.")
    assert store.get_work_log(store.get_job_by_name("Wealthspire").id, "2026-11-02").hours == 41
    assert len(store.list_income(source="job")) == 1                       # replaced, not added
    again = json.loads(_log_tool(days={"wed": 7}, week="2026-11-04").content)  # a correction to one day
    assert again["hours"] == 40 and again["gross"] == 2200


def test_ambiguous_or_missing_week_asks_once(clock):
    ledger.set_job("Wealthspire", hourly_rate=55)
    res = _log_tool(days={"mon": 9})
    assert not res.ok and "which week" in res.content
    res = _log_tool(days={"mon": 9}, week="last week, November 9")        # today is Fri 13 Nov: last week = 2 Nov
    assert not res.ok and "ambiguous week" in res.content and "Ask him once" in res.content
    assert store.list_work_log() == []


def test_hours_logged_before_the_rate_was_known_are_paid_and_reported(clock):
    from assistant import status

    ledger.log_hours(41, week="last week")                  # placeholder job, no rate: pay unknown
    assert store.list_income() == []
    ledger.set_job("Job", hourly_rate=55, est_tax_rate=0.2)   # the rate arrives: the week is recomputed
    s = ledger.summary(weeks=2)
    assert s["last_week"]["hours"] == 41 and s["last_week"]["job_gross"] == 2255
    assert s["last_week"]["job_net_estimate"] == 1804
    lines = status.work_lines()
    assert "Last week (from Monday 2026-11-02): 41 h worked, $2,255.00 gross pay, $1,804.00 estimated take-home" in lines
    assert "income logged $0.00" not in status.status_snapshot()


def test_checkin_answer_with_hours_and_rate(clock):
    from assistant import checkins

    chk = store.add_checkin("weekly_hours", "How many hours last week?", db.to_iso(db.utcnow()))
    reply = checkins._answer(store.get_checkin(chk), "41 hours at $55/h", "sir")
    assert reply and "$2,255" in reply
    assert store.get_job_by_name("Job").hourly_rate == 55 and store.income_total(source="job") == 2255


def test_backfill_recomputes_missing_job_pay(clock):
    jid = store.upsert_job("Wealthspire", hourly_rate=55)
    store.log_hours(jid, 41, week_start="2026-11-02")          # an old row written without its income row
    assert ledger.sync_job_income() == 1 and ledger.sync_job_income() == 0
    assert store.income_total("2026-11-02", "2026-11-08", source="job") == 2255
    store.upsert_job("Wealthspire", hourly_rate=60)             # a rate changed behind the ledger's back
    assert ledger.summary()["last_week"]["job_gross"] == 2460
