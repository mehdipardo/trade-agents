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


def _is_mock_exit(entry: dict, mock_prices: dict[str, float]) -> bool:
    """True when a journal row's exit_price equals its symbol's mock reference.

    That is the exact signature of a false SL/TP fabricated by the (now fixed)
    mock-price fallback: the monitor marked at a static mock (e.g. BTC=60000)
    and booked a stop there. A real exit lands on the live mark, which never
    equals the mock constant, so this match is precise.
    """
    symbol, exit_price = entry.get("symbol"), entry.get("exit_price")
    if symbol is None or exit_price is None:
        return False
    mock = mock_prices.get(symbol)
    if mock is None:
        return False
    return abs(float(exit_price) - float(mock)) <= max(1e-6, abs(float(mock)) * 1e-6)


def _counter_reversal(removed_trades: list[dict]) -> tuple[float, int, int]:
    """(realized, closed, wins) to subtract for a set of purged closed-trade rows.

    Every close (partial or full) added its net PnL to the realized total; only
    full closes (leg ``full``/``runner``) incremented the closed-trade and win
    counters. Mirror that here so ``performance()`` stays consistent post-purge.
    """
    realized = sum(float(e.get("pnl_quote") or 0.0) for e in removed_trades)
    full = [e for e in removed_trades if e.get("leg") in ("full", "runner")]
    wins = sum(1 for e in full if float(e.get("pnl_quote") or 0.0) > 0)
    return realized, len(full), wins


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

    # Per-source runtime config (dashboard-editable overrides on top of env).
    async def get_source_config(self, source_id: str) -> dict[str, str]: ...
    async def set_source_config(self, source_id: str, config: dict[str, str]) -> None: ...
    async def all_source_configs(self) -> dict[str, dict[str, str]]: ...

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

    # Lifetime realized performance (for the equity / win-rate hero metrics)
    async def bump_realized(self, net_pnl: float, *, closed: bool, win: bool) -> None: ...
    async def performance(self) -> dict: ...

    # LLM usage tracking (Groq consumption watch)
    async def bump_llm(self, prompt_tokens: int, completion_tokens: int) -> None: ...
    async def bump_news_analyzed(self) -> None: ...
    async def llm_usage(self) -> dict: ...

    # Ingestion funnel (received -> analyzed / dropped) for the dashboard.
    async def bump_ingest(self, kind: str) -> None: ...
    async def record_news_age(self, seconds: float) -> None: ...
    async def ingestion(self) -> dict: ...

    # Dedup
    async def seen_before(self, key: str, ttl_s: int) -> bool: ...

    # History (for the dashboard)
    async def record_history(self, entry: dict) -> None: ...
    async def history(self, limit: int = 100) -> list[dict]: ...

    # LLM post-mortems on stop-loss hits
    async def record_critique(self, entry: dict) -> None: ...
    async def critiques(self, limit: int = 20) -> list[dict]: ...

    # Closed-trade ledger (entry/exit/reason/pnl) for the trade history panel
    async def record_trade_close(self, entry: dict) -> None: ...
    async def closed_trades(self, limit: int = 50) -> list[dict]: ...

    # Maintenance: drop journal rows fabricated by the (fixed) mock-price bug.
    async def purge_mock_journal(self, mock_prices: dict[str, float]) -> dict: ...

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
        self._closed_trades: list[dict] = []  # trade ledger (bounded)
        self._strategy_id: str | None = None
        self._source_configs: dict[str, dict[str, str]] = {}
        self._realized_total = 0.0  # lifetime realized PnL (net of fees)
        self._closed_count = 0
        self._wins = 0
        self._llm_calls = 0
        self._llm_prompt_tokens = 0
        self._llm_completion_tokens = 0
        self._llm_calls_by_day: dict[str, int] = {}
        self._llm_tokens_by_day: dict[str, list[int]] = {}  # day -> [prompt, completion]
        self._news_analyzed = 0
        self._news_analyzed_by_day: dict[str, int] = {}
        self._ingest_by_day: dict[str, dict[str, int]] = {}  # day -> {received,stale,duplicate}
        self._news_age_by_day: dict[str, list[float]] = {}  # day -> [sum_s, count, max_s]

    async def connect(self) -> None:
        log.info("store_backend", backend="in-memory")

    async def bump_ingest(self, kind: str) -> None:
        day = self._ingest_by_day.setdefault(_utc_day(), {})
        day[kind] = day.get(kind, 0) + 1

    async def record_news_age(self, seconds: float) -> None:
        d = self._news_age_by_day.setdefault(_utc_day(), [0.0, 0, 0.0])
        d[0] += seconds
        d[1] = int(d[1]) + 1
        d[2] = max(d[2], seconds)

    async def ingestion(self) -> dict:
        d = self._ingest_by_day.get(_utc_day(), {})
        age = self._news_age_by_day.get(_utc_day(), [0.0, 0, 0.0])
        avg = age[0] / age[1] if age[1] else 0.0
        return {
            "received_today": d.get("received", 0),
            "dropped_stale_today": d.get("stale", 0),
            "dropped_duplicate_today": d.get("duplicate", 0),
            "avg_news_age_s": round(avg, 1),
            "max_news_age_seen_s": round(age[2], 1),
        }

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

    async def get_source_config(self, source_id: str) -> dict[str, str]:
        return dict(self._source_configs.get(source_id, {}))

    async def set_source_config(self, source_id: str, config: dict[str, str]) -> None:
        self._source_configs[source_id] = {k: str(v) for k, v in config.items() if v}

    async def all_source_configs(self) -> dict[str, dict[str, str]]:
        return {k: dict(v) for k, v in self._source_configs.items()}

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

    async def bump_realized(self, net_pnl: float, *, closed: bool, win: bool) -> None:
        self._realized_total += net_pnl
        if closed:
            self._closed_count += 1
            if win:
                self._wins += 1

    async def performance(self) -> dict:
        return {
            "realized_total": round(self._realized_total, 4),
            "closed_trades": self._closed_count,
            "wins": self._wins,
        }

    async def bump_llm(self, prompt_tokens: int, completion_tokens: int) -> None:
        self._llm_calls += 1
        self._llm_prompt_tokens += prompt_tokens
        self._llm_completion_tokens += completion_tokens
        day = _utc_day()
        self._llm_calls_by_day[day] = self._llm_calls_by_day.get(day, 0) + 1
        tok = self._llm_tokens_by_day.setdefault(day, [0, 0])
        tok[0] += prompt_tokens
        tok[1] += completion_tokens

    async def bump_news_analyzed(self) -> None:
        self._news_analyzed += 1
        day = _utc_day()
        self._news_analyzed_by_day[day] = self._news_analyzed_by_day.get(day, 0) + 1

    async def llm_usage(self) -> dict:
        day = _utc_day()
        pt_today, ct_today = self._llm_tokens_by_day.get(day, [0, 0])
        return {
            "calls_total": self._llm_calls,
            "calls_today": self._llm_calls_by_day.get(day, 0),
            "prompt_tokens": self._llm_prompt_tokens,
            "completion_tokens": self._llm_completion_tokens,
            "total_tokens": self._llm_prompt_tokens + self._llm_completion_tokens,
            "prompt_tokens_today": pt_today,
            "completion_tokens_today": ct_today,
            "news_analyzed_total": self._news_analyzed,
            "news_analyzed_today": self._news_analyzed_by_day.get(day, 0),
        }

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

    async def record_trade_close(self, entry: dict) -> None:
        self._closed_trades.append(entry)
        del self._closed_trades[:-100]

    async def closed_trades(self, limit: int = 50) -> list[dict]:
        return list(reversed(self._closed_trades[-limit:]))

    async def purge_mock_journal(self, mock_prices: dict[str, float]) -> dict:
        removed_c = [c for c in self._critiques if _is_mock_exit(c, mock_prices)]
        removed_t = [t for t in self._closed_trades if _is_mock_exit(t, mock_prices)]
        self._critiques = [c for c in self._critiques if c not in removed_c]
        self._closed_trades = [t for t in self._closed_trades if t not in removed_t]
        realized, closed, wins = _counter_reversal(removed_t)
        self._realized_total -= realized
        self._closed_count = max(0, self._closed_count - closed)
        self._wins = max(0, self._wins - wins)
        return {
            "critiques_removed": len(removed_c),
            "trades_removed": len(removed_t),
            "realized_reversed": round(realized, 4),
            "closed_reversed": closed,
            "wins_reversed": wins,
        }

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

    async def get_source_config(self, source_id: str) -> dict[str, str]:
        raw = await self._r.get(f"fst:source_config:{source_id}")
        return orjson.loads(raw) if raw else {}

    async def set_source_config(self, source_id: str, config: dict[str, str]) -> None:
        cleaned = {k: str(v) for k, v in config.items() if v}
        if cleaned:
            await self._r.set(f"fst:source_config:{source_id}", orjson.dumps(cleaned).decode())
        else:
            await self._r.delete(f"fst:source_config:{source_id}")

    async def all_source_configs(self) -> dict[str, dict[str, str]]:
        result: dict[str, dict[str, str]] = {}
        async for key in self._r.scan_iter("fst:source_config:*"):
            source_id = key.split(":", 2)[2]
            raw = await self._r.get(key)
            if raw:
                result[source_id] = orjson.loads(raw)
        return result

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

    async def bump_realized(self, net_pnl: float, *, closed: bool, win: bool) -> None:
        await self._r.incrbyfloat("fst:realized_total", net_pnl)
        if closed:
            await self._r.incr("fst:closed_count")
            if win:
                await self._r.incr("fst:wins")

    async def performance(self) -> dict:
        total = await self._r.get("fst:realized_total")
        closed = await self._r.get("fst:closed_count")
        wins = await self._r.get("fst:wins")
        return {
            "realized_total": round(float(total), 4) if total else 0.0,
            "closed_trades": int(closed) if closed else 0,
            "wins": int(wins) if wins else 0,
        }

    async def bump_llm(self, prompt_tokens: int, completion_tokens: int) -> None:
        day = _utc_day()
        await self._r.incr("fst:llm:calls")
        await self._r.incrby("fst:llm:prompt_tokens", prompt_tokens)
        await self._r.incrby("fst:llm:completion_tokens", completion_tokens)
        for key, amount in (
            (f"fst:llm:calls:{day}", 1),
            (f"fst:llm:pt:{day}", prompt_tokens),
            (f"fst:llm:ct:{day}", completion_tokens),
        ):
            await self._r.incrby(key, amount)
            await self._r.expire(key, 2 * 24 * _HOUR_S)

    async def bump_news_analyzed(self) -> None:
        day = _utc_day()
        await self._r.incr("fst:news_analyzed")
        dk = f"fst:news_analyzed:{day}"
        await self._r.incr(dk)
        await self._r.expire(dk, 2 * 24 * _HOUR_S)

    async def bump_ingest(self, kind: str) -> None:
        k = f"fst:ingest:{kind}:{_utc_day()}"
        await self._r.incr(k)
        await self._r.expire(k, 2 * 24 * _HOUR_S)

    async def record_news_age(self, seconds: float) -> None:
        day = _utc_day()
        ttl = 2 * 24 * _HOUR_S
        await self._r.incrbyfloat(f"fst:ingest:age_sum:{day}", seconds)
        await self._r.incr(f"fst:ingest:age_cnt:{day}")
        mk = f"fst:ingest:age_max:{day}"
        cur = await self._r.get(mk)
        if cur is None or seconds > float(cur):
            await self._r.set(mk, seconds)
        for k in (f"fst:ingest:age_sum:{day}", f"fst:ingest:age_cnt:{day}", mk):
            await self._r.expire(k, ttl)

    async def ingestion(self) -> dict:
        day = _utc_day()

        async def _int(key: str) -> int:
            v = await self._r.get(key)
            return int(v) if v else 0

        async def _float(key: str) -> float:
            v = await self._r.get(key)
            return float(v) if v else 0.0

        cnt = await _int(f"fst:ingest:age_cnt:{day}")
        avg = (await _float(f"fst:ingest:age_sum:{day}")) / cnt if cnt else 0.0
        return {
            "received_today": await _int(f"fst:ingest:received:{day}"),
            "dropped_stale_today": await _int(f"fst:ingest:stale:{day}"),
            "dropped_duplicate_today": await _int(f"fst:ingest:duplicate:{day}"),
            "avg_news_age_s": round(avg, 1),
            "max_news_age_seen_s": round(await _float(f"fst:ingest:age_max:{day}"), 1),
        }

    async def llm_usage(self) -> dict:
        day = _utc_day()

        async def _int(key: str) -> int:
            v = await self._r.get(key)
            return int(v) if v else 0

        pt, ct = await _int("fst:llm:prompt_tokens"), await _int("fst:llm:completion_tokens")
        return {
            "calls_total": await _int("fst:llm:calls"),
            "calls_today": await _int(f"fst:llm:calls:{day}"),
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": pt + ct,
            "prompt_tokens_today": await _int(f"fst:llm:pt:{day}"),
            "completion_tokens_today": await _int(f"fst:llm:ct:{day}"),
            "news_analyzed_total": await _int("fst:news_analyzed"),
            "news_analyzed_today": await _int(f"fst:news_analyzed:{day}"),
        }

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

    async def record_trade_close(self, entry: dict) -> None:
        # NB: distinct key from TRADES_ZSET ("fst:trades"). That key is a sorted
        # set (hourly trade-rate counter); reusing it here as a list would raise
        # WRONGTYPE and silently drop every closed-trade row on Redis.
        await self._r.lpush("fst:closed_trades", orjson.dumps(entry).decode())
        await self._r.ltrim("fst:closed_trades", 0, 99)

    async def closed_trades(self, limit: int = 50) -> list[dict]:
        raw = await self._r.lrange("fst:closed_trades", 0, limit - 1)
        return [orjson.loads(v) for v in raw]

    async def _rewrite_list(self, key: str, kept: list[dict]) -> None:
        """Replace a JSON list key with ``kept`` (newest-first order preserved)."""
        await self._r.delete(key)
        if kept:
            await self._r.rpush(key, *[orjson.dumps(e).decode() for e in kept])

    async def purge_mock_journal(self, mock_prices: dict[str, float]) -> dict:
        crit_raw = await self._r.lrange("fst:critiques", 0, -1)
        crit = [orjson.loads(v) for v in crit_raw]
        kept_crit = [c for c in crit if not _is_mock_exit(c, mock_prices)]

        trade_raw = await self._r.lrange("fst:closed_trades", 0, -1)
        trades = [orjson.loads(v) for v in trade_raw]
        removed_t = [t for t in trades if _is_mock_exit(t, mock_prices)]
        kept_t = [t for t in trades if not _is_mock_exit(t, mock_prices)]
        realized, closed, wins = _counter_reversal(removed_t)

        if len(kept_crit) != len(crit):
            await self._rewrite_list("fst:critiques", kept_crit)
        if removed_t:
            await self._rewrite_list("fst:closed_trades", kept_t)
            if realized:
                await self._r.incrbyfloat("fst:realized_total", -realized)
            for k, n in (("fst:closed_count", closed), ("fst:wins", wins)):
                if n:
                    await self._r.incrby(k, -n)
        return {
            "critiques_removed": len(crit) - len(kept_crit),
            "trades_removed": len(removed_t),
            "realized_reversed": round(realized, 4),
            "closed_reversed": closed,
            "wins_reversed": wins,
        }

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
