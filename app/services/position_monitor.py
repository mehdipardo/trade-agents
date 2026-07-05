"""Position monitor.

An asyncio task that polls the price of every open position and exits at market
when the stop-loss or take-profit is touched (no native OCO in the MVP). The
decision logic (``exit_reason``, ``realized_pnl``) is pure and unit-tested.

In offline mode (no exchange) prices come from a static mock so nothing
triggers; the loop still runs and is a no-op, which keeps the demo self-contained.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from app.logging_config import get_logger
from app.services.exchange import get_exchange
from app.services.store import get_store

log = get_logger("app.services.position_monitor")

ExitReason = Literal["stop_loss", "take_profit"]


def exit_reason(
    side: str, entry_price: float, current_price: float, sl_pct: float, tp_pct: float
) -> ExitReason | None:
    """Return the exit reason if SL/TP is touched for a long/short, else None."""
    if side == "buy":  # long
        if current_price <= entry_price * (1 - sl_pct / 100):
            return "stop_loss"
        if current_price >= entry_price * (1 + tp_pct / 100):
            return "take_profit"
    else:  # short (sell)
        if current_price >= entry_price * (1 + sl_pct / 100):
            return "stop_loss"
        if current_price <= entry_price * (1 - tp_pct / 100):
            return "take_profit"
    return None


def realized_pnl(side: str, entry_price: float, exit_price: float, amount: float) -> float:
    """Realized quote PnL for closing a position."""
    if side == "buy":
        return (exit_price - entry_price) * amount
    return (entry_price - exit_price) * amount


async def _current_price(symbol: str) -> float | None:
    ex = get_exchange()
    if ex is not None:
        try:
            return await ex.last_price(symbol)
        except Exception as exc:  # noqa: BLE001
            log.warning("price_poll_failed", symbol=symbol, error=str(exc))
            return None
    # Offline: static mock price (never triggers SL/TP).
    from app.graph.nodes.executor import _MOCK_PRICES

    return _MOCK_PRICES.get(symbol)


async def _check_position(position: dict) -> None:
    symbol = position["asset"]
    side = position["side"]
    entry = float(position["entry_price"])
    amount = float(position["amount"])
    sl = float(position["stop_loss_pct"])
    tp = float(position["take_profit_pct"])

    price = await _current_price(symbol)
    if price is None:
        return

    reason = exit_reason(side, entry, price, sl, tp)
    if reason is None:
        return

    store = get_store()
    ex = get_exchange()
    close_side = "sell" if side == "buy" else "buy"
    if ex is not None:
        from app.graph.nodes.executor import client_order_id

        try:
            await ex.create_market_order(
                symbol, close_side, amount, client_order_id(f"close-{symbol}-{entry}"),
                reduce_only=True,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("position_close_failed", symbol=symbol, error=str(exc))
            return

    pnl = realized_pnl(side, entry, price, amount)
    await store.set_position(symbol, is_open=False)
    await store.add_daily_pnl(pnl)
    log.info(
        "position_closed",
        symbol=symbol,
        reason=reason,
        side=side,
        entry_price=entry,
        exit_price=price,
        pnl_quote=round(pnl, 4),
    )


async def position_monitor_loop(poll_interval_s: float = 2.0) -> None:
    """Poll open positions and exit on SL/TP until cancelled."""
    log.info("position_monitor_started", poll_interval_s=poll_interval_s)
    try:
        while True:
            store = get_store()
            for position in await store.open_positions():
                try:
                    await _check_position(position)
                except Exception as exc:  # noqa: BLE001 - never kill the monitor
                    log.error("position_check_error", error=str(exc))
            await asyncio.sleep(poll_interval_s)
    except asyncio.CancelledError:
        log.info("position_monitor_stopped")
        raise
