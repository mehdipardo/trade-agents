"""Runner mechanism, leverage boost on high-impact signals, LLM critique."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.models.schemas import Signal
from app.risk.rules import RiskConfig, RiskContext, evaluate
from app.services.critique import generate_and_store_critique
from app.services.position_monitor import (
    exit_reason,
    net_realized_pnl,
    realized_pnl,
    round_trip_fee,
    runner_exit_reason,
    sl_tp_prices,
)
from app.services.store import InMemoryStore, get_store, set_store
from app.services.strategy import STRATEGIES, get_active_strategy, set_active_strategy


def _settings() -> Settings:
    return Settings(_env_file=None, paper_trading="true", exchange_sandbox="true")  # type: ignore[arg-type]


def _ctx(**overrides: object) -> RiskContext:
    base = {
        "equity_quote": 1000.0,
        "trades_last_hour": 0,
        "daily_pnl_quote": 0.0,
        "kill_switch_active": False,
        "asset_in_cooldown": False,
        "open_position_on_asset": False,
        "free_capital_quote": 1000.0,
    }
    base.update(overrides)
    return RiskContext(**base)  # type: ignore[arg-type]


def _signal(impact: int = 5, sentiment: str = "BULL") -> Signal:
    return Signal(
        sentiment=sentiment,  # type: ignore[arg-type]
        intensity=4,
        asset="BTC/USDT",
        confidence=0.8,
        rationale="test",
        event_type="macro",
        actionability=4,
        impact_score=impact,
    )


# --- Leverage boost -------------------------------------------------------


def test_balanced_strategy_applies_x3_on_impact_ge_8() -> None:
    config = RiskConfig.from_settings(_settings(), STRATEGIES["balanced"])
    base = evaluate(_signal(impact=5), _ctx(), config)
    verdict = evaluate(_signal(impact=9), _ctx(), config)
    assert verdict.approved
    assert verdict.leverage == 3
    # SL/TP percents stay fixed; the position size (risk budget) is what x3s.
    assert verdict.stop_loss_pct == pytest.approx(1.5)
    assert verdict.take_profit_pct == pytest.approx(3.0)
    assert verdict.position_size_quote == pytest.approx(base.position_size_quote * 3, abs=0.1)


def test_no_leverage_below_threshold() -> None:
    config = RiskConfig.from_settings(_settings(), STRATEGIES["balanced"])
    verdict = evaluate(_signal(impact=7), _ctx(), config)
    assert verdict.approved
    assert verdict.leverage == 1
    assert verdict.stop_loss_pct == pytest.approx(1.5)


def test_conservative_strategy_never_boosts_by_default() -> None:
    # Conservative was created with default leverage_multiplier=1.
    config = RiskConfig.from_settings(_settings(), STRATEGIES["conservative"])
    # Conservative requires min_intensity=4 & actionability=4, so bump both.
    sig = Signal(
        sentiment="BULL", intensity=5, asset="BTC/USDT", confidence=0.9,
        rationale="test", event_type="macro", actionability=5, impact_score=10,
    )
    verdict = evaluate(sig, _ctx(), config)
    assert verdict.approved
    assert verdict.leverage == 1


def test_verdict_carries_runner_params_when_strategy_defines_them() -> None:
    config = RiskConfig.from_settings(_settings(), STRATEGIES["balanced"])
    verdict = evaluate(_signal(), _ctx(), config)
    assert verdict.runner_pct == pytest.approx(0.20)
    assert verdict.runner_tp_pct == pytest.approx(50.0)


# --- Runner phase transitions --------------------------------------------


def test_runner_stop_at_entry_for_long() -> None:
    # Long: any price <= entry is the runner's SL (breakeven).
    assert runner_exit_reason("buy", 100.0, 100.0, 50.0) == "runner_stop"
    assert runner_exit_reason("buy", 100.0, 99.9, 50.0) == "runner_stop"
    assert runner_exit_reason("buy", 100.0, 149.0, 50.0) is None
    assert runner_exit_reason("buy", 100.0, 150.0, 50.0) == "runner_take_profit"


def test_runner_stop_at_entry_for_short() -> None:
    # Short: any price >= entry is the runner's SL.
    assert runner_exit_reason("sell", 100.0, 100.0, 50.0) == "runner_stop"
    assert runner_exit_reason("sell", 100.0, 100.1, 50.0) == "runner_stop"
    assert runner_exit_reason("sell", 100.0, 51.0, 50.0) is None
    assert runner_exit_reason("sell", 100.0, 50.0, 50.0) == "runner_take_profit"


def test_round_trip_fee_charges_both_sides() -> None:
    # 0.02% on entry + exit notional.
    fee = round_trip_fee(60000, 61800, 0.008, 0.02)
    expected = (60000 * 0.008 + 61800 * 0.008) * 0.0002
    assert fee == pytest.approx(expected)


def test_net_pnl_is_gross_minus_fees() -> None:
    gross = realized_pnl("buy", 60000, 61800, 0.008)
    net = net_realized_pnl("buy", 60000, 61800, 0.008, 0.02)
    assert net < gross
    assert net == pytest.approx(gross - round_trip_fee(60000, 61800, 0.008, 0.02))


def test_zero_fee_pct_leaves_pnl_untouched() -> None:
    gross = realized_pnl("sell", 180, 175.5, 0.5)
    assert net_realized_pnl("sell", 180, 175.5, 0.5, 0.0) == pytest.approx(gross)


def test_main_phase_helpers_unchanged() -> None:
    assert exit_reason("buy", 100.0, 98.5, 1.5, 3.0) == "stop_loss"
    assert exit_reason("buy", 100.0, 103.0, 1.5, 3.0) == "take_profit"
    sl, tp = sl_tp_prices("buy", 100.0, 1.5, 3.0)
    assert sl == pytest.approx(98.5)
    assert tp == pytest.approx(103.0)


# --- LLM critique on SL ---------------------------------------------------


async def test_critique_falls_back_offline_without_key() -> None:
    set_store(InMemoryStore())
    position = {
        "side": "buy",
        "entry_price": 100.0,
        "leverage": 3,
        "original_signal": {
            "sentiment": "BULL",
            "intensity": 4,
            "actionability": 4,
            "impact_score": 9,
            "confidence": 0.85,
            "event_type": "macro",
            "rationale": "why",
        },
    }
    record = await generate_and_store_critique(
        "BTC/USDT", "stop_loss", position, exit_price=95.5, pnl=-4.5
    )
    assert record["symbol"] == "BTC/USDT"
    assert record["reason"] == "stop_loss"
    assert "critique" in record and record["critique"]
    all_records = await get_store().critiques()
    assert len(all_records) == 1


# --- Active strategy still works ------------------------------------------


async def test_active_strategy_has_runner_and_leverage_metadata() -> None:
    set_store(InMemoryStore())
    await set_active_strategy("balanced")
    active = await get_active_strategy()
    assert active.runner_pct == 0.20
    assert active.runner_tp_pct == 50.0
    assert active.leverage_multiplier == 3
    assert active.high_impact_threshold == 8
