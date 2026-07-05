"""Étape 4 tests: in-memory store + risk-node integration (kill switch)."""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.services.store import InMemoryStore, set_store


@pytest.fixture(autouse=True)
def _safe_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("EXCHANGE_SANDBOX", "true")
    get_settings.cache_clear()
    set_store(InMemoryStore())
    yield
    get_settings.cache_clear()
    set_store(None)


async def test_record_trade_advances_counter_and_cooldown() -> None:
    store = InMemoryStore()
    assert await store.trades_last_hour() == 0
    await store.record_trade("BTC/USDT", cooldown_s=900)
    assert await store.trades_last_hour() == 1
    assert await store.in_cooldown("BTC/USDT")
    assert not await store.in_cooldown("ETH/USDT")


async def test_positions_open_and_close() -> None:
    store = InMemoryStore()
    assert not await store.has_open_position("SOL/USDT")
    await store.set_position("SOL/USDT", is_open=True)
    assert await store.has_open_position("SOL/USDT")
    await store.set_position("SOL/USDT", is_open=False)
    assert not await store.has_open_position("SOL/USDT")


async def test_kill_switch_toggle() -> None:
    store = InMemoryStore()
    assert not await store.get_kill_switch()
    await store.set_kill_switch(True, reason="manual")
    assert await store.get_kill_switch()
    await store.set_kill_switch(False)
    assert not await store.get_kill_switch()


async def test_dedup_seen_before() -> None:
    store = InMemoryStore()
    assert await store.seen_before("k1", ttl_s=60) is False
    assert await store.seen_before("k1", ttl_s=60) is True
    assert await store.seen_before("k2", ttl_s=60) is False


async def test_daily_pnl_accumulates() -> None:
    store = InMemoryStore()
    await store.add_daily_pnl(-10.0)
    await store.add_daily_pnl(-5.0)
    assert await store.daily_pnl() == pytest.approx(-15.0)


async def test_risk_node_latches_kill_switch_on_daily_loss() -> None:
    from app.graph.nodes.risk import risk_node
    from app.graph.state import initial_state
    from app.ingestion.simulator import load_scenario
    from app.services.store import get_store

    store = get_store()
    # Equity default is 1000; push daily PnL below -3%.
    await store.add_daily_pnl(-50.0)

    state = initial_state(load_scenario("trump_btc_bull"))
    # Provide a tradable signal as if analyst had run.
    from app.services.llm import offline_keyword_classify

    state["signal"] = offline_keyword_classify(state["event"], get_settings())

    update = await risk_node(state)
    assert update["status"] == "rejected_risk"
    assert await store.get_kill_switch() is True
