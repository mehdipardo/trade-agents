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

from dataclasses import dataclass

from app.models.schemas import RiskVerdict, Signal


@dataclass(frozen=True)
class RiskConfig:
    """Tunable risk parameters (defaults mirror the brief)."""

    confidence_threshold: float = 0.6
    min_intensity: int = 3
    min_actionability: int = 2
    # Risk-based sizing: fraction of equity lost if the SL is hit.
    risk_per_trade_pct: float = 0.01
    max_gross_exposure: float = 3.0  # notional cap as a multiple of equity
    margin_leverage: int = 5  # margin locked = notional / margin_leverage
    max_notional_abs: float = 100.0  # legacy (display only)
    max_notional_equity_pct: float = 0.05  # legacy (display only)
    stop_loss_pct: float = 1.5
    take_profit_pct: float = 3.0  # RR 1:2
    max_trades_per_hour: int = 6
    cooldown_s: int = 900  # 15 min per asset
    max_positions_per_asset: int = 1
    daily_loss_limit_pct: float = 0.03  # -3% equity -> kill switch
    # Runner + leverage boost (overlaid by the active Strategy).
    runner_pct: float = 0.0
    runner_tp_pct: float = 0.0
    high_impact_threshold: int = 8
    leverage_multiplier: int = 1

    @classmethod
    def from_settings(cls, settings: object, strategy: object | None = None) -> RiskConfig:
        """Build a RiskConfig from settings, optionally overlaying a Strategy.

        The strategy overrides SL/TP, sizing and signal-quality gates. Only the
        daily loss limit stays global — it's a safety guard, not a strategy knob.
        """
        source = strategy if strategy is not None else settings
        return cls(
            confidence_threshold=source.confidence_threshold,  # type: ignore[attr-defined]
            min_intensity=source.min_intensity,  # type: ignore[attr-defined]
            min_actionability=source.min_actionability,  # type: ignore[attr-defined]
            risk_per_trade_pct=getattr(source, "risk_per_trade_pct", 0.01),
            max_gross_exposure=getattr(
                source, "max_gross_exposure", getattr(settings, "max_gross_exposure", 3.0)
            ),
            margin_leverage=getattr(
                source, "margin_leverage", getattr(settings, "margin_leverage", 5)
            ),
            stop_loss_pct=source.stop_loss_pct,  # type: ignore[attr-defined]
            take_profit_pct=source.take_profit_pct,  # type: ignore[attr-defined]
            max_trades_per_hour=source.max_trades_per_hour,  # type: ignore[attr-defined]
            cooldown_s=source.cooldown_s,  # type: ignore[attr-defined]
            daily_loss_limit_pct=settings.daily_loss_limit_pct,  # type: ignore[attr-defined]
            runner_pct=getattr(source, "runner_pct", 0.0),
            runner_tp_pct=getattr(source, "runner_tp_pct", 0.0),
            high_impact_threshold=getattr(source, "high_impact_threshold", 8),
            leverage_multiplier=getattr(source, "leverage_multiplier", 1),
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
    free_capital_quote: float = 0.0  # equity minus margin already locked
    # Technical confluence score for the intended side (-2..+2), None when no
    # candle data is available (missing data NEVER penalizes a trade).
    technical_score: int | None = None
    # Operator-declared directional bias on the asset ("BULL"/"BEAR"), None if unset.
    operator_bias: str | None = None


def _reject(reason: str) -> RiskVerdict:
    return RiskVerdict(approved=False, reject_reason=reason)


def daily_loss_breached(ctx: RiskContext, config: RiskConfig) -> bool:
    """True when the daily loss cap has been hit (should latch the kill switch)."""
    limit = -abs(config.daily_loss_limit_pct) * ctx.equity_quote
    return ctx.daily_pnl_quote <= limit


def _risk_based_notional(
    risk_pct: float, sl_pct: float, ctx: RiskContext, config: RiskConfig
) -> float:
    """Notional sized so a stop-loss loses ``risk_pct`` of equity.

    notional = risk_dollars / (sl_pct/100), capped at ``max_gross_exposure`` x
    equity (a futures leverage ceiling).
    """
    if sl_pct <= 0:
        return 0.0
    risk_dollars = ctx.equity_quote * risk_pct
    notional = risk_dollars / (sl_pct / 100.0)
    cap = config.max_gross_exposure * ctx.equity_quote
    return min(notional, cap)


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
    if signal.actionability < config.min_actionability:
        return _reject(
            f"actionability {signal.actionability} < min {config.min_actionability}"
        )

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

    # --- Leverage boost on high-impact signals ---------------------------
    # A high impact_score risks MORE per trade (x the multiplier). SL/TP percents
    # stay fixed; sizing up the risk budget is what grows the position (which is
    # exactly what leverage means on futures: same stop distance, bigger size).
    leverage = 1
    if signal.impact_score >= config.high_impact_threshold and config.leverage_multiplier > 1:
        leverage = config.leverage_multiplier
    sl_pct = config.stop_loss_pct
    tp_pct = config.take_profit_pct

    # --- Confluence: technicals + operator bias ---------------------------
    # Philosophy: confluence adjusts SIZE, it rarely vetoes. A news signal
    # fighting the technicals gets a smaller risk budget; one confirmed by them
    # gets a modest boost. The only hard veto is a WEAK signal that is STRONGLY
    # counter-trend — the classic late chase into an exhausted move.
    confluence_notes: list[str] = []
    risk_multiplier = 1.0
    if ctx.operator_bias in ("BULL", "BEAR"):
        bias_aligned = (side == "buy") == (ctx.operator_bias == "BULL")
        if bias_aligned:
            confluence_notes.append("bias-aligned")
        else:
            risk_multiplier *= 0.5
            confluence_notes.append("counter-bias: risk halved")
    if ctx.technical_score is not None:
        s = ctx.technical_score
        if s <= -2 and signal.confidence < 0.75:
            return _reject(
                f"technicals strongly against (score {s}) and confidence "
                f"{signal.confidence:.2f} < 0.75"
            )
        if s < 0:
            risk_multiplier *= 0.6
            confluence_notes.append(f"counter-trend (score {s}): risk reduced")
        elif s >= 2:
            risk_multiplier *= 1.25
            confluence_notes.append(f"trend-confirmed (score +{s}): risk boosted")
        else:
            confluence_notes.append(f"technicals neutral (score {s:+d})")

    # --- Risk-based sizing ------------------------------------------------
    risk_pct = config.risk_per_trade_pct * leverage * risk_multiplier
    size_quote = _risk_based_notional(risk_pct, sl_pct, ctx, config)
    if size_quote <= 0:
        return _reject("computed position size is zero")

    # --- Margin & free-capital gate --------------------------------------
    # Leverage locks only notional/margin_leverage as margin, which is what frees
    # capital for other triggers. Reject only if even that margin is unavailable.
    margin_leverage = max(1, config.margin_leverage)
    margin_quote = size_quote / margin_leverage
    if margin_quote > ctx.free_capital_quote + 1e-9:
        return _reject(
            f"insufficient free margin (need {margin_quote:.2f}, "
            f"have {ctx.free_capital_quote:.2f})"
        )

    return RiskVerdict(
        approved=True,
        reject_reason=None,
        side=side,  # type: ignore[arg-type]
        position_size_quote=round(size_quote, 2),
        stop_loss_pct=sl_pct,
        take_profit_pct=tp_pct,
        runner_pct=config.runner_pct if config.runner_pct > 0 else None,
        runner_tp_pct=config.runner_tp_pct if config.runner_tp_pct > 0 else None,
        leverage=leverage,
        margin_leverage=margin_leverage,
        margin_quote=round(margin_quote, 2),
        confluence="; ".join(confluence_notes) or None,
    )
