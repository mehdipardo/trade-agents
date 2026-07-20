"""Ingestion funnel: received -> analyzed / dropped counters + freshness gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.config import Settings
from app.graph.nodes.dedup import dedup_node
from app.graph.state import initial_state
from app.models.schemas import NewsEvent
from app.services.store import InMemoryStore, set_store


def _event(published_at: datetime | None, title: str = "Fed hikes rates") -> NewsEvent:
    return NewsEvent(
        id=title, source="rss", title=title, content="…",
        published_at=published_at, received_at=datetime.now(UTC),
    )


async def _run(event: NewsEvent) -> dict:
    state = initial_state(event)
    out = await dedup_node(state)
    return out


async def test_fresh_news_passes_and_counts_received_analyzed_path() -> None:
    store = InMemoryStore()
    set_store(store)
    out = await _run(_event(datetime.now(UTC) - timedelta(minutes=5)))
    assert out["status"] == "received"
    funnel = await store.ingestion()
    assert funnel["received_today"] == 1
    assert funnel["dropped_stale_today"] == 0


async def test_two_hour_old_news_still_passes_default_gate() -> None:
    # Regression guard: the default gate must NOT starve on ~2h-stale RSS news.
    store = InMemoryStore()
    set_store(store)
    assert Settings().max_news_age_s >= 7200
    out = await _run(_event(datetime.now(UTC) - timedelta(minutes=90)))
    assert out["status"] == "received"  # 90 min < 2h default -> passes


async def test_clearly_stale_news_is_dropped_and_counted() -> None:
    store = InMemoryStore()
    set_store(store)
    out = await _run(_event(datetime.now(UTC) - timedelta(hours=5)))
    assert out["status"] == "skipped_stale"
    funnel = await store.ingestion()
    assert funnel["received_today"] == 1
    assert funnel["dropped_stale_today"] == 1


async def test_duplicate_headline_is_counted() -> None:
    store = InMemoryStore()
    set_store(store)
    fresh = lambda: _event(datetime.now(UTC), title="Same headline")  # noqa: E731
    assert (await _run(fresh()))["status"] == "received"
    assert (await _run(fresh()))["status"] == "skipped_duplicate"
    funnel = await store.ingestion()
    assert funnel["received_today"] == 2
    assert funnel["dropped_duplicate_today"] == 1


async def test_ingestion_counters_start_empty() -> None:
    store = InMemoryStore()
    set_store(store)
    assert await store.ingestion() == {
        "received_today": 0, "dropped_stale_today": 0, "dropped_duplicate_today": 0,
    }
