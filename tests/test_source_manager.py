"""Source manager factories: default-enabled sources must run turnkey."""

from __future__ import annotations

import asyncio

from app.config import Settings
from app.services.store import InMemoryStore, set_store
from app.sources.manager import (
    MergedSettings,
    _crypto_news_rss,
    _econ_calendar,
    _news_aggregator,
)


def _merged(**overrides: str) -> MergedSettings:
    settings = Settings(_env_file=None, paper_trading="true", exchange_sandbox="true")  # type: ignore[arg-type]
    return MergedSettings(settings, overrides)


def _close(coro) -> None:
    # The factory returns an un-awaited coroutine; close it so pytest doesn't warn.
    if coro is not None:
        coro.close()


def test_aggregator_falls_back_to_default_url_when_env_unset() -> None:
    # No aggregator_sse_url configured anywhere -> must still start (default URL).
    coro = _news_aggregator(asyncio.Queue(), _merged())
    assert coro is not None  # would have been None (source_not_configured) before
    _close(coro)


def test_econ_calendar_falls_back_to_default_url_when_env_unset() -> None:
    coro = _econ_calendar(asyncio.Queue(), _merged())
    assert coro is not None
    _close(coro)


def test_store_override_takes_precedence_over_env() -> None:
    set_store(InMemoryStore())
    merged = _merged(aggregator_sse_url="https://override.example/sse")
    assert merged.aggregator_sse_url == "https://override.example/sse"


def test_rss_source_falls_back_to_default_feeds() -> None:
    coro = _crypto_news_rss(asyncio.Queue(), _merged())
    assert coro is not None  # runs on the curated default feed set
    _close(coro)


def test_three_heterogeneous_sources_enabled_by_default() -> None:
    from app.sources import catalog

    catalog.reset_state()
    enabled = {s.id for s in catalog.list_specs() if catalog.is_enabled(s.id)}
    # Crypto firehose + macro calendar + broad world/markets RSS = 3 markets.
    assert enabled == {"news_aggregator", "econ_calendar", "crypto_news_rss"}
