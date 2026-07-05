"""Étape 7 tests: dedup key + node + one-pass behaviour."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.config import get_settings
from app.graph.nodes.dedup import dedup_key, dedup_node
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


def _event(title: str) -> NewsEvent:
    return NewsEvent(
        id="e", source="rss", title=title, received_at=datetime.now(UTC)
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
