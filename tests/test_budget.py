import json

import pytest

import budget
import config
import db
import strategy as st


def test_pricing_table():
    # Haiku 5.5 short-context card: 100K in ($0.10/MTok) + 1M out ($0.50/MTok)
    assert budget.cost_usd("claude-haiku-5-5", 100_000, 1_000_000) == pytest.approx(0.01 + 0.50)
    assert budget.cost_usd("claude-sonnet-5-5", 1_000_000, 0, cache_read_tokens=1_000_000,
                           cache_write_tokens=1_000_000) == pytest.approx(2.0 + 0.10 + 2.50)
    # Haiku long-context card kicks in above 100K prompt tokens
    assert budget.cost_usd("claude-haiku-5-5", 100_001, 0) == pytest.approx(100_001 * 0.50 / 1e6)
    # unknown model -> conservative fallback
    assert budget.price_for("mystery") == budget.FALLBACK_PRICE


def test_price_override(monkeypatch):
    monkeypatch.setenv("LLM_PRICES_JSON", json.dumps({"claude-haiku-5-5": {"input": 1.0, "output": 1.0}}))
    config.reset_settings()
    assert budget.cost_usd("claude-haiku-5-5", 100_000, 1_000_000) == pytest.approx(1.1)


def test_daily_cap_logic(strategy, settings):
    assert budget.daily_cap_usd() == pytest.approx(1.0)  # from strategy
    st.save_strategy(strategy.updated(daily_llm_budget_usd=3.0))
    assert budget.daily_cap_usd() == pytest.approx(3.0)
    st.save_strategy(strategy.updated(daily_llm_budget_usd=50.0))
    assert budget.daily_cap_usd() == pytest.approx(settings.max_daily_llm_budget_usd)  # ceiling
    settings.strategy_path.write_text("{broken")
    config.set_settings(settings.replace(daily_llm_budget_usd=0.42))
    assert budget.daily_cap_usd() == pytest.approx(0.42)  # strategy unreadable -> config


def test_record_spend_and_remaining(clock):
    cost = budget.record_usage("claude-sonnet-5-5", "t", input_tokens=100_000, output_tokens=10_000)
    assert cost == pytest.approx(0.2 + 0.1)
    assert budget.spent_today() == pytest.approx(0.3)
    assert budget.remaining_today() == pytest.approx(0.7)
    assert budget.check_budget(0.5) == pytest.approx(0.7)
    with pytest.raises(budget.BudgetExceeded) as ei:
        budget.check_budget(0.8)
    assert ei.value.cap == pytest.approx(1.0) and ei.value.spent == pytest.approx(0.3)
    clock.advance(days=1)
    assert budget.spent_today() == 0


def test_local_day_boundary(clock):
    from datetime import UTC, datetime

    clock.set(datetime(2026, 11, 14, 4, 30, tzinfo=UTC))  # 23:30 local on the 13th
    budget.record_usage("claude-sonnet-5-5", "late", input_tokens=0, output_tokens=50_000)
    clock.set(datetime(2026, 11, 14, 5, 30, tzinfo=UTC))  # 00:30 local on the 14th
    assert budget.spent_today() == 0


def test_cap_hit_alerts_once_per_day(clock):
    budget.record_usage("claude-sonnet-5-5", "big", input_tokens=0, output_tokens=100_000)  # $1.00
    assert budget.is_paused()
    for _ in range(3):
        with pytest.raises(budget.BudgetExceeded):
            budget.check_budget(0.01)
    alerts = [n for n in db.list_notifications() if n.kind == "budget_cap"]
    assert len(alerts) == 1 and alerts[0].channel == "call" and alerts[0].status == "queued"
    assert len(db.list_events(module="budget", level="warning")) == 1
    clock.advance(days=1)
    assert not budget.is_paused()
    budget.record_usage("claude-sonnet-5-5", "big", input_tokens=0, output_tokens=100_000)
    assert len([n for n in db.list_notifications() if n.kind == "budget_cap"]) == 2


def test_manual_pause():
    assert not budget.is_paused()
    budget.set_paused(True, "owner texted PAUSE")
    assert budget.is_paused()
    with pytest.raises(budget.BudgetExceeded, match="paused"):
        budget.check_budget()
    budget.set_paused(False)
    budget.check_budget()
