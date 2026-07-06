"""Tests for the broad news aggregator connector (SSE, mocked)."""

from __future__ import annotations

import asyncio

from app.sources.aggregator import (
    _emit_item,
    _items_from_payload,
    item_key,
    parse_item,
    parse_sse_data,
)


def _item(title: str, link: str, source: str = "CoinDesk") -> dict:
    return {
        "title": title,
        "link": link,
        "description": "summary text",
        "pubDate": "2026-07-06T13:00:00Z",
        "source": source,
        "currencies": ["BTC"],
    }


def test_item_key_prefers_link() -> None:
    assert item_key(_item("t", "https://e/1")) == "https://e/1"
    assert item_key({"title": "only title"}) == "only title"


def test_parse_item_maps_fields_and_tickers() -> None:
    payload = parse_item(_item("Saylor sells BTC", "https://e/2"))
    assert payload["title"] == "Saylor sells BTC"
    assert payload["author"] == "CoinDesk"
    assert payload["url"] == "https://e/2"
    assert "[BTC]" in payload["content"]


def test_parse_sse_data() -> None:
    assert parse_sse_data(' {"title":"x"} ') == {"title": "x"}
    assert parse_sse_data("not json") is None
    assert parse_sse_data("") is None


def test_items_from_payload_shapes() -> None:
    assert _items_from_payload({"title": "x"}) == [{"title": "x"}]
    assert _items_from_payload([{"a": 1}, {"b": 2}]) == [{"a": 1}, {"b": 2}]
    assert _items_from_payload({"items": [{"a": 1}]}) == [{"a": 1}]
    assert _items_from_payload("nope") == []


async def test_emit_item_dedups() -> None:
    queue: asyncio.Queue = asyncio.Queue()
    item = _item("Breaking", "https://e/3")
    assert await _emit_item(queue, item) is True
    assert await _emit_item(queue, item) is False  # duplicate link
    assert queue.qsize() == 1
    event = queue.get_nowait()
    assert event.source == "news"
    assert event.title == "Breaking"
