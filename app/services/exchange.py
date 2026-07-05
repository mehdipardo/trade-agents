"""CCXT exchange wrapper — FUTURES testnet, sandbox forced.

The agents must be able to SHORT, which spot markets cannot do, so the target is
a futures/perpetuals testnet. Binance Futures is geo-blocked in France, so the
default is **Kraken Futures** (CCXT id ``krakenfutures``, demo environment via
``set_sandbox_mode(True)``); MEXC is a documented alternative.

Hard guards: the client is only ever created with ``PAPER_TRADING=true`` and
``EXCHANGE_SANDBOX=true``, and ``set_sandbox_mode(True)`` is always applied.
There is no code path to a production exchange.

When no API key/secret is configured the factory returns ``None`` and the
executor falls back to an offline paper fill, so the demo runs without keys.
"""

from __future__ import annotations

from typing import Any

from app.config import Settings
from app.logging_config import get_logger

log = get_logger("app.services.exchange")


class ExchangeClient:
    """Thin async wrapper around a CCXT futures exchange in sandbox mode."""

    def __init__(self, exchange: Any, quote_ccy: str = "USDT") -> None:
        self._ex = exchange
        self.quote_ccy = quote_ccy

    @property
    def id(self) -> str:
        return self._ex.id

    async def last_price(self, symbol: str) -> float:
        ticker = await self._ex.fetch_ticker(symbol)
        price = ticker.get("last") or ticker.get("close")
        if not price:
            raise ValueError(f"no last price for {symbol}")
        return float(price)

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        return float(self._ex.amount_to_precision(symbol, amount))

    async def create_market_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        client_order_id: str,
        *,
        reduce_only: bool = False,
    ) -> dict:
        params: dict[str, Any] = {"clientOrderId": client_order_id}
        if reduce_only:
            params["reduceOnly"] = True
        return await self._ex.create_order(symbol, "market", side, amount, None, params)

    async def fetch_order_by_client_id(self, client_order_id: str, symbol: str) -> dict | None:
        """Best-effort lookup by client order id (used after a send timeout)."""
        try:
            orders = await self._ex.fetch_orders(symbol)
        except Exception as exc:  # noqa: BLE001 - lookup is best-effort
            log.warning("fetch_orders_failed", error=str(exc))
            return None
        for order in orders:
            if order.get("clientOrderId") == client_order_id:
                return order
        return None

    async def equity_quote(self) -> float | None:
        try:
            balance = await self._ex.fetch_balance()
        except Exception as exc:  # noqa: BLE001
            log.warning("fetch_balance_failed", error=str(exc))
            return None
        entry = balance.get(self.quote_ccy) or {}
        total = entry.get("total")
        return float(total) if total is not None else None

    async def close(self) -> None:
        try:
            await self._ex.close()
        except Exception:  # noqa: BLE001
            pass


_exchange: ExchangeClient | None = None


async def init_exchange(settings: Settings) -> ExchangeClient | None:
    """Create the exchange client (sandbox forced), or ``None`` when key-less."""
    global _exchange

    if not settings.exchange_api_key or not settings.exchange_secret:
        log.info("exchange_offline_mode", reason="no api key configured")
        _exchange = None
        return None

    # Non-negotiable guards before we ever construct a client.
    if not (settings.paper_trading and settings.exchange_sandbox):
        raise RuntimeError(
            "refusing to build exchange: PAPER_TRADING/EXCHANGE_SANDBOX must be true"
        )

    try:
        import ccxt.async_support as ccxt
    except ImportError:
        log.warning("ccxt_not_installed_offline_mode")
        _exchange = None
        return None

    klass = getattr(ccxt, settings.exchange_id, None)
    if klass is None:
        raise RuntimeError(f"unknown exchange id: {settings.exchange_id}")

    exchange = klass(
        {
            "apiKey": settings.exchange_api_key,
            "secret": settings.exchange_secret,
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},  # perpetual futures
        }
    )
    exchange.set_sandbox_mode(True)  # ALWAYS sandbox/testnet
    await exchange.load_markets()

    _exchange = ExchangeClient(exchange)
    log.info("exchange_ready", exchange_id=settings.exchange_id, sandbox=True)
    return _exchange


def get_exchange() -> ExchangeClient | None:
    """Return the exchange client singleton (``None`` in offline mode)."""
    return _exchange


def set_exchange(client: ExchangeClient | None) -> None:
    """Override the singleton (used by tests)."""
    global _exchange
    _exchange = client
