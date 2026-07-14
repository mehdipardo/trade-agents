"""Position monitor.

Polls the price of every open position and manages the two-phase exit:

- Phase "main": watch SL and TP. On TP hit, close the main portion (typically
  80%) and transition the remainder to "runner" phase with SL moved to entry
  (breakeven). On SL hit, close the whole position.
- Phase "runner": watch the runner's SL (entry price) and the runner TP
  (typically +50%). Whichever fires first closes the runner.

When an SL hits (main or runner), fire an LLM post-mortem so the operator can
read a critique of what went wrong.

Decision helpers are pure and unit-tested. In offline mode prices come from a
static mock so nothing triggers; the loop still runs (no-op) which keeps the
demo self-contained.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Literal

from app.logging_config import get_logger
from app.services.exchange import get_exchange
from app.services.store import get_store

log = get_logger("app.services.position_monitor")

ExitReason = Literal["stop_loss", "take_profit", "runner_stop", "runner_take_profit"]


def exit_reason(
    side: str, entry_price: float, current_price: float, sl_pct: float, tp_pct: float
) -> Literal["stop_loss", "take_profit"] | None:
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


def sl_tp_prices(
    side: str, entry_price: float, sl_pct: float, tp_pct: float
) -> tuple[float, float]:
    """Absolute (stop_loss, take_profit) price levels for a long/short."""
    if side == "buy":  # long
        return entry_price * (1 - sl_pct / 100), entry_price * (1 + tp_pct / 100)
    return entry_price * (1 + sl_pct / 100), entry_price * (1 - tp_pct / 100)


def realized_pnl(side: str, entry_price: float, exit_price: float, amount: float) -> float:
    """Gross realized quote PnL for closing a position (before fees)."""
    if side == "buy":
        return (exit_price - entry_price) * amount
    return (entry_price - exit_price) * amount


def round_trip_fee(
    entry_price: float, exit_price: float, amount: float, fee_pct: float
) -> float:
    """Total taker fee (quote) charged on both the entry and the exit fill.

    Fees are a percent of NOTIONAL (price x amount) on each side, so a round
    trip costs entry_notional*fee + exit_notional*fee.
    """
    rate = fee_pct / 100.0
    return (entry_price * amount + exit_price * amount) * rate


def net_realized_pnl(
    side: str, entry_price: float, exit_price: float, amount: float, fee_pct: float
) -> float:
    """Realized quote PnL net of round-trip taker fees."""
    gross = realized_pnl(side, entry_price, exit_price, amount)
    return gross - round_trip_fee(entry_price, exit_price, amount, fee_pct)


def runner_exit_reason(
    side: str, entry_price: float, current_price: float, runner_tp_pct: float
) -> Literal["runner_stop", "runner_take_profit"] | None:
    """Runner exit: SL is at breakeven (entry), TP is runner_tp_pct."""
    if side == "buy":
        if current_price <= entry_price:
            return "runner_stop"
        if current_price >= entry_price * (1 + runner_tp_pct / 100):
            return "runner_take_profit"
    else:
        if current_price >= entry_price:
            return "runner_stop"
        if current_price <= entry_price * (1 - runner_tp_pct / 100):
            return "runner_take_profit"
    return None


async def _current_price(symbol: str) -> float | None:
    ex = get_exchange()
    if ex is not None:
        try:
            return await ex.last_price(symbol)
        except Exception as exc:  # noqa: BLE001
            log.warning("price_poll_failed", symbol=symbol, error=str(exc))
            return None
    from app.config import get_settings

    # Live mode: mark against REAL prices ONLY. If a price is momentarily
    # unavailable, return None so the caller SKIPS this poll — never fall back to
    # a static mock, which would fabricate a huge move and fire a false SL/TP.
    if get_settings().use_live_prices:
        from app.services.prices import get_price

        live = await get_price(symbol)
        if live is not None and live > 0:
            return live
        log.warning("price_unavailable_skip_poll", symbol=symbol)
        return None
    # Offline/test mode only: a static mock reference (never triggers SL/TP).
    from app.graph.nodes.executor import _MOCK_PRICES

    return _MOCK_PRICES.get(symbol)


async def _close_market(symbol: str, close_side: str, amount: float, tag: str) -> bool:
    """Send a reduce-only close (live), or do nothing (offline). Returns True on success."""
    ex = get_exchange()
    if ex is None:
        return True
    from app.graph.nodes.executor import client_order_id

    try:
        await ex.create_market_order(
            symbol, close_side, amount, client_order_id(tag), reduce_only=True
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("position_close_failed", symbol=symbol, error=str(exc), tag=tag)
        return False


async def _finalize_close(
    symbol: str,
    reason: ExitReason,
    position: dict,
    exit_price: float,
    amount_closed: float,
    is_full_close: bool,
) -> None:
    """Book PnL (net of fees), update the store, fire the LLM critique on SL."""
    from app.config import get_settings

    store = get_store()
    side = position["side"]
    entry = float(position["entry_price"])
    fee_pct = get_settings().taker_fee_pct
    fee = round_trip_fee(entry, exit_price, amount_closed, fee_pct)
    pnl = realized_pnl(side, entry, exit_price, amount_closed) - fee
    await store.add_daily_pnl(pnl)
    # A full close counts as one closed trade for win-rate; a partial (TP1) only
    # contributes its realized PnL to the lifetime total.
    await store.bump_realized(pnl, closed=is_full_close, win=pnl > 0)

    if is_full_close:
        await store.set_position(symbol, is_open=False)

    # Which leg closed: TP1 closes the 80% main leg; the rest are full/runner exits.
    leg = "main_tp" if (reason == "take_profit" and not is_full_close) else (
        "runner" if reason in ("runner_stop", "runner_take_profit") else "full"
    )
    await store.record_trade_close({
        "ts": datetime.now(UTC).isoformat(),
        "symbol": symbol,
        "side": side,
        "leg": leg,
        "reason": reason,
        "entry_price": entry,
        "exit_price": round(exit_price, 8),
        "amount": amount_closed,
        "pnl_quote": round(pnl, 4),
        "fee_quote": round(fee, 4),
        "opened_at": position.get("opened_at"),
    })
    log.info(
        "position_leg_closed",
        symbol=symbol,
        reason=reason,
        side=side,
        entry_price=entry,
        exit_price=exit_price,
        amount=amount_closed,
        fee_quote=round(fee, 4),
        pnl_quote=round(pnl, 4),
        full_close=is_full_close,
    )
    # Async LLM post-mortem on any SL hit (never blocks the monitor).
    if reason in ("stop_loss", "runner_stop"):
        asyncio.create_task(_run_critique(symbol, reason, position, exit_price, pnl))


async def _run_critique(
    symbol: str, reason: ExitReason, position: dict, exit_price: float, pnl: float
) -> None:
    """Ask the LLM why this stop hit; store the report. Never raises."""
    try:
        from app.services.critique import generate_and_store_critique

        await generate_and_store_critique(symbol, reason, position, exit_price, pnl)
    except Exception as exc:  # noqa: BLE001 - critiques are best-effort
        log.warning("critique_failed", symbol=symbol, error=str(exc))


async def _handle_main_phase(position: dict, price: float) -> None:
    side = position["side"]
    entry = float(position["entry_price"])
    main_amount = float(position.get("main_amount") or position["amount"])
    runner_amount = float(position.get("runner_amount") or 0.0)
    sl_pct = float(position["stop_loss_pct"])
    tp_pct = float(position["take_profit_pct"])

    reason = exit_reason(side, entry, price, sl_pct, tp_pct)
    if reason is None:
        return

    symbol = position["asset"]
    close_side = "sell" if side == "buy" else "buy"

    if reason == "stop_loss":
        total = main_amount + runner_amount
        if not await _close_market(symbol, close_side, total, f"sl-{symbol}-{entry}"):
            return
        await _finalize_close(symbol, "stop_loss", position, price, total, is_full_close=True)
        return

    # TP hit — close the main leg. If there's no runner leg, close everything.
    if runner_amount <= 0:
        if not await _close_market(symbol, close_side, main_amount, f"tp-{symbol}-{entry}"):
            return
        await _finalize_close(
            symbol, "take_profit", position, price, main_amount, is_full_close=True
        )
        return

    if not await _close_market(symbol, close_side, main_amount, f"tp1-{symbol}-{entry}"):
        return
    await _finalize_close(
        symbol, "take_profit", position, price, main_amount, is_full_close=False
    )

    # Transition the remaining position to runner phase: SL moves to entry
    # (breakeven), only the runner amount remains.
    updated = {
        **position,
        "amount": runner_amount,
        "main_amount": 0.0,
        "runner_amount": runner_amount,
        "phase": "runner",
        "stop_loss_price": entry,
        "tp1_price": price,
    }
    await get_store().set_position(symbol, is_open=True, detail=updated)
    log.info(
        "runner_armed",
        symbol=symbol,
        entry_price=entry,
        runner_tp_price=position.get("runner_tp_price"),
        amount=runner_amount,
    )


async def _handle_runner_phase(position: dict, price: float) -> None:
    side = position["side"]
    entry = float(position["entry_price"])
    runner_tp_pct = float(position.get("runner_tp_pct") or 0.0)
    amount = float(position.get("runner_amount") or position["amount"])

    reason = runner_exit_reason(side, entry, price, runner_tp_pct)
    if reason is None:
        return

    symbol = position["asset"]
    close_side = "sell" if side == "buy" else "buy"
    if not await _close_market(symbol, close_side, amount, f"runner-{symbol}-{entry}"):
        return
    await _finalize_close(symbol, reason, position, price, amount, is_full_close=True)


async def _check_position(position: dict) -> None:
    symbol = position["asset"]
    price = await _current_price(symbol)
    if price is None:
        return
    if position.get("phase") == "runner":
        await _handle_runner_phase(position, price)
    else:
        await _handle_main_phase(position, price)


async def close_position_manually(symbol: str) -> dict[str, Any] | None:
    """Force-close a position at market. Returns the exit report or None."""
    store = get_store()
    positions = {p["asset"]: p for p in await store.open_positions()}
    position = positions.get(symbol)
    if position is None:
        return None
    price = await _current_price(symbol)
    if price is None:
        return None
    amount = float(position.get("amount") or 0)
    close_side = "sell" if position["side"] == "buy" else "buy"
    if not await _close_market(symbol, close_side, amount, f"manual-{symbol}"):
        return None
    from app.config import get_settings

    entry = float(position["entry_price"])
    fee = round_trip_fee(entry, price, amount, get_settings().taker_fee_pct)
    pnl = realized_pnl(position["side"], entry, price, amount) - fee
    await store.add_daily_pnl(pnl)
    await store.bump_realized(pnl, closed=True, win=pnl > 0)
    await store.set_position(symbol, is_open=False)
    await store.record_trade_close({
        "ts": datetime.now(UTC).isoformat(),
        "symbol": symbol,
        "side": position["side"],
        "leg": "manual",
        "reason": "manual_close",
        "entry_price": entry,
        "exit_price": round(price, 8),
        "amount": amount,
        "pnl_quote": round(pnl, 4),
        "fee_quote": round(fee, 4),
        "opened_at": position.get("opened_at"),
    })
    log.info(
        "position_closed_manually",
        symbol=symbol, exit_price=price, fee_quote=round(fee, 4), pnl_quote=round(pnl, 4),
    )
    return {
        "symbol": symbol, "exit_price": price,
        "fee_quote": round(fee, 4), "pnl_quote": round(pnl, 4),
    }


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
