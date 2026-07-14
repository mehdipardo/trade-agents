"""RedisStore against a minimal type-enforcing fake.

The point is to catch the real-Redis-only bug the in-memory store can't: a key
used as two different types (WRONGTYPE). ``FakeRedis`` mimics Redis' one-type-
per-key rule so ``record_trade`` (sorted set) and ``record_trade_close`` (list)
sharing a key would raise here just as they would on a live server.
"""

from __future__ import annotations

import pytest

from app.services.store import RedisStore


class _WrongType(Exception):
    pass


class FakeRedis:
    def __init__(self) -> None:
        self._data: dict = {}
        self._types: dict[str, str] = {}

    def _check(self, key: str, kind: str) -> None:
        existing = self._types.get(key)
        if existing is not None and existing != kind:
            raise _WrongType(f"WRONGTYPE {key}: {existing} != {kind}")
        self._types[key] = kind

    # --- sorted set ---
    async def zadd(self, key: str, mapping: dict) -> int:
        self._check(key, "zset")
        self._data.setdefault(key, {}).update(mapping)
        return len(mapping)

    async def zremrangebyscore(self, key: str, lo, hi) -> int:  # noqa: ANN001
        return 0

    async def zcard(self, key: str) -> int:
        return len(self._data.get(key, {}))

    # --- list ---
    async def lpush(self, key: str, *vals: str) -> int:
        self._check(key, "list")
        lst = self._data.setdefault(key, [])
        for v in vals:
            lst.insert(0, v)
        return len(lst)

    async def rpush(self, key: str, *vals: str) -> int:
        self._check(key, "list")
        self._data.setdefault(key, []).extend(vals)
        return len(self._data[key])

    async def ltrim(self, key: str, start: int, end: int) -> None:
        lst = self._data.get(key, [])
        self._data[key] = lst[start : (end + 1 if end >= 0 else None)]

    async def lrange(self, key: str, start: int, end: int) -> list:
        lst = self._data.get(key, [])
        return lst[start:] if end == -1 else lst[start : end + 1]

    # --- scalars ---
    async def incrbyfloat(self, key: str, amt: float) -> float:
        self._check(key, "str")
        self._data[key] = float(self._data.get(key, 0.0)) + amt
        return self._data[key]

    async def incr(self, key: str) -> int:
        return await self.incrby(key, 1)

    async def incrby(self, key: str, amt: int) -> int:
        self._check(key, "str")
        self._data[key] = int(self._data.get(key, 0)) + amt
        return self._data[key]

    async def get(self, key: str):  # noqa: ANN201
        return self._data.get(key)

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)
        self._types.pop(key, None)

    async def expire(self, key: str, ttl: int) -> None:
        return None

    async def ping(self) -> bool:
        return True


@pytest.fixture()
def store() -> RedisStore:
    return RedisStore(FakeRedis())


async def test_trade_rate_and_closed_ledger_do_not_collide(store: RedisStore) -> None:
    # zadd (hourly rate) then lpush (ledger) must not share a key -> no WRONGTYPE.
    await store.record_trade("BTC/USDT", cooldown_s=0)
    await store.record_trade_close(
        {"symbol": "BTC/USDT", "leg": "full", "exit_price": 64275.0, "pnl_quote": 3.0}
    )
    trades = await store.closed_trades()
    assert len(trades) == 1 and trades[0]["exit_price"] == 64275.0
    assert await store.trades_last_hour() == 1


async def test_redis_purge_removes_mock_rows_and_reverses_counters(store: RedisStore) -> None:
    await store.record_trade_close(
        {"symbol": "BTC/USDT", "leg": "full", "exit_price": 65920.0, "pnl_quote": 12.0}
    )
    await store.bump_realized(12.0, closed=True, win=True)
    await store.record_trade_close(
        {"symbol": "BTC/USDT", "leg": "full", "exit_price": 60000.0, "pnl_quote": -44.0}
    )
    await store.bump_realized(-44.0, closed=True, win=False)
    await store.record_critique(
        {"symbol": "BTC/USDT", "exit_price": 60000.0, "critique": "phantom"}
    )

    result = await store.purge_mock_journal({"BTC/USDT": 60000.0})
    assert result["trades_removed"] == 1
    assert result["critiques_removed"] == 1

    trades = await store.closed_trades()
    assert len(trades) == 1 and trades[0]["exit_price"] == 65920.0
    perf = await store.performance()
    assert perf == {"realized_total": 12.0, "closed_trades": 1, "wins": 1}
