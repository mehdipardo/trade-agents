"""Executor node.

Étape 2: a FAKE-FILL mock. It builds a realistic ``OrderResult`` (idempotent
client order id derived from the event id) without touching any exchange. The
CCXT sandbox executor replaces ``_fake_fill`` in Étape 5.
"""

from __future__ import annotations

from time import perf_counter
from typing import Any

from app.config import get_settings
from app.graph.state import TradingState
from app.graph.timing import timed_node
from app.models.schemas import OrderResult
from app.services.store import get_store

# Mock reference prices (USDT) used to convert notional -> base amount.
_MOCK_PRICES = {
    "BTC/USDT": 60000.0,
    "ETH/USDT": 3000.0,
    "SOL/USDT": 150.0,
    "XRP/USDT": 0.6,
    "DOGE/USDT": 0.15,
}


def client_order_id(event_id: str) -> str:
    """Deterministic idempotency key derived from the event id."""
    return f"fst-{abs(hash(event_id))}"


@timed_node("executor")
async def executor_node(state: TradingState) -> dict[str, Any]:
    t0 = perf_counter()
    event = state["event"]
    signal = state["signal"]
    risk = state["risk"]
    assert signal is not None and signal.asset is not None
    assert risk is not None and risk.position_size_quote is not None

    price = _MOCK_PRICES.get(signal.asset, 1.0)
    amount = risk.position_size_quote / price

    side = risk.side or "buy"
    order = OrderResult(
        order_id=f"mock-{event.id[:8]}",
        client_order_id=client_order_id(event.id),
        symbol=signal.asset,
        side=side,
        amount=round(amount, 8),
        avg_price=price,
        status="filled",
        exchange_latency_ms=int((perf_counter() - t0) * 1000),
    )

    # Record the executed trade so risk counters/cooldowns/positions advance.
    store = get_store()
    settings = get_settings()
    await store.record_trade(signal.asset, cooldown_s=settings.cooldown_s)
    # buy opens the single position; sell closes it.
    await store.set_position(signal.asset, is_open=(side == "buy"))

    return {"order": order, "status": "executed"}
