"""Executor node.

Places a market order on the futures **sandbox** via CCXT when an exchange
client is configured; otherwise performs an offline paper fill so the demo runs
without keys. In both cases the order id is idempotent (derived from the event
id) so a redelivered event never creates a second order, and the executed
position (side, entry price, SL/TP) is recorded for the position monitor.
"""

from __future__ import annotations

from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential

from app.api.ws import emit
from app.config import get_settings
from app.graph.state import TradingState
from app.graph.timing import timed_node
from app.logging_config import get_logger
from app.models.schemas import OrderResult
from app.services.exchange import ExchangeClient, get_exchange
from app.services.store import get_store

log = get_logger("app.graph.executor")

# Mock reference prices (USDT) used by the offline paper fill.
_MOCK_PRICES = {
    "BTC/USDT": 60000.0,
    "ETH/USDT": 3000.0,
    "SOL/USDT": 150.0,
    "XRP/USDT": 0.6,
    "DOGE/USDT": 0.15,
    "BTC/USD:USD": 60000.0,
    "ETH/USD:USD": 3000.0,
    "SOL/USD:USD": 150.0,
}


def client_order_id(event_id: str) -> str:
    """Deterministic idempotency key derived from the event id."""
    return f"fst-{abs(hash(event_id))}"


def _position_detail(symbol: str, side: str, entry_price: float, amount: float) -> dict:
    from app.services.position_monitor import sl_tp_prices

    settings = get_settings()
    sl_price, tp_price = sl_tp_prices(
        side, entry_price, settings.stop_loss_pct, settings.take_profit_pct
    )
    return {
        "symbol": symbol,
        "side": side,  # buy = long, sell = short
        "entry_price": entry_price,
        "amount": amount,
        "stop_loss_pct": settings.stop_loss_pct,
        "take_profit_pct": settings.take_profit_pct,
        "stop_loss_price": round(sl_price, 8),
        "take_profit_price": round(tp_price, 8),
        "opened_at": datetime.now(UTC).isoformat(),
    }


async def _record(symbol: str, side: str, entry_price: float, amount: float) -> None:
    store = get_store()
    settings = get_settings()
    await store.record_trade(symbol, cooldown_s=settings.cooldown_s)
    await store.set_position(
        symbol, is_open=True, detail=_position_detail(symbol, side, entry_price, amount)
    )


async def _offline_fill(state: TradingState) -> dict[str, Any]:
    t0 = perf_counter()
    event, signal, risk = state["event"], state["signal"], state["risk"]
    symbol = signal.asset  # type: ignore[union-attr]
    price = _MOCK_PRICES.get(symbol, 1.0)
    amount = round(risk.position_size_quote / price, 8)  # type: ignore[union-attr]
    side = risk.side or "buy"  # type: ignore[union-attr]

    order = OrderResult(
        order_id=f"paper-{event.id[:8]}",
        client_order_id=client_order_id(event.id),
        symbol=symbol,
        side=side,
        amount=amount,
        avg_price=price,
        status="filled",
        exchange_latency_ms=int((perf_counter() - t0) * 1000),
    )
    await _record(symbol, side, price, amount)
    log.info("paper_fill", event_id=event.id, symbol=symbol, side=side, amount=amount)
    await emit("order", event_id=event.id, payload=order.model_dump())
    return {"order": order, "status": "executed"}


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=0.2, max=2), reraise=True)
async def _send_order(ex: ExchangeClient, symbol: str, side: str, amount: float, coid: str) -> dict:
    return await ex.create_market_order(symbol, side, amount, coid)


async def _live_fill(state: TradingState, ex: ExchangeClient) -> dict[str, Any]:
    t0 = perf_counter()
    event, signal, risk = state["event"], state["signal"], state["risk"]
    symbol = signal.asset  # type: ignore[union-attr]
    side = risk.side or "buy"  # type: ignore[union-attr]
    coid = client_order_id(event.id)

    price = await ex.last_price(symbol)
    amount = ex.amount_to_precision(symbol, risk.position_size_quote / price)  # type: ignore[union-attr]

    try:
        raw = await _send_order(ex, symbol, side, amount, coid)
    except Exception as exc:  # noqa: BLE001
        # If the order may have gone through, verify by client id before failing.
        existing = await ex.fetch_order_by_client_id(coid, symbol)
        if existing is None:
            log.error("order_failed", event_id=event.id, error=str(exc))
            return {"status": "failed", "error": f"executor: {exc}"}
        raw = existing

    avg_price = raw.get("average") or raw.get("price") or price
    filled = raw.get("filled") or amount
    order = OrderResult(
        order_id=str(raw.get("id")) if raw.get("id") else None,
        client_order_id=coid,
        symbol=symbol,
        side=side,
        amount=float(filled),
        avg_price=float(avg_price) if avg_price else None,
        status="filled" if raw.get("status") in ("closed", "filled") else "open",
        exchange_latency_ms=int((perf_counter() - t0) * 1000),
    )
    await _record(symbol, side, float(avg_price or price), float(filled))
    log.info("live_fill", event_id=event.id, symbol=symbol, side=side, amount=order.amount)
    await emit("order", event_id=event.id, payload=order.model_dump())
    return {"order": order, "status": "executed"}


@timed_node("executor")
async def executor_node(state: TradingState) -> dict[str, Any]:
    signal, risk = state["signal"], state["risk"]
    assert signal is not None and signal.asset is not None
    assert risk is not None and risk.position_size_quote is not None

    ex = get_exchange()
    if ex is None:
        return await _offline_fill(state)
    return await _live_fill(state, ex)
