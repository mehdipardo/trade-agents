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
    ctx = RiskContext(
        equity_quote=settings.starting_equity_quote,
        trades_last_hour=await store.trades_last_hour(),
        daily_pnl_quote=await store.daily_pnl(),
        kill_switch_active=await store.get_kill_switch(),
        asset_in_cooldown=await store.in_cooldown(asset),
        open_position_on_asset=await store.has_open_position(asset),
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
