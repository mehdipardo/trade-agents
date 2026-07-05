"""Tests for the Congress.gov bill tracker (mocked)."""

from __future__ import annotations

import asyncio

import pytest

from app.sources import congress
from app.sources.congress import action_key, parse_bill, parse_bill_refs, poll_once


def _bill(date: str, text: str, number: str = "1747") -> dict:
    return {
        "number": number,
        "title": "CLARITY Act",
        "url": "https://api.congress.gov/v3/bill/119/hr/1747",
        "latestAction": {"actionDate": date, "text": text},
    }


def test_parse_bill_refs() -> None:
    refs = parse_bill_refs("119/hr/1747, 119/s/1582 ,bad, /x/")
    assert refs == [("119", "hr", "1747"), ("119", "s", "1582")]


def test_action_key_and_parse() -> None:
    bill = _bill("2026-06-01", "Passed House")
    assert action_key(bill) == "2026-06-01::Passed House"
    payload = parse_bill(bill, "119/hr/1747")
    assert payload is not None
    assert "CLARITY Act" in payload["title"]
    assert "Passed House" in payload["title"]


def test_action_key_none_when_incomplete() -> None:
    assert action_key({"latestAction": {}}) is None


async def test_poll_once_primes_then_emits_on_change(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"bill": _bill("2026-06-01", "Referred to committee")}

    async def fake_fetch(congress_, bill_type, number, api_key):  # noqa: ANN001
        return state["bill"]

    monkeypatch.setattr(congress, "fetch_bill", fake_fetch)
    refs = [("119", "hr", "1747")]
    queue: asyncio.Queue = asyncio.Queue()

    # First poll primes the baseline; no emission.
    assert await poll_once(queue, refs, "key") == 0
    assert queue.qsize() == 0

    # Same action -> still nothing.
    assert await poll_once(queue, refs, "key") == 0

    # New action -> one regulatory event emitted.
    state["bill"] = _bill("2026-06-15", "Passed House")
    assert await poll_once(queue, refs, "key") == 1
    event = queue.get_nowait()
    assert event.source == "regulatory"
    assert "Passed House" in event.title
