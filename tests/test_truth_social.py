"""Tests for the Trump / Truth Social connector (Mastodon-shaped, mocked)."""

from __future__ import annotations

import asyncio

import pytest

from app.sources import truth_social
from app.sources.truth_social import (
    new_statuses,
    parse_status,
    poll_once,
    strip_html,
)


def _status(id_: str, content: str, reblog: bool = False) -> dict:
    return {
        "id": id_,
        "created_at": "2026-07-05T09:00:00Z",
        "content": content,
        "url": f"https://truthsocial.com/@realDonaldTrump/{id_}",
        "account": {"username": "realDonaldTrump", "display_name": "Donald J. Trump"},
        "reblog": {"id": "x"} if reblog else None,
    }


def test_strip_html() -> None:
    out = strip_html("<p>Strategic Bitcoin reserve, <b>NOW</b>!</p><p>We win.</p>")
    assert out == "Strategic Bitcoin reserve, NOW!\nWe win."


def test_strip_html_entities_and_breaks() -> None:
    assert strip_html("Tariffs &amp; trade<br>next line") == "Tariffs & trade\nnext line"


def test_parse_status() -> None:
    payload = parse_status(_status("111", "<p>I will make America the crypto capital.</p>"))
    assert payload["id"] == "111"
    assert payload["author"] == "Donald J. Trump"
    assert payload["title"].startswith("I will make America")
    assert "crypto capital" in payload["content"]


def test_new_statuses_filters_seen_and_reblogs_and_orders() -> None:
    statuses = [
        _status("3", "third"),  # newest first
        _status("2", "second", reblog=True),  # skipped (re-truth)
        _status("1", "first"),
    ]
    fresh = new_statuses(statuses, seen={"1"})
    # "1" already seen, "2" is a reblog -> only "3"; oldest-first order.
    assert [s["id"] for s in fresh] == ["3"]


async def test_poll_once_emits_new_only(monkeypatch: pytest.MonkeyPatch) -> None:
    feed = [_status("2", "second post"), _status("1", "first post")]

    async def fake_fetch(url: str):  # noqa: ANN001
        return feed

    monkeypatch.setattr(truth_social, "fetch_statuses", fake_fetch)
    queue: asyncio.Queue = asyncio.Queue()

    n1 = await poll_once(queue, "http://x")
    assert n1 == 2
    assert queue.qsize() == 2
    first = queue.get_nowait()
    assert first.source == "social"
    assert first.content == "first post"  # oldest emitted first

    # Second poll: nothing new.
    n2 = await poll_once(queue, "http://x")
    assert n2 == 0
