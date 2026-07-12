"""Étape 1 tests: normalizer and scenario simulator."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.ingestion.normalizer import normalize_payload
from app.ingestion.simulator import list_scenarios, load_scenario

EXPECTED_SCENARIOS = {
    "trump_btc_bull",
    "trump_iran_bear",
    "nvda_chip_ban",
    "cpi_hot_bear",
    "sec_etf_approval",
    "neutral_report",
    "prompt_injection",
}


def test_normalize_full_payload_sets_received_at() -> None:
    before = datetime.now(UTC)
    event = normalize_payload(
        {
            "id": "abc-123",
            "author": "Someone",
            "title": "A title",
            "content": "Some content",
            "url": "https://example.test/x",
            "published_at": "2026-07-05T08:00:00Z",
        },
        source="webhook",
    )
    assert event.id == "abc-123"
    assert event.source == "webhook"
    assert event.title == "A title"
    assert event.content == "Some content"
    assert event.received_at >= before
    assert event.received_at.tzinfo is not None


def test_normalize_generates_id_when_missing() -> None:
    event = normalize_payload({"title": "No id here"}, source="rss")
    assert event.id  # a uuid4 string
    assert len(event.id) >= 8


def test_normalize_field_aliases() -> None:
    event = normalize_payload(
        {"headline": "Aliased", "text": "body text", "link": "https://e.test"},
        source="webhook",
    )
    assert event.title == "Aliased"
    assert event.content == "body text"
    assert str(event.url) == "https://e.test"


def test_normalize_title_falls_back_to_first_content_line() -> None:
    event = normalize_payload(
        {"content": "First line becomes title\nsecond line"}, source="webhook"
    )
    assert event.title == "First line becomes title"


def test_normalize_raises_without_title_or_content() -> None:
    with pytest.raises(ValueError):
        normalize_payload({"author": "nobody"}, source="webhook")


def test_rfc822_date_is_parsed_not_dropped() -> None:
    # Regression: RSS/aggregator feeds use RFC 822 dates. A bad date must NEVER
    # drop the whole event (that silently starved the pipeline of news).
    event = normalize_payload(
        {"title": "BTC headline", "published_at": "Mon, 07 Jul 2025 12:00:00 GMT"},
        source="news",
    )
    assert event.title == "BTC headline"
    assert event.published_at is not None
    assert event.published_at.tzinfo is not None


def test_unparseable_date_falls_back_to_none_and_keeps_event() -> None:
    event = normalize_payload(
        {"title": "keep me", "published_at": "yesterday-ish"}, source="news"
    )
    assert event.title == "keep me"
    assert event.published_at is None  # event kept, date simply dropped


def test_epoch_seconds_date_parsed() -> None:
    event = normalize_payload(
        {"title": "epoch news", "date": "1752345600"}, source="news"
    )
    assert event.published_at is not None
    assert event.published_at.year == 2025


def test_all_scenarios_present() -> None:
    assert set(list_scenarios()) == EXPECTED_SCENARIOS


@pytest.mark.parametrize("name", sorted(EXPECTED_SCENARIOS))
def test_load_each_scenario(name: str) -> None:
    event = load_scenario(name)
    assert event.source == "simulator"
    assert event.title
    assert event.received_at.tzinfo is not None
    assert event.id  # generated fresh


def test_load_unknown_scenario_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_scenario("does_not_exist")


def test_repeated_load_produces_distinct_ids() -> None:
    a = load_scenario("trump_btc_bull")
    b = load_scenario("trump_btc_bull")
    assert a.id != b.id
