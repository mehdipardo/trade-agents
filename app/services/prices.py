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

from app.logging_config import get_logger

log = get_logger("app.services.prices")

_CACHE_TTL_S = 3.0

_client: Any | None = None
_client_failed = False
_cache: dict[str, tuple[float, float]] = {}  # symbol -> (price, epoch)
_exchange_id = "mexc"


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


async def get_price(symbol: str) -> float | None:
    """Return the live mark price for ``symbol`` (quote units), or ``None``."""
    now = time.time()
    hit = _cache.get(symbol)
    if hit is not None and now - hit[1] < _CACHE_TTL_S:
        return hit[0]

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
            _cache[symbol] = (float(price), now)
            return float(price)
    log.debug("price_unresolved", symbol=symbol)
    return None


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
