"""Purge of false SL/TP journal rows fabricated by the fixed mock-price bug."""

from __future__ import annotations

from app.services.store import (
    InMemoryStore,
    _counter_reversal,
    _is_mock_exit,
    set_store,
)

MOCK = {"BTC/USDT": 60000.0, "ETH/USDT": 3000.0}


def test_is_mock_exit_matches_only_mock_price() -> None:
    assert _is_mock_exit({"symbol": "BTC/USDT", "exit_price": 60000.0}, MOCK)
    # A real exit near — but not equal to — the mock is NOT a false SL.
    assert not _is_mock_exit({"symbol": "BTC/USDT", "exit_price": 64275.0}, MOCK)
    assert not _is_mock_exit({"symbol": "BTC/USDT", "exit_price": 60050.0}, MOCK)
    # Unknown symbol / missing fields never match.
    assert not _is_mock_exit({"symbol": "GOLD/USDT", "exit_price": 60000.0}, MOCK)
    assert not _is_mock_exit({"exit_price": 60000.0}, MOCK)


def test_counter_reversal_counts_full_closes_only() -> None:
    removed = [
        {"leg": "full", "pnl_quote": -44.0},   # false SL (full close, loss)
        {"leg": "main_tp", "pnl_quote": 5.0},  # partial: realized only, not a close
        {"leg": "runner", "pnl_quote": 8.0},   # full close, win
    ]
    realized, closed, wins = _counter_reversal(removed)
    assert realized == -31.0  # -44 + 5 + 8
    assert closed == 2  # full + runner
    assert wins == 1  # runner was a win


async def test_purge_removes_false_sls_and_fixes_counters() -> None:
    store = InMemoryStore()
    set_store(store)

    # A real winning trade, plus a false SL fabricated at the mock price.
    await store.record_trade_close(
        {"symbol": "BTC/USDT", "leg": "full", "reason": "take_profit",
         "entry_price": 64000.0, "exit_price": 65920.0, "pnl_quote": 12.0}
    )
    await store.bump_realized(12.0, closed=True, win=True)
    await store.record_trade_close(
        {"symbol": "BTC/USDT", "leg": "full", "reason": "stop_loss",
         "entry_price": 64275.0, "exit_price": 60000.0, "pnl_quote": -44.0}
    )
    await store.bump_realized(-44.0, closed=True, win=False)
    await store.record_critique(
        {"symbol": "BTC/USDT", "reason": "stop_loss", "exit_price": 60000.0,
         "critique": "phantom loss"}
    )
    await store.record_critique(
        {"symbol": "ETH/USDT", "reason": "stop_loss", "exit_price": 2850.0,
         "critique": "real loss"}
    )

    before = await store.performance()
    assert before == {"realized_total": -32.0, "closed_trades": 2, "wins": 1}

    result = await store.purge_mock_journal(MOCK)
    assert result["trades_removed"] == 1
    assert result["critiques_removed"] == 1

    # The real trade and real critique survive; the phantom ones are gone.
    trades = await store.closed_trades()
    assert len(trades) == 1 and trades[0]["exit_price"] == 65920.0
    crits = await store.critiques()
    assert len(crits) == 1 and crits[0]["critique"] == "real loss"

    # Counters rolled back to reflect only the real winning trade.
    after = await store.performance()
    assert after == {"realized_total": 12.0, "closed_trades": 1, "wins": 1}


async def test_purge_noop_when_no_mock_rows() -> None:
    store = InMemoryStore()
    set_store(store)
    await store.record_trade_close(
        {"symbol": "BTC/USDT", "leg": "full", "exit_price": 64275.0, "pnl_quote": 3.0}
    )
    result = await store.purge_mock_journal(MOCK)
    assert result["trades_removed"] == 0
    assert result["critiques_removed"] == 0
    assert len(await store.closed_trades()) == 1
