"""Risk node.

Assembles a ``RiskContext`` from the state store, delegates the decision to the
pure ``app.risk.rules.evaluate`` function, and latches the kill switch when the
daily loss cap is breached. No trading logic lives here beyond orchestration.
"""

from __future__ import annotations

from typing import Any

from app.api.ws import emit
from app.config import get_settings
from app.graph.state import TradingState
from app.graph.timing import timed_node
from app.risk.rules import RiskConfig, RiskContext, daily_loss_breached, evaluate
from app.services.store import get_store
from app.services.strategy import get_active_strategy


@timed_node("risk")
async def risk_node(state: TradingState) -> dict[str, Any]:
    signal = state["signal"]
    assert signal is not None  # routing guarantees a tradable signal here

    settings = get_settings()
    strategy = await get_active_strategy()
    config = RiskConfig.from_settings(settings, strategy)
    store = get_store()

    asset = signal.asset or ""
    # Free capital = equity + realized PnL - margin already locked by open
    # positions. Leverage keeps this from being exhausted, so multiple triggers
    # can run concurrently.
    equity = settings.starting_equity_quote + (await store.performance())["realized_total"]
    margin_locked = sum(
        float(p.get("margin_quote") or 0.0) for p in await store.open_positions()
    )
    # Confluence inputs: technical score for the intended side + operator bias.
    # Both best-effort — None (no candles / no bias) means no adjustment. A
    # scanner-emitted setup skips the gate: its own detection IS the technicals.
    side_guess = "buy" if signal.sentiment == "BULL" else "sell"
    technical_score: int | None = None
    if settings.technical_gate and asset and state["event"].source != "technical":
        from app.services.technicals import assess_symbol

        view = await assess_symbol(asset, side_guess)
        if view is not None:
            technical_score = view.score
    ctx = RiskContext(
        equity_quote=settings.starting_equity_quote,
        trades_last_hour=await store.trades_last_hour(),
        daily_pnl_quote=await store.daily_pnl(),
        kill_switch_active=await store.get_kill_switch(),
        asset_in_cooldown=await store.in_cooldown(asset),
        open_position_on_asset=await store.has_open_position(asset),
        free_capital_quote=max(0.0, equity - margin_locked),
        technical_score=technical_score,
        operator_bias=await store.get_bias(asset) if asset else None,
    )

    # Latch the kill switch on a daily-loss breach so it persists until reset.
    if daily_loss_breached(ctx, config) and not ctx.kill_switch_active:
        await store.set_kill_switch(True, reason="daily loss limit reached")

    verdict = evaluate(signal, ctx, config)
    await emit("risk_verdict", event_id=state["event"].id, payload=verdict.model_dump())
    return {
        "risk": verdict,
        "status": "received" if verdict.approved else "rejected_risk",
    }
