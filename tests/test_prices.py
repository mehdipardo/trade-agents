"""Public price provider: symbol resolution, caching, graceful fallback."""

from __future__ import annotations

import pytest

from app.services import prices


class _FakeClient:
    """Stand-in for a ccxt async client: resolves only known symbols."""

    def __init__(self, tickers: dict[str, float], *, fail: set[str] | None = None) -> None:
        self._t = tickers
        self._fail = fail or set()
        self.calls = 0

    async def fetch_ticker(self, symbol: str) -> dict:
        self.calls += 1
        if symbol in self._fail or symbol not in self._t:
            raise ValueError(f"no market {symbol}")
        return {"last": self._t[symbol]}


@pytest.fixture(autouse=True)
def _reset() -> None:
    prices.reset_state()
    prices.set_client(None)
    yield
    prices.reset_state()
    prices.set_client(None)


def test_symbol_candidates() -> None:
    assert prices._symbol_candidates("BTC/USDT") == ["BTC/USDT:USDT", "BTC/USDT"]
    assert prices._symbol_candidates("BTC/USDT:USDT") == ["BTC/USDT:USDT"]


async def test_get_price_resolves_swap_symbol() -> None:
    prices.set_client(_FakeClient({"BTC/USDT:USDT": 62000.0}))
    assert await prices.get_price("BTC/USDT") == 62000.0


async def test_get_price_falls_back_to_spot_form() -> None:
    prices.set_client(_FakeClient({"NVDA/USDT": 180.0}))  # only spot form resolves
    assert await prices.get_price("NVDA/USDT") == 180.0


async def test_get_price_none_when_unresolved() -> None:
    prices.set_client(_FakeClient({}))
    assert await prices.get_price("BTC/USDT") is None


async def test_get_price_none_when_no_client() -> None:
    prices.set_client(None)  # forces _client_failed -> no ccxt
    assert await prices.get_price("BTC/USDT") is None


async def test_price_is_cached_within_ttl() -> None:
    client = _FakeClient({"BTC/USDT:USDT": 62000.0})
    prices.set_client(client)
    await prices.get_price("BTC/USDT")
    await prices.get_price("BTC/USDT")
    # Second call served from cache -> only the first hit the client.
    assert client.calls == 1
