"""The usage breakdown groups real calls, prices them, and names the biggest user."""

from __future__ import annotations

import pytest

import db
import main
from assistant import usage_report
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")


def test_breakdown_names_the_biggest_user(clock, capsys):
    for _ in range(3):
        db.insert_llm_usage(model="claude-opus-5-5", purpose="assistant.main", input_tokens=500, output_tokens=200,
                            cache_read_tokens=60_000, cache_write_tokens=2_000, cost_usd=0)
    for _ in range(12):
        db.insert_llm_usage(model="claude-haiku-5-5", purpose="assistant.heartbeat", input_tokens=3_000,
                            output_tokens=100, cost_usd=0)
    db.insert_llm_usage(model="m", purpose="assistant.main", input_tokens=9, output_tokens=9, cost_usd=0, fake=True)
    rep = usage_report.report(24)
    assert rep["calls"] == 15 and rep["api_usd_total"] > 0
    assert rep["by_purpose"][0]["purpose"] == "assistant.main"
    assert sum(g["share_pct"] for g in rep["by_purpose"]) == pytest.approx(100, abs=0.2)
    joined = " ".join(rep["findings"])
    assert "Biggest user: spoken" in joined and "heartbeat called the model 12 times" in joined
    assert "re-reads about 62k tokens" in joined
    assert main.main(["assistant", "usage-report", "--hours", "24"]) == 0
    assert "Findings:" in capsys.readouterr().out


def test_old_calls_are_outside_the_window(clock):
    db.insert_llm_usage(model="m", purpose="assistant.main", input_tokens=1, output_tokens=1, cost_usd=0)
    clock.advance(hours=30)
    assert usage_report.report(24)["calls"] == 0
