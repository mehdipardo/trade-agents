"""Étape 4 tests: exhaustive coverage of the deterministic risk engine."""

from __future__ import annotations

import pytest

from app.models.schemas import Signal
from app.risk.rules import RiskConfig, RiskContext, daily_loss_breached, evaluate

CONFIG = RiskConfig()


def make_signal(
    sentiment: str = "BULL",
    intensity: int = 4,
    asset: str | None = "BTC/USDT",
    confidence: float = 0.9,
    actionability: int = 4,
    impact_score: int = 5,
) -> Signal:
    return Signal(
        sentiment=sentiment,  # type: ignore[arg-type]
        intensity=intensity,
        asset=asset,
        confidence=confidence,
        rationale="test",
        event_type="macro",
        actionability=actionability,
        impact_score=impact_score,
    )


def make_signal_impact(impact: int) -> Signal:
    return make_signal(intensity=5, impact_score=impact)


def make_ctx(
    equity_quote: float = 1000.0,
    trades_last_hour: int = 0,
    daily_pnl_quote: float = 0.0,
    kill_switch_active: bool = False,
    asset_in_cooldown: bool = False,
    open_position_on_asset: bool = False,
) -> RiskContext:
    return RiskContext(
        equity_quote=equity_quote,
        trades_last_hour=trades_last_hour,
        daily_pnl_quote=daily_pnl_quote,
        kill_switch_active=kill_switch_active,
        asset_in_cooldown=asset_in_cooldown,
        open_position_on_asset=open_position_on_asset,
    )


# --- Approvals ------------------------------------------------------------


def test_bull_is_approved_buy() -> None:
    v = evaluate(make_signal(), make_ctx(), CONFIG)
    assert v.approved
    assert v.side == "buy"
    assert v.stop_loss_pct == CONFIG.stop_loss_pct
    assert v.take_profit_pct == CONFIG.take_profit_pct


def test_risk_based_sizing_targets_the_sl_loss() -> None:
    # Default: 1% risk, 1.5% SL -> notional 1000*0.01/0.015 = 666.67, and the
    # dollar loss if the SL is hit is exactly 1% of equity ($10).
    v = evaluate(make_signal(), make_ctx(equity_quote=1000.0), CONFIG)
    assert v.approved
    assert v.position_size_quote == pytest.approx(666.67, abs=0.01)
    risk_at_sl = v.position_size_quote * (v.stop_loss_pct / 100)
    assert risk_at_sl == pytest.approx(10.0, abs=0.01)


def test_gross_exposure_cap_applies() -> None:
    # A very tight SL would imply a huge notional; capped at max_gross_exposure.
    cfg = RiskConfig(risk_per_trade_pct=0.01, stop_loss_pct=0.05, max_gross_exposure=3.0)
    v = evaluate(make_signal(), make_ctx(equity_quote=1000.0), cfg)
    assert v.position_size_quote == pytest.approx(3000.0)  # 3x equity cap


def test_high_impact_scales_risk_not_stop() -> None:
    cfg = RiskConfig(
        risk_per_trade_pct=0.01, stop_loss_pct=1.5, high_impact_threshold=8,
        leverage_multiplier=3, max_gross_exposure=5.0,
    )
    base = evaluate(make_signal(intensity=5), make_ctx(equity_quote=1000.0), cfg)
    boosted = evaluate(
        make_signal_impact(9), make_ctx(equity_quote=1000.0), cfg
    )
    assert boosted.leverage == 3
    assert boosted.stop_loss_pct == base.stop_loss_pct  # SL % unchanged
    assert boosted.position_size_quote == pytest.approx(base.position_size_quote * 3, abs=0.1)


def test_bear_opens_short() -> None:
    # Futures: BEAR with no open position opens a short (sell).
    v = evaluate(make_signal(sentiment="BEAR"), make_ctx(open_position_on_asset=False), CONFIG)
    assert v.approved
    assert v.side == "sell"


# --- Rejections (one per rule) -------------------------------------------


def test_reject_kill_switch() -> None:
    v = evaluate(make_signal(), make_ctx(kill_switch_active=True), CONFIG)
    assert not v.approved
    assert "kill switch" in v.reject_reason


def test_reject_daily_loss_breach() -> None:
    # -3% of 1000 = -30. A -30 PnL breaches.
    v = evaluate(make_signal(), make_ctx(daily_pnl_quote=-30.0), CONFIG)
    assert not v.approved
    assert "daily loss" in v.reject_reason


def test_reject_low_confidence() -> None:
    v = evaluate(make_signal(confidence=0.5), make_ctx(), CONFIG)
    assert not v.approved
    assert "confidence" in v.reject_reason


def test_reject_low_intensity() -> None:
    v = evaluate(make_signal(intensity=2), make_ctx(), CONFIG)
    assert not v.approved
    assert "intensity" in v.reject_reason


def test_reject_low_actionability() -> None:
    v = evaluate(make_signal(actionability=1), make_ctx(), CONFIG)
    assert not v.approved
    assert "actionability" in v.reject_reason


def test_reject_no_asset() -> None:
    v = evaluate(make_signal(asset=None), make_ctx(), CONFIG)
    assert not v.approved
    assert "asset" in v.reject_reason


def test_reject_neutral() -> None:
    v = evaluate(make_signal(sentiment="NEUTRAL"), make_ctx(), CONFIG)
    assert not v.approved
    assert "neutral" in v.reject_reason


def test_reject_max_trades_per_hour() -> None:
    v = evaluate(make_signal(), make_ctx(trades_last_hour=CONFIG.max_trades_per_hour), CONFIG)
    assert not v.approved
    assert "max trades" in v.reject_reason


def test_reject_cooldown() -> None:
    v = evaluate(make_signal(), make_ctx(asset_in_cooldown=True), CONFIG)
    assert not v.approved
    assert "cooldown" in v.reject_reason


@pytest.mark.parametrize("sentiment", ["BULL", "BEAR"])
def test_reject_position_already_open(sentiment: str) -> None:
    # One position per asset in either direction blocks a new entry.
    v = evaluate(make_signal(sentiment=sentiment), make_ctx(open_position_on_asset=True), CONFIG)
    assert not v.approved
    assert "already open" in v.reject_reason


# --- Ordering / helpers ---------------------------------------------------


def test_kill_switch_takes_priority_over_signal_quality() -> None:
    # Even a garbage signal is rejected for the kill switch first.
    bad = make_signal(confidence=0.0, intensity=1)
    v = evaluate(bad, make_ctx(kill_switch_active=True), CONFIG)
    assert v.reject_reason == "kill switch active"


def test_daily_loss_breached_helper() -> None:
    assert daily_loss_breached(make_ctx(daily_pnl_quote=-31.0), CONFIG)
    assert not daily_loss_breached(make_ctx(daily_pnl_quote=-29.0), CONFIG)


def test_config_from_settings() -> None:
    from app.config import Settings

    s = Settings(_env_file=None, paper_trading="true", exchange_sandbox="true")  # type: ignore[arg-type]
    cfg = RiskConfig.from_settings(s)
    assert cfg.confidence_threshold == s.confidence_threshold
    assert cfg.max_trades_per_hour == s.max_trades_per_hour
