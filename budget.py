"""LLM spend accounting and the hard daily budget.

* :func:`record_usage` prices a call and writes an ``llm_usage`` row.
* :func:`check_budget` raises :class:`BudgetExceeded` before a call that would
  push today's spend past the cap (``llm.complete`` calls it for you).
* "Today" is the local calendar day in ``TIMEZONE``.

Daily cap (:func:`daily_cap_usd`)::

    cap = min(strategy.daily_llm_budget_usd  (or DAILY_LLM_BUDGET_USD if the strategy is unreadable),
              MAX_DAILY_LLM_BUDGET_USD)

Strategy increases require phone approval (``strategy.diff_major_changes``);
``MAX_DAILY_LLM_BUDGET_USD`` is the absolute ceiling only the owner can raise
by editing config.

The first time the cap is hit on a given day, a ``warning`` event and a queued
``budget_cap`` call notification are written (once per day). ``notify`` sends
queued notifications; this module never imports it.

Prices are USD per million tokens, from Anthropic's published pricing
(claude-api skill model table, cached 2026-11-03). Cache writes are priced at
the 5-minute TTL rate (1.25x input). Override with ``LLM_PRICES_JSON``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Mapping

import db
from config import get_settings

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """USD per million tokens."""

    input: float
    output: float
    cache_write: float  # 5-minute TTL write
    cache_read: float
    # Some models switch to a higher rate card when the prompt is long.
    long_context_threshold: int | None = None
    long_context: "ModelPrice | None" = None

    def cost(self, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0,
             cache_write_tokens: int = 0) -> float:
        prompt = input_tokens + cache_read_tokens + cache_write_tokens
        card = self
        if self.long_context and self.long_context_threshold is not None and prompt > self.long_context_threshold:
            card = self.long_context
        return (input_tokens * card.input + output_tokens * card.output
                + cache_write_tokens * card.cache_write + cache_read_tokens * card.cache_read) / 1_000_000


PRICES: dict[str, ModelPrice] = {
    # Haiku 5.5: $0.10 / $0.50 for prompts <= 100K tokens, $0.50 / $2.50 above;
    # cache reads 0.1x input, 5-minute cache writes 1.25x input.
    "claude-haiku-5-5": ModelPrice(
        input=0.10, output=0.50, cache_write=0.125, cache_read=0.01,
        long_context_threshold=100_000,
        long_context=ModelPrice(input=0.50, output=2.50, cache_write=0.625, cache_read=0.05),
    ),
    # Sonnet 5.5: $2 / $10; cache reads $0.10; 5-minute writes 1.25x.
    "claude-sonnet-5-5": ModelPrice(input=2.00, output=10.00, cache_write=2.50, cache_read=0.10),
    # Possible server-side fallback target for Sonnet 5.5 refusals.
    "claude-sonnet-5": ModelPrice(input=2.00, output=10.00, cache_write=2.50, cache_read=0.20),
    "claude-opus-5-5": ModelPrice(input=4.00, output=20.00, cache_write=5.00, cache_read=0.20),
}
# Unknown models are priced conservatively so the budget errs on the safe side.
FALLBACK_PRICE = PRICES["claude-opus-5-5"]


class BudgetExceeded(RuntimeError):
    """Today's LLM budget is exhausted (or spending is paused)."""

    def __init__(self, message: str, *, spent: float, cap: float, estimated: float = 0.0):
        super().__init__(message)
        self.spent = spent
        self.cap = cap
        self.estimated = estimated


def price_for(model: str) -> ModelPrice:
    """Price card for ``model``, honouring ``LLM_PRICES_JSON`` overrides."""
    override: Mapping[str, float] | None = get_settings().llm_price_overrides.get(model)
    if override:
        base = PRICES.get(model, FALLBACK_PRICE)
        return ModelPrice(
            input=float(override.get("input", base.input)),
            output=float(override.get("output", base.output)),
            cache_write=float(override.get("cache_write", base.cache_write)),
            cache_read=float(override.get("cache_read", base.cache_read)),
        )
    if model not in PRICES:
        log.warning("no price for model %s; using conservative fallback", model)
        return FALLBACK_PRICE
    return PRICES[model]


def cost_usd(model: str, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0,
             cache_write_tokens: int = 0) -> float:
    return price_for(model).cost(input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)


def estimate_cost(model: str, input_tokens: int, max_output_tokens: int) -> float:
    """Worst-case cost of a call: every prompt token uncached, every output token used."""
    return price_for(model).cost(input_tokens, max_output_tokens)


# ---------------------------------------------------------------------------
# daily cap
# ---------------------------------------------------------------------------

def daily_cap_usd() -> float:
    s = get_settings()
    base = s.daily_llm_budget_usd
    try:
        from strategy import load_strategy  # late import keeps budget importable on its own

        try:
            base = float(load_strategy()["daily_llm_budget_usd"])
        except (OSError, ValueError):  # missing, unparsable or invalid (StrategyError is a ValueError)
            log.warning("strategy unreadable; using DAILY_LLM_BUDGET_USD for the cap")
    except ImportError:  # pragma: no cover
        pass
    return max(0.0, min(base, s.max_daily_llm_budget_usd))


def spent_today(day: date | None = None) -> float:
    start, end = db.local_day_bounds(day)
    return db.llm_spend_between(start, end)


def remaining_today() -> float:
    return max(0.0, daily_cap_usd() - spent_today())


_PAUSE_KEY = "budget:paused"


def set_paused(paused: bool, reason: str = "") -> None:
    """Manual kill switch for all LLM spending (e.g. owner texts PAUSE)."""
    if paused:
        db.kv_set(_PAUSE_KEY, {"reason": reason, "at": db.now_iso()})
    else:
        db.kv_delete(_PAUSE_KEY)
    db.log_event("budget", "LLM spending paused" if paused else "LLM spending resumed",
                 level="warning" if paused else "info", data={"reason": reason})


def is_paused() -> bool:
    """True if spending is manually paused or today's cap is already used up."""
    return db.kv_get(_PAUSE_KEY) is not None or spent_today() >= daily_cap_usd()


def check_budget(estimated_usd: float = 0.0) -> float:
    """Raise :class:`BudgetExceeded` unless ``estimated_usd`` fits in today's remaining budget.

    Returns the remaining budget (before the call) when OK.
    """
    pause = db.kv_get(_PAUSE_KEY)
    spent, cap = spent_today(), daily_cap_usd()
    if pause is not None:
        raise BudgetExceeded(f"LLM spending is paused: {pause.get('reason') or 'manual pause'}",
                             spent=spent, cap=cap, estimated=estimated_usd)
    if spent + estimated_usd > cap:
        _alert_cap_hit(spent, cap, estimated_usd)
        raise BudgetExceeded(
            f"daily LLM budget exhausted: spent ${spent:.4f} + est ${estimated_usd:.4f} > cap ${cap:.2f}",
            spent=spent, cap=cap, estimated=estimated_usd)
    return cap - spent


def record_usage(
    model: str,
    purpose: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    tier: str | None = None,
    source_id: int | None = None,
    clip_id: int | None = None,
    request_id: str | None = None,
    fake: bool = False,
) -> float:
    """Price a completed call, write ``llm_usage``, and return its cost in USD.

    Fake-mode calls are recorded (``fake=1``) and count toward the cap, so
    budget behaviour can be exercised offline.
    """
    cost = cost_usd(model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)
    db.insert_llm_usage(
        model=model, purpose=purpose, input_tokens=input_tokens, output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens, cache_write_tokens=cache_write_tokens, cost_usd=cost,
        tier=tier, source_id=source_id, clip_id=clip_id, request_id=request_id, fake=fake,
    )
    spent, cap = spent_today(), daily_cap_usd()
    if spent >= cap:
        _alert_cap_hit(spent, cap, 0.0)
    return cost


def _alert_cap_hit(spent: float, cap: float, estimated: float) -> None:
    """Once per local day: log a warning event and queue a phone notification."""
    day = db.local_today().isoformat()
    if not db.kv_set_if_absent(f"budget:alerted:{day}", {"spent": spent, "cap": cap}):
        return
    msg = (f"Heads up: today's AI budget of {cap:.2f} dollars is used up. "
           f"Spent {spent:.2f}. Judgment steps are paused until tomorrow.")
    db.log_event("budget", "daily LLM budget cap hit", level="warning",
                 data={"day": day, "spent": spent, "cap": cap, "estimated": estimated})
    db.add_notification("budget_cap", msg, "call")
