"""Tests for the Trump / Truth Social connector (Mastodon-shaped, mocked)."""

from __future__ import annotations

import asyncio

import pytest

from app.sources import truth_social
from app.sources.truth_social import (
    new_statuses,
    parse_account_urls,
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


def test_parse_account_urls_splits_comma_and_newline() -> None:
    assert parse_account_urls("a, b\nc ,, ") == ["a", "b", "c"]
    assert parse_account_urls("") == []
    assert parse_account_urls("  https://x/statuses  ") == ["https://x/statuses"]


async def test_watchlist_shares_seen_set_across_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Monitoring several accounts: a post already emitted from one feed must not
    # re-fire if it also surfaces on another feed (one shared seen-set).
    truth_social.reset_state()
    feeds = {
        "acct-A": [_status("100", "Trump on tariffs")],
        "acct-B": [_status("100", "Trump on tariffs"), _status("200", "Vance on rates")],
    }

    async def fake_fetch(url: str):  # noqa: ANN001
        return feeds[url]

    monkeypatch.setattr(truth_social, "fetch_statuses", fake_fetch)
    queue: asyncio.Queue = asyncio.Queue()

    assert await poll_once(queue, "acct-A") == 1  # post 100
    assert await poll_once(queue, "acct-B") == 1  # only 200; 100 already seen
    assert queue.qsize() == 2


async def test_resolve_entries_handles_urls_and_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_lookup(handle: str, base: str):  # noqa: ANN001
        key = handle.lstrip("@")
        return {"realDonaldTrump": "107780257626128497", "dbongino": "12345"}.get(key)

    monkeypatch.setattr(truth_social, "_lookup_account_id", fake_lookup)
    entries = [
        "@realDonaldTrump",                              # handle -> resolved
        "https://mirror.example/accounts/9/statuses",    # URL -> passthrough
        "@dbongino",                                     # handle -> resolved
        "@ghost_unknown",                                # unresolved -> dropped
    ]
    urls = await truth_social.resolve_entries(entries, base="https://truthsocial.com")
    assert urls == [
        "https://truthsocial.com/api/v1/accounts/107780257626128497/statuses",
        "https://mirror.example/accounts/9/statuses",
        "https://truthsocial.com/api/v1/accounts/12345/statuses",
    ]


def test_auth_headers_include_bearer_when_token_set(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.config as config

    class _S:
        truth_social_token = "tok"

    monkeypatch.setattr(config, "get_settings", lambda: _S())
    headers = truth_social._auth_headers()
    assert headers["Authorization"] == "Bearer tok"
    assert headers["User-Agent"] == "flashsentiment/0.1"


def test_auth_headers_no_bearer_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.config as config

    class _S:
        truth_social_token = ""

    monkeypatch.setattr(config, "get_settings", lambda: _S())
    assert "Authorization" not in truth_social._auth_headers()
