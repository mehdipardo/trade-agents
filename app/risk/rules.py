"""Deterministic risk engine — pure functions only.

No LLM, no I/O, no Redis in this module: it takes a ``Signal`` plus an immutable
snapshot of the current risk state (``RiskContext``) and a ``RiskConfig``, and
returns a ``RiskVerdict``. All side effects (reading/writing counters, the kill
switch) live in the caller. This purity is what makes the module exhaustively
testable — the "production-grade" argument of the demo.

Futures-testnet model: shorting is allowed. BULL opens a long (buy), BEAR opens
a short (sell); one position per asset in either direction.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models.schemas import RiskVerdict, Signal


@dataclass(frozen=True)
class RiskConfig:
    """Tunable risk parameters (defaults mirror the brief)."""

    confidence_threshold: float = 0.6
    min_intensity: int = 3
    # Fraction of equity to risk per intensity bucket.
    sizing_by_intensity: dict[int, float] = field(
        default_factory=lambda: {3: 0.01, 4: 0.02, 5: 0.03}
    )
    max_notional_abs: float = 100.0  # USDT hard cap
    max_notional_equity_pct: float = 0.05  # <= 5% of equity
    stop_loss_pct: float = 1.5
    take_profit_pct: float = 3.0  # RR 1:2
    max_trades_per_hour: int = 6
    cooldown_s: int = 900  # 15 min per asset
    max_positions_per_asset: int = 1
    daily_loss_limit_pct: float = 0.03  # -3% equity -> kill switch

    @classmethod
    def from_settings(cls, settings: object) -> RiskConfig:
        """Build a RiskConfig from application settings."""
        return cls(
            confidence_threshold=settings.confidence_threshold,  # type: ignore[attr-defined]
            min_intensity=settings.min_intensity,  # type: ignore[attr-defined]
            max_notional_abs=settings.max_notional_abs,  # type: ignore[attr-defined]
            max_notional_equity_pct=settings.max_notional_equity_pct,  # type: ignore[attr-defined]
            stop_loss_pct=settings.stop_loss_pct,  # type: ignore[attr-defined]
            take_profit_pct=settings.take_profit_pct,  # type: ignore[attr-defined]
            max_trades_per_hour=settings.max_trades_per_hour,  # type: ignore[attr-defined]
            cooldown_s=settings.cooldown_s,  # type: ignore[attr-defined]
            daily_loss_limit_pct=settings.daily_loss_limit_pct,  # type: ignore[attr-defined]
        )


@dataclass(frozen=True)
class RiskContext:
    """Immutable snapshot of the state relevant to one signal's asset."""

    equity_quote: float
    trades_last_hour: int
    daily_pnl_quote: float  # realized PnL since the start of the UTC day
    kill_switch_active: bool
    asset_in_cooldown: bool
    open_position_on_asset: bool


def _reject(reason: str) -> RiskVerdict:
    return RiskVerdict(approved=False, reject_reason=reason)


def daily_loss_breached(ctx: RiskContext, config: RiskConfig) -> bool:
    """True when the daily loss cap has been hit (should latch the kill switch)."""
    limit = -abs(config.daily_loss_limit_pct) * ctx.equity_quote
    return ctx.daily_pnl_quote <= limit


def _position_size_quote(intensity: int, ctx: RiskContext, config: RiskConfig) -> float:
    pct = config.sizing_by_intensity.get(intensity, 0.0)
    raw = ctx.equity_quote * pct
    cap = min(config.max_notional_equity_pct * ctx.equity_quote, config.max_notional_abs)
    return min(raw, cap)


def evaluate(signal: Signal, ctx: RiskContext, config: RiskConfig) -> RiskVerdict:
    """Return the risk decision for ``signal`` given the current context.

    Rejections are checked in a deterministic order; the first failing rule
    produces the ``reject_reason``.
    """
    # --- Circuit breakers first ------------------------------------------
    if ctx.kill_switch_active:
        return _reject("kill switch active")
    if daily_loss_breached(ctx, config):
        return _reject("daily loss limit reached; kill switch")

    # --- Signal-quality gates --------------------------------------------
    if signal.asset is None:
        return _reject("no whitelisted asset")
    if signal.sentiment == "NEUTRAL":
        return _reject("neutral signal")
    if signal.confidence < config.confidence_threshold:
        return _reject(
            f"confidence {signal.confidence:.2f} < threshold {config.confidence_threshold:.2f}"
        )
    if signal.intensity < config.min_intensity:
        return _reject(f"intensity {signal.intensity} < min {config.min_intensity}")

    # --- Throughput / concurrency limits ---------------------------------
    if ctx.trades_last_hour >= config.max_trades_per_hour:
        return _reject(f"max trades/hour reached ({config.max_trades_per_hour})")
    if ctx.asset_in_cooldown:
        return _reject("asset in cooldown")

    # --- Side resolution (futures: shorts allowed) -----------------------
    # One position per asset, in either direction. BULL opens a long (buy),
    # BEAR opens a short (sell). SL/TP exits are handled by the position monitor.
    if ctx.open_position_on_asset:
        return _reject("position already open on asset")
    side = "buy" if signal.sentiment == "BULL" else "sell"

    # --- Sizing ----------------------------------------------------------
    size = _position_size_quote(signal.intensity, ctx, config)
    if size <= 0:
        return _reject("computed position size is zero")

    return RiskVerdict(
        approved=True,
        reject_reason=None,
        side=side,  # type: ignore[arg-type]
        position_size_quote=round(size, 2),
        stop_loss_pct=config.stop_loss_pct,
        take_profit_pct=config.take_profit_pct,
    )
