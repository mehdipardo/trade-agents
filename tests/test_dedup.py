"""Étape 7 tests: dedup key + node + one-pass behaviour + freshness gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import get_settings
from app.graph.nodes.dedup import dedup_key, dedup_node, is_stale
from app.graph.state import initial_state
from app.models.schemas import NewsEvent
from app.services.store import InMemoryStore, set_store


@pytest.fixture(autouse=True)
def _store() -> None:
    set_store(InMemoryStore())
    get_settings.cache_clear()
    yield
    set_store(None)
    get_settings.cache_clear()


def _event(title: str, *, source: str = "rss", published_at=None) -> NewsEvent:
    return NewsEvent(
        id="e", source=source, title=title,
        received_at=datetime.now(UTC), published_at=published_at,
    )


def test_dedup_key_normalizes() -> None:
    # Case, punctuation and whitespace differences collapse to the same key.
    assert dedup_key("Bitcoin ETF Approved!") == dedup_key("  bitcoin   etf  approved ")
    assert dedup_key("A") != dedup_key("B")


async def test_first_is_new_second_is_duplicate() -> None:
    state = initial_state(_event("SEC approves spot Solana ETF"))
    first = await dedup_node(state)
    assert first["is_duplicate"] is False
    assert first["status"] == "received"

    # Same headline from a different source -> duplicate.
    state2 = initial_state(_event("SEC APPROVES spot solana etf!!!"))
    second = await dedup_node(state2)
    assert second["is_duplicate"] is True
    assert second["status"] == "skipped_duplicate"


async def test_different_titles_not_duplicate() -> None:
    a = await dedup_node(initial_state(_event("headline one")))
    b = await dedup_node(initial_state(_event("headline two")))
    assert a["is_duplicate"] is False
    assert b["is_duplicate"] is False


# --- freshness gate -------------------------------------------------------


def test_is_stale_helper() -> None:
    now = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)
    assert is_stale(now - timedelta(hours=7), 21600, now=now) is True
    assert is_stale(now - timedelta(hours=5), 21600, now=now) is False
    assert is_stale(None, 21600, now=now) is False  # no timestamp -> fresh
    assert is_stale(now - timedelta(days=9), 0, now=now) is False  # gate disabled


async def test_stale_wild_feed_event_is_skipped() -> None:
    old = datetime.now(UTC) - timedelta(hours=8)
    state = initial_state(_event("Iran ceasefire holds", source="news", published_at=old))
    result = await dedup_node(state)
    assert result["status"] == "skipped_stale"


async def test_fresh_wild_feed_event_passes() -> None:
    recent = datetime.now(UTC) - timedelta(minutes=1)  # within the 5-min budget
    state = initial_state(_event("Fresh breaking news", source="news", published_at=recent))
    result = await dedup_node(state)
    assert result["status"] == "received"


async def test_manual_inject_never_gated_as_stale() -> None:
    # A simulator inject with an old published_at is a deliberate "act now".
    old = datetime.now(UTC) - timedelta(days=30)
    state = initial_state(_event("Demo scenario", source="simulator", published_at=old))
    result = await dedup_node(state)
    assert result["status"] == "received"
