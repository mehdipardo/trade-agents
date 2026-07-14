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
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    # Stub the Binance HTTP path off by default so ccxt-path tests are hermetic
    # (no network). Individual tests re-patch it to exercise the Binance path.
    async def _no_binance(symbol: str):  # noqa: ANN202
        return None

    monkeypatch.setattr(prices, "_binance_price", _no_binance)
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


async def test_binance_is_tried_first_for_crypto(monkeypatch) -> None:
    async def _fake_binance(symbol: str):
        return 62345.0 if symbol == "BTC/USDT" else None

    monkeypatch.setattr(prices, "_binance_price", _fake_binance)
    # ccxt should never be consulted when Binance resolves.
    prices.set_client(_FakeClient({}))
    assert await prices.get_price("BTC/USDT") == 62345.0
    assert prices.last_source("BTC/USDT") == "binance"


async def test_falls_back_to_ccxt_when_binance_misses() -> None:
    # NVDA isn't a Binance crypto symbol -> ccxt path.
    prices.set_client(_FakeClient({"NVDA/USDT": 180.0}))
    assert await prices.get_price("NVDA/USDT") == 180.0
    assert prices.last_source("NVDA/USDT") == "ccxt"


async def test_trade_ledger_records_and_reads() -> None:
    from app.services.store import InMemoryStore, set_store
    set_store(InMemoryStore())
    from app.services.store import get_store
    s = get_store()
    assert await s.closed_trades() == []
    await s.record_trade_close({"symbol": "BTC/USDT", "leg": "main_tp", "pnl_quote": 14.7})
    await s.record_trade_close({"symbol": "BTC/USDT", "leg": "runner", "pnl_quote": -0.05})
    trades = await s.closed_trades()
    assert len(trades) == 2
    assert trades[0]["leg"] == "runner"  # newest first
