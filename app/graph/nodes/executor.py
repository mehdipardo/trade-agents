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
    # Crypto
    "BTC/USDT": 60000.0,
    "ETH/USDT": 3000.0,
    "SOL/USDT": 150.0,
    "XRP/USDT": 0.6,
    "DOGE/USDT": 0.15,
    "BTC/USD:USD": 60000.0,
    "ETH/USD:USD": 3000.0,
    "SOL/USD:USD": 150.0,
    # TradFi (MEXC perpetuals)
    "GOLD/USDT": 4088.0,
    "SILVER/USDT": 52.0,
    "OIL/USDT": 73.6,
    "SPX/USDT": 6100.0,
    "BABA/USDT": 109.3,
    "TSLA/USDT": 350.0,
    "NVDA/USDT": 180.0,
    "AVGO/USDT": 391.0,
    "AAPL/USDT": 240.0,
    "MSFT/USDT": 440.0,
    "META/USDT": 700.0,
}


def client_order_id(event_id: str) -> str:
    """Deterministic idempotency key derived from the event id."""
    return f"fst-{abs(hash(event_id))}"


def _news_ref(event: Any) -> dict:
    """Compact reference to the news that triggered the trade (for the UI)."""
    return {
        "id": getattr(event, "id", None),
        "title": getattr(event, "title", None),
        "url": getattr(event, "url", None),
        "source": getattr(event, "source", None),
        "published_at": (
            event.published_at.isoformat()
            if getattr(event, "published_at", None) is not None
            else None
        ),
    }


def _position_detail(
    symbol: str,
    side: str,
    entry_price: float,
    amount: float,
    stop_loss_pct: float,
    take_profit_pct: float,
    runner_pct: float,
    runner_tp_pct: float,
    leverage: int,
    margin_leverage: int,
    impact_score: int,
    original_signal: dict | None,
    news: dict | None = None,
) -> dict:
    from app.services.position_monitor import sl_tp_prices

    sl_price, tp_price = sl_tp_prices(side, entry_price, stop_loss_pct, take_profit_pct)
    runner_amount = round(amount * runner_pct, 8) if runner_pct > 0 else 0.0
    main_amount = round(amount - runner_amount, 8)
    runner_tp_price = None
    if runner_pct > 0 and runner_tp_pct > 0:
        _, runner_tp_price = sl_tp_prices(side, entry_price, stop_loss_pct, runner_tp_pct)
        runner_tp_price = round(runner_tp_price, 8)
    notional = entry_price * amount
    margin_quote = round(notional / max(1, margin_leverage), 2)
    return {
        "symbol": symbol,
        "side": side,  # buy = long, sell = short
        "entry_price": entry_price,
        "amount": amount,
        "main_amount": main_amount,
        "runner_amount": runner_amount,
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "runner_pct": runner_pct,
        "runner_tp_pct": runner_tp_pct,
        "leverage": leverage,
        "margin_leverage": margin_leverage,
        "notional_quote": round(notional, 2),
        "margin_quote": margin_quote,
        "impact_score": impact_score,
        "stop_loss_price": round(sl_price, 8),
        "take_profit_price": round(tp_price, 8),
        "runner_tp_price": runner_tp_price,
        "phase": "main",  # "main" -> "runner" once TP1 hits
        "opened_at": datetime.now(UTC).isoformat(),
        "original_signal": original_signal or {},
        "news": news or {},
    }


async def _record(
    symbol: str,
    side: str,
    entry_price: float,
    amount: float,
    stop_loss_pct: float,
    take_profit_pct: float,
    runner_pct: float,
    runner_tp_pct: float,
    leverage: int,
    margin_leverage: int,
    impact_score: int,
    cooldown_s: int,
    original_signal: dict | None,
    news: dict | None = None,
) -> None:
    store = get_store()
    await store.record_trade(symbol, cooldown_s=cooldown_s)
    await store.set_position(
        symbol,
        is_open=True,
        detail=_position_detail(
            symbol, side, entry_price, amount, stop_loss_pct, take_profit_pct,
            runner_pct, runner_tp_pct, leverage, margin_leverage, impact_score,
            original_signal, news,
        ),
    )


async def _offline_price(symbol: str) -> tuple[float, str]:
    """Real public price for the paper fill (+ its source), or a mock fallback."""
    settings = get_settings()
    if settings.use_live_prices:
        from app.services.prices import get_price, last_source

        live = await get_price(symbol)
        if live is not None and live > 0:
            return live, last_source(symbol)
    return _MOCK_PRICES.get(symbol, 1.0), "mock"


async def _offline_fill(state: TradingState) -> dict[str, Any]:
    t0 = perf_counter()
    event, signal, risk = state["event"], state["signal"], state["risk"]
    symbol = signal.asset  # type: ignore[union-attr]
    settings = get_settings()
    price, price_source = await _offline_price(symbol)
    # In live mode, refuse to open at a fabricated mock price — a bad entry
    # corrupts the whole SL/TP lifecycle (the "-$44 stop at 60000" bug). Skip
    # the trade instead; the next matching signal can retry once prices recover.
    if price_source == "mock" and settings.use_live_prices:
        log.warning("paper_fill_skipped_no_live_price", symbol=symbol)
        return {"status": "failed", "error": f"no live price for {symbol}; skipped"}
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
    await _record(
        symbol, side, price, amount,
        risk.stop_loss_pct,  # type: ignore[union-attr]
        risk.take_profit_pct,  # type: ignore[union-attr]
        risk.runner_pct or 0.0,  # type: ignore[union-attr]
        risk.runner_tp_pct or 0.0,  # type: ignore[union-attr]
        risk.leverage or 1,  # type: ignore[union-attr]
        risk.margin_leverage or 1,  # type: ignore[union-attr]
        signal.impact_score,  # type: ignore[union-attr]
        settings.cooldown_s,
        signal.model_dump(),  # type: ignore[union-attr]
        _news_ref(event),
    )
    log.info(
        "paper_fill", event_id=event.id, symbol=symbol, side=side,
        amount=amount, price=price, price_source=price_source,
    )
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
    settings = get_settings()
    await _record(
        symbol, side, float(avg_price or price), float(filled),
        risk.stop_loss_pct,  # type: ignore[union-attr]
        risk.take_profit_pct,  # type: ignore[union-attr]
        risk.runner_pct or 0.0,  # type: ignore[union-attr]
        risk.runner_tp_pct or 0.0,  # type: ignore[union-attr]
        risk.leverage or 1,  # type: ignore[union-attr]
        risk.margin_leverage or 1,  # type: ignore[union-attr]
        signal.impact_score,  # type: ignore[union-attr]
        settings.cooldown_s,
        signal.model_dump(),  # type: ignore[union-attr]
        _news_ref(event),
    )
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
