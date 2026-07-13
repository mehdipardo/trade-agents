"""Backtest engine: pure scoring math + replay + net-of-fees + labeling."""

from __future__ import annotations

import pytest

from app.services.backtest import (
    _capped_pnl_pct,
    load_backtest_events,
    run_backtest,
    score_trade,
)
from app.services.store import InMemoryStore, get_store, set_store


def test_capped_pnl_respects_sl_tp() -> None:
    # Long: +10% market move but TP caps at 3%.
    assert _capped_pnl_pct("buy", 10.0, 1.5, 3.0) == pytest.approx(3.0)
    # Long: -10% move but SL caps loss at -1.5%.
    assert _capped_pnl_pct("buy", -10.0, 1.5, 3.0) == pytest.approx(-1.5)
    # Short captures the negation: -4% market move -> +3% (capped at TP).
    assert _capped_pnl_pct("sell", -4.0, 1.5, 3.0) == pytest.approx(3.0)
    # Short on a +2% move -> -1.5% (capped at SL).
    assert _capped_pnl_pct("sell", 2.0, 1.5, 3.0) == pytest.approx(-1.5)


def test_score_trade_is_net_of_fees() -> None:
    s = score_trade("buy", 100.0, 60000.0, 10.0, 1.5, 3.0, 0.02)
    # +3% on 100 notional = +3 gross, minus round-trip fee.
    assert s["gross_pnl"] == pytest.approx(3.0)
    assert s["fee"] > 0
    assert s["net_pnl"] == pytest.approx(s["gross_pnl"] - s["fee"])


def test_sample_dataset_loads() -> None:
    events = load_backtest_events()
    assert len(events) >= 5
    assert all("market_move_pct" in e for e in events)


async def test_run_backtest_labels_and_records() -> None:
    set_store(InMemoryStore())
    report = await run_backtest(record=True)
    assert report["mode"] == "backtest"
    assert report["events"] >= 5
    assert "net_pnl_quote" in report and "total_fees_quote" in report
    # Every recorded history row is tagged mode=backtest (never confused with live).
    history = await get_store().history(100)
    assert history and all(h.get("mode") == "backtest" for h in history)


async def test_run_backtest_without_record_does_not_touch_history() -> None:
    set_store(InMemoryStore())
    await run_backtest(record=False)
    assert await get_store().history(100) == []


# --- LLM usage tracker (separate concern, colocated for brevity) -----------


async def test_llm_usage_starts_empty_and_bumps() -> None:
    set_store(InMemoryStore())
    s = get_store()
    assert (await s.llm_usage())["calls_total"] == 0
    await s.bump_llm(300, 50)
    await s.bump_llm(200, 40)
    u = await s.llm_usage()
    assert u["calls_total"] == 2
    assert u["calls_today"] == 2
    assert u["prompt_tokens"] == 500
    assert u["completion_tokens"] == 90
    assert u["total_tokens"] == 590
