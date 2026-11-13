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
