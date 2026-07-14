"""Public real-time price provider (read-only, no API keys).

Paper trading is only meaningful if positions are marked against REAL prices:
otherwise SL/TP never trigger and equity never moves. This module fetches live
mark prices from a public exchange endpoint (MEXC by default — the same venue we
paper-trade on, so symbols line up). It is strictly read-only market data: it
places no orders and therefore does not touch the paper-trading safety guards.

Robustness: prices are cached briefly (TTL) to avoid hammering the API, every
lookup degrades gracefully to ``None`` on any error (the caller then falls back
to a mock price), and the ccxt client is created lazily so the app runs even
when the library or network is unavailable.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.logging_config import get_logger

log = get_logger("app.services.prices")

_CACHE_TTL_S = 3.0

_client: Any | None = None
_client_failed = False
_cache: dict[str, tuple[float, float]] = {}  # symbol -> (price, epoch)
_last_source: dict[str, str] = {}  # symbol -> "binance" | "ccxt"
_exchange_id = "mexc"

# Binance public REST is the primary source for crypto: one GET, no markets to
# load, extremely reliable (and reachable from the Jakarta VPS). Map our internal
# BASE/QUOTE symbols to Binance tickers. Non-crypto (TradFi) falls through to ccxt.
_BINANCE_MAP = {
    "BTC/USDT": "BTCUSDT",
    "ETH/USDT": "ETHUSDT",
    "SOL/USDT": "SOLUSDT",
    "XRP/USDT": "XRPUSDT",
    "DOGE/USDT": "DOGEUSDT",
}
_BINANCE_URL = "https://api.binance.com/api/v3/ticker/price"


def configure(exchange_id: str) -> None:
    """Set which public exchange to price against (default ``mexc``)."""
    global _exchange_id
    _exchange_id = exchange_id


def set_client(client: Any | None) -> None:
    """Override the ccxt client (used by tests)."""
    global _client, _client_failed
    _client = client
    _client_failed = client is None


def reset_state() -> None:
    _cache.clear()


def _symbol_candidates(symbol: str) -> list[str]:
    """ccxt symbol forms to try for an internal ``BASE/QUOTE`` symbol.

    Linear perpetuals are ``BASE/QUOTE:QUOTE`` in ccxt; we also try the plain
    spot form as a fallback so a missing swap listing still resolves a price.
    """
    if ":" in symbol:
        return [symbol]
    quote = symbol.split("/")[-1] if "/" in symbol else "USDT"
    return [f"{symbol}:{quote}", symbol]


async def _get_client() -> Any | None:
    global _client, _client_failed
    if _client is not None or _client_failed:
        return _client
    try:
        import ccxt.async_support as ccxt

        klass = getattr(ccxt, _exchange_id, None)
        if klass is None:
            _client_failed = True
            return None
        # Public, mainnet (real prices), perpetuals. No keys, no sandbox: this is
        # read-only market data, never order placement.
        _client = klass({"enableRateLimit": True, "options": {"defaultType": "swap"}})
        return _client
    except Exception as exc:  # noqa: BLE001 - degrade to mock prices
        log.warning("price_client_unavailable", error=str(exc))
        _client_failed = True
        return None


async def _binance_price(symbol: str) -> float | None:
    """Fetch a crypto price from Binance public REST (or None)."""
    ticker = _BINANCE_MAP.get(symbol)
    if ticker is None:
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(_BINANCE_URL, params={"symbol": ticker})
            resp.raise_for_status()
            price = resp.json().get("price")
            return float(price) if price else None
    except Exception as exc:  # noqa: BLE001 - fall through to ccxt / mock
        log.debug("binance_price_failed", symbol=symbol, error=str(exc))
        return None


async def _ccxt_price(symbol: str) -> float | None:
    client = await _get_client()
    if client is None:
        return None
    for cand in _symbol_candidates(symbol):
        try:
            ticker = await client.fetch_ticker(cand)
        except Exception:  # noqa: BLE001 - try the next candidate / fall back
            continue
        price = ticker.get("last") or ticker.get("close")
        if price:
            return float(price)
    return None


async def get_price(symbol: str) -> float | None:
    """Return the live mark price for ``symbol`` (quote units), or ``None``.

    Crypto resolves via Binance REST first (fast, reliable); everything else
    (and any Binance miss) via ccxt on the configured exchange.
    """
    now = time.time()
    hit = _cache.get(symbol)
    if hit is not None and now - hit[1] < _CACHE_TTL_S:
        return hit[0]

    price = await _binance_price(symbol)
    source = "binance"
    if price is None:
        price = await _ccxt_price(symbol)
        source = "ccxt"

    if price is not None and price > 0:
        _cache[symbol] = (price, now)
        _last_source[symbol] = source
        return price
    log.warning("price_unresolved", symbol=symbol)
    return None


def last_source(symbol: str) -> str:
    """Which provider last resolved ``symbol`` ('binance'/'ccxt'/'mock')."""
    return _last_source.get(symbol, "mock")


async def warm() -> None:
    """Pre-create the client and load markets so the first trade isn't slow.

    ccxt loads the full market list on the first ticker call (can take a few
    seconds); doing it at startup keeps the executor's first price fetch fast.
    Best-effort: never raises.
    """
    client = await _get_client()
    if client is None:
        return
    try:
        await client.load_markets()
        log.info("price_provider_warmed", exchange_id=_exchange_id)
    except Exception as exc:  # noqa: BLE001 - warming is best-effort
        log.warning("price_warm_failed", error=str(exc))


async def close() -> None:
    global _client
    if _client is not None:
        try:
            await _client.close()
        except Exception:  # noqa: BLE001
            pass
        _client = None
