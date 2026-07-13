"""Strategy presets — swap risk parameters at runtime.

Each ``Strategy`` overrides a subset of ``RiskConfig`` (SL/TP, sizing, gates,
throughput). The active strategy is persisted so it survives restarts. Only the
daily loss limit stays global (safety guard).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from app.services.store import get_store

DEFAULT_STRATEGY_ID = "balanced"


@dataclass(frozen=True)
class Strategy:
    id: str
    name: str
    description: str
    # Signal gates
    min_intensity: int
    min_actionability: int
    confidence_threshold: float
    # Risk-based sizing: fraction of equity risked at the SL. Notional is
    # derived: (equity * risk_per_trade_pct) / (stop_loss_pct/100).
    risk_per_trade_pct: float
    # SL / TP (percent, absolute values applied by the executor)
    stop_loss_pct: float
    take_profit_pct: float
    # Throughput
    max_trades_per_hour: int
    cooldown_s: int
    # Notional ceiling as a multiple of equity (futures leverage cap).
    max_gross_exposure: float = 3.0
    # Margin leverage: margin locked = notional / margin_leverage. Frees capital
    # for other triggers; SL/TP value is unchanged (they scale with notional).
    margin_leverage: int = 5
    # Runner mechanism: close (1 - runner_pct) at TP, keep runner_pct with SL
    # moved to breakeven, aiming at runner_tp_pct.
    runner_pct: float = 0.0
    runner_tp_pct: float = 0.0
    # High-impact boost: when Signal.impact_score >= threshold, the per-trade
    # risk budget (and thus the position size) is multiplied by
    # leverage_multiplier. SL/TP percents stay fixed.
    high_impact_threshold: int = 8
    leverage_multiplier: int = 1


STRATEGIES: dict[str, Strategy] = {
    "conservative": Strategy(
        id="conservative",
        name="Conservative",
        description="High-conviction only. Risks 0.5%/trade (~$5 on $1k). Tight SL.",
        min_intensity=4,
        min_actionability=4,
        confidence_threshold=0.75,
        risk_per_trade_pct=0.005,
        stop_loss_pct=1.0,
        take_profit_pct=2.0,
        max_trades_per_hour=3,
        cooldown_s=1800,
        max_gross_exposure=2.0,
        margin_leverage=3,
    ),
    "balanced": Strategy(
        id="balanced",
        name="Balanced",
        description="Default. Risks 1%/trade (~$10 on $1k). SL 1.5% / TP 3%. "
        "80% closes at TP, 20% runs to +50% (SL to entry).",
        min_intensity=3,
        min_actionability=2,
        confidence_threshold=0.60,
        risk_per_trade_pct=0.01,
        stop_loss_pct=1.5,
        take_profit_pct=3.0,
        max_trades_per_hour=6,
        cooldown_s=900,
        max_gross_exposure=3.0,
        margin_leverage=5,
        runner_pct=0.20,
        runner_tp_pct=50.0,
        high_impact_threshold=8,
        leverage_multiplier=3,
    ),
    "aggressive": Strategy(
        id="aggressive",
        name="Aggressive",
        description="Trades weaker signals. Risks 2%/trade (~$20 on $1k). Wider "
        "SL/TP, runner to +80%.",
        min_intensity=3,
        min_actionability=2,
        confidence_threshold=0.55,
        risk_per_trade_pct=0.02,
        stop_loss_pct=2.5,
        take_profit_pct=5.0,
        max_trades_per_hour=10,
        cooldown_s=300,
        max_gross_exposure=5.0,
        margin_leverage=10,
        runner_pct=0.25,
        runner_tp_pct=80.0,
        high_impact_threshold=8,
        leverage_multiplier=3,
    ),
    "scalp": Strategy(
        id="scalp",
        name="Scalp",
        description="Fast in/out on strong signals. Risks 0.5%/trade. Very tight SL/TP.",
        min_intensity=3,
        min_actionability=4,
        confidence_threshold=0.60,
        risk_per_trade_pct=0.005,
        stop_loss_pct=0.5,
        take_profit_pct=1.0,
        max_trades_per_hour=15,
        cooldown_s=120,
        max_gross_exposure=3.0,
        margin_leverage=10,
    ),
}


def list_strategies() -> list[dict]:
    """Return every strategy as a plain dict (JSON-serializable)."""
    return [asdict(s) for s in STRATEGIES.values()]


def get_strategy(strategy_id: str) -> Strategy:
    """Return a strategy by id, falling back to the default when unknown."""
    return STRATEGIES.get(strategy_id, STRATEGIES[DEFAULT_STRATEGY_ID])


async def get_active_strategy() -> Strategy:
    """Fetch the active strategy from the store (falls back to default)."""
    store = get_store()
    active_id = await store.get_strategy_id()
    return get_strategy(active_id or DEFAULT_STRATEGY_ID)


async def set_active_strategy(strategy_id: str) -> Strategy:
    """Persist a new active strategy. Raises ValueError on unknown id."""
    if strategy_id not in STRATEGIES:
        raise ValueError(f"unknown strategy: {strategy_id}")
    await get_store().set_strategy_id(strategy_id)
    return STRATEGIES[strategy_id]
