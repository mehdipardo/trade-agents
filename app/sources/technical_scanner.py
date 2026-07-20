"""Standalone technical-setup scanner.

Every ``interval_s`` it sweeps the crypto whitelist (the symbols we have real
candles for), runs the strict ``detect_setup`` rule (breakout/breakdown WITH
trend and momentum agreement) and emits a ``NewsEvent(source="technical")``
into the normal pipeline. The event carries its direction/confidence in
``meta`` so the analyst skips the LLM — a technical setup is deterministic,
there is nothing for a language model to read.

Discipline: the pipeline's existing guards still apply (risk engine, cooldown,
one position per asset, kill switch), plus a local per-symbol emit cooldown so
a persisting breakout doesn't re-fire every sweep.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

from app.logging_config import get_logger
from app.models.schemas import NewsEvent
from app.services.technicals import detect_setup

log = get_logger("app.sources.technical_scanner")

EMIT_COOLDOWN_S = 3600  # one setup per symbol per hour max
SETUP_CONFIDENCE = 0.7
SETUP_IMPACT = 5  # deliberately below the leverage-boost threshold

_last_emit: dict[str, float] = {}


def reset_state() -> None:
    """Clear the per-symbol emit cooldown (used by tests)."""
    _last_emit.clear()


def build_setup_event(symbol: str, setup: dict) -> NewsEvent:
    """Wrap a detected setup into a pipeline event (LLM bypass via meta)."""
    direction = setup["direction"]
    now = datetime.now(UTC)
    return NewsEvent(
        id=f"ta-{symbol}-{int(now.timestamp())}",
        source="technical",
        author="technical-scanner",
        title=f"Technical setup: {symbol} {'breakout' if direction == 'BULL' else 'breakdown'}",
        content=setup["reason"],
        published_at=now,
        received_at=now,
        meta={
            "symbol": symbol,
            "direction": direction,
            "confidence": SETUP_CONFIDENCE,
            "impact": SETUP_IMPACT,
            "reason": setup["reason"],
        },
    )


async def scan_once(queue: asyncio.Queue[NewsEvent]) -> int:
    """One sweep over the whitelist. Returns the number of setups emitted."""
    from app.config import get_settings
    from app.services.prices import klines
    from app.services.store import get_store

    settings = get_settings()
    store = get_store()
    now = time.monotonic()
    emitted = 0
    for symbol in settings.asset_whitelist_set:
        last = _last_emit.get(symbol)
        if last is not None and now - last < EMIT_COOLDOWN_S:
            continue
        # Don't even scan symbols we couldn't trade right now.
        if await store.has_open_position(symbol) or await store.in_cooldown(symbol):
            continue
        candles = await klines(symbol, interval="5m", limit=200)
        if not candles:
            continue  # TradFi (no kline source) or transient fetch failure
        setup = detect_setup(candles)
        if setup is None:
            continue
        _last_emit[symbol] = now
        event = build_setup_event(symbol, setup)
        try:
            queue.put_nowait(event)
            emitted += 1
            log.info("technical_setup_emitted", symbol=symbol, direction=setup["direction"])
        except asyncio.QueueFull:
            log.warning("technical_queue_full", symbol=symbol)
    return emitted


async def scanner_loop(queue: asyncio.Queue[NewsEvent], interval_s: float = 300.0) -> None:
    """Sweep forever. One bad sweep never kills the loop."""
    log.info("technical_scanner_started", interval_s=interval_s)
    try:
        while True:
            await asyncio.sleep(interval_s)
            try:
                await scan_once(queue)
            except Exception as exc:  # noqa: BLE001 - never die on a sweep error
                log.error("technical_scan_error", error=str(exc))
    except asyncio.CancelledError:
        log.info("technical_scanner_stopped")
        raise
