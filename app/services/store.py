"""State store: risk counters, cooldowns, positions, kill switch, dedup.

Backed by Redis when reachable, with a fully-functional in-memory fallback so
the app and tests run without a Redis server. Both backends implement the same
async ``Store`` interface. Time-based state (hourly trade window, cooldowns,
dedup keys) uses TTLs / timestamp windows.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol

import orjson

from app.logging_config import get_logger

log = get_logger("app.services.store")

_HOUR_S = 3600


def _utc_day() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


class Store(Protocol):
    """Async state-store interface."""

    async def connect(self) -> None: ...
    async def close(self) -> None: ...

    # Kill switch
    async def get_kill_switch(self) -> bool: ...
    async def set_kill_switch(self, active: bool, reason: str | None = None) -> None: ...

    # Active strategy id (persisted so it survives restarts)
    async def get_strategy_id(self) -> str | None: ...
    async def set_strategy_id(self, strategy_id: str) -> None: ...

    # Risk counters / cooldowns / positions
    async def trades_last_hour(self) -> int: ...
    async def record_trade(self, asset: str, cooldown_s: int) -> None: ...
    async def in_cooldown(self, asset: str) -> bool: ...
    async def has_open_position(self, asset: str) -> bool: ...
    async def set_position(
        self, asset: str, is_open: bool, detail: dict | None = None
    ) -> None: ...
    async def open_positions(self) -> list[dict]: ...

    # Daily PnL
    async def daily_pnl(self) -> float: ...
    async def add_daily_pnl(self, delta: float) -> None: ...

    # Dedup
    async def seen_before(self, key: str, ttl_s: int) -> bool: ...

    # History (for the dashboard)
    async def record_history(self, entry: dict) -> None: ...
    async def history(self, limit: int = 100) -> list[dict]: ...

    # LLM post-mortems on stop-loss hits
    async def record_critique(self, entry: dict) -> None: ...
    async def critiques(self, limit: int = 20) -> list[dict]: ...

    async def snapshot(self) -> dict: ...


class InMemoryStore:
    """Process-local store. Not shared across workers; fine for the MVP."""

    def __init__(self) -> None:
        self._kill_switch = False
        self._kill_reason: str | None = None
        self._trade_ts: list[float] = []
        self._cooldowns: dict[str, float] = {}  # asset -> expiry epoch
        self._positions: dict[str, dict] = {}  # asset -> position detail
        self._pnl: dict[str, float] = {}  # day -> pnl
        self._dedup: dict[str, float] = {}  # key -> expiry epoch
        self._history: list[dict] = []  # recent pipeline results (bounded)
        self._critiques: list[dict] = []  # SL post-mortems (bounded)
        self._strategy_id: str | None = None

    async def connect(self) -> None:
        log.info("store_backend", backend="in-memory")

    async def close(self) -> None:
        return None

    async def get_kill_switch(self) -> bool:
        return self._kill_switch

    async def set_kill_switch(self, active: bool, reason: str | None = None) -> None:
        self._kill_switch = active
        self._kill_reason = reason if active else None

    async def get_strategy_id(self) -> str | None:
        return self._strategy_id

    async def set_strategy_id(self, strategy_id: str) -> None:
        self._strategy_id = strategy_id

    def _trim_trades(self, now: float) -> None:
        cutoff = now - _HOUR_S
        self._trade_ts = [t for t in self._trade_ts if t >= cutoff]

    async def trades_last_hour(self) -> int:
        now = time.time()
        self._trim_trades(now)
        return len(self._trade_ts)

    async def record_trade(self, asset: str, cooldown_s: int) -> None:
        now = time.time()
        self._trade_ts.append(now)
        if cooldown_s > 0:
            self._cooldowns[asset] = now + cooldown_s

    async def in_cooldown(self, asset: str) -> bool:
        expiry = self._cooldowns.get(asset)
        if expiry is None:
            return False
        if expiry <= time.time():
            self._cooldowns.pop(asset, None)
            return False
        return True

    async def has_open_position(self, asset: str) -> bool:
        return asset in self._positions

    async def set_position(self, asset: str, is_open: bool, detail: dict | None = None) -> None:
        if is_open:
            self._positions[asset] = {**(detail or {}), "asset": asset}
        else:
            self._positions.pop(asset, None)

    async def open_positions(self) -> list[dict]:
        return list(self._positions.values())

    async def daily_pnl(self) -> float:
        return self._pnl.get(_utc_day(), 0.0)

    async def add_daily_pnl(self, delta: float) -> None:
        day = _utc_day()
        self._pnl[day] = self._pnl.get(day, 0.0) + delta

    async def seen_before(self, key: str, ttl_s: int) -> bool:
        now = time.time()
        expiry = self._dedup.get(key)
        if expiry is not None and expiry > now:
            return True
        self._dedup[key] = now + ttl_s
        return False

    async def record_history(self, entry: dict) -> None:
        self._history.append(entry)
        del self._history[:-200]  # keep the last 200

    async def history(self, limit: int = 100) -> list[dict]:
        return list(reversed(self._history[-limit:]))

    async def record_critique(self, entry: dict) -> None:
        self._critiques.append(entry)
        del self._critiques[:-50]

    async def critiques(self, limit: int = 20) -> list[dict]:
        return list(reversed(self._critiques[-limit:]))

    async def snapshot(self) -> dict:
        return {
            "backend": "in-memory",
            "kill_switch": self._kill_switch,
            "kill_reason": self._kill_reason,
            "trades_last_hour": await self.trades_last_hour(),
            "open_positions": sorted(self._positions),
            "cooldowns": sorted(self._cooldowns),
            "daily_pnl": await self.daily_pnl(),
        }


class RedisStore:
    """Redis-backed store (used when a Redis server is reachable)."""

    KILL_KEY = "fst:killswitch"
    KILL_REASON_KEY = "fst:killswitch:reason"
    STRATEGY_KEY = "fst:strategy"
    TRADES_ZSET = "fst:trades"

    def __init__(self, client) -> None:  # noqa: ANN001 - redis.asyncio client
        self._r = client

    async def connect(self) -> None:
        await self._r.ping()
        log.info("store_backend", backend="redis")

    async def close(self) -> None:
        await self._r.aclose()

    async def get_kill_switch(self) -> bool:
        return bool(await self._r.exists(self.KILL_KEY))

    async def set_kill_switch(self, active: bool, reason: str | None = None) -> None:
        if active:
            await self._r.set(self.KILL_KEY, "1")
            await self._r.set(self.KILL_REASON_KEY, reason or "")
        else:
            await self._r.delete(self.KILL_KEY, self.KILL_REASON_KEY)

    async def get_strategy_id(self) -> str | None:
        val = await self._r.get(self.STRATEGY_KEY)
        return val or None

    async def set_strategy_id(self, strategy_id: str) -> None:
        await self._r.set(self.STRATEGY_KEY, strategy_id)

    async def trades_last_hour(self) -> int:
        now = time.time()
        await self._r.zremrangebyscore(self.TRADES_ZSET, 0, now - _HOUR_S)
        return int(await self._r.zcard(self.TRADES_ZSET))

    async def record_trade(self, asset: str, cooldown_s: int) -> None:
        now = time.time()
        await self._r.zadd(self.TRADES_ZSET, {f"{asset}:{now}": now})
        await self._r.expire(self.TRADES_ZSET, _HOUR_S)
        if cooldown_s > 0:
            await self._r.set(f"fst:cooldown:{asset}", "1", ex=cooldown_s)

    async def in_cooldown(self, asset: str) -> bool:
        return bool(await self._r.exists(f"fst:cooldown:{asset}"))

    async def has_open_position(self, asset: str) -> bool:
        return bool(await self._r.hexists("fst:positions", asset))

    async def set_position(self, asset: str, is_open: bool, detail: dict | None = None) -> None:
        if is_open:
            payload = orjson.dumps({**(detail or {}), "asset": asset}).decode()
            await self._r.hset("fst:positions", asset, payload)
        else:
            await self._r.hdel("fst:positions", asset)

    async def open_positions(self) -> list[dict]:
        raw = await self._r.hgetall("fst:positions")
        return [orjson.loads(v) for v in raw.values()]

    async def daily_pnl(self) -> float:
        val = await self._r.get(f"fst:pnl:{_utc_day()}")
        return float(val) if val is not None else 0.0

    async def add_daily_pnl(self, delta: float) -> None:
        key = f"fst:pnl:{_utc_day()}"
        await self._r.incrbyfloat(key, delta)
        await self._r.expire(key, 2 * 24 * _HOUR_S)

    async def seen_before(self, key: str, ttl_s: int) -> bool:
        # SETNX-with-TTL: returns True if the key already existed.
        was_set = await self._r.set(f"fst:dedup:{key}", "1", nx=True, ex=ttl_s)
        return not bool(was_set)

    async def record_history(self, entry: dict) -> None:
        await self._r.lpush("fst:history", orjson.dumps(entry).decode())
        await self._r.ltrim("fst:history", 0, 199)

    async def history(self, limit: int = 100) -> list[dict]:
        raw = await self._r.lrange("fst:history", 0, limit - 1)
        return [orjson.loads(v) for v in raw]

    async def record_critique(self, entry: dict) -> None:
        await self._r.lpush("fst:critiques", orjson.dumps(entry).decode())
        await self._r.ltrim("fst:critiques", 0, 49)

    async def critiques(self, limit: int = 20) -> list[dict]:
        raw = await self._r.lrange("fst:critiques", 0, limit - 1)
        return [orjson.loads(v) for v in raw]

    async def snapshot(self) -> dict:
        reason = await self._r.get(self.KILL_REASON_KEY)
        return {
            "backend": "redis",
            "kill_switch": await self.get_kill_switch(),
            "kill_reason": reason or None,
            "trades_last_hour": await self.trades_last_hour(),
            "open_positions": sorted(await self._r.hkeys("fst:positions")),
            "daily_pnl": await self.daily_pnl(),
        }


_store: Store | None = None


async def init_store(redis_url: str | None) -> Store:
    """Create the store singleton, preferring Redis and falling back in-memory."""
    global _store
    if redis_url:
        try:
            import redis.asyncio as aioredis

            client = aioredis.from_url(redis_url, decode_responses=True)
            store: Store = RedisStore(client)
            await store.connect()
            _store = store
            return _store
        except Exception as exc:  # noqa: BLE001 - fall back gracefully
            log.warning("redis_unavailable_fallback_memory", error=str(exc))
    _store = InMemoryStore()
    await _store.connect()
    return _store


def get_store() -> Store:
    """Return the current store singleton (in-memory if not initialised)."""
    global _store
    if _store is None:
        _store = InMemoryStore()
    return _store


def set_store(store: Store | None) -> None:
    """Override the singleton (used by tests)."""
    global _store
    _store = store
