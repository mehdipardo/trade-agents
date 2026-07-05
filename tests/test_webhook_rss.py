"""Étape 7 tests: secured webhook + RSS entry parsing + one-pass e2e."""

from __future__ import annotations

import importlib.util

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.ingestion.rss_poller import parse_entry, parse_feeds_setting


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("EXCHANGE_SANDBOX", "true")
    monkeypatch.setenv("WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("APP_ENV", "test")
    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()


def test_webhook_rejects_missing_secret(client: TestClient) -> None:
    resp = client.post("/webhooks/news", json={"title": "hi"})
    assert resp.status_code == 401


def test_webhook_rejects_wrong_secret(client: TestClient) -> None:
    resp = client.post(
        "/webhooks/news", json={"title": "hi"}, headers={"X-Webhook-Secret": "nope"}
    )
    assert resp.status_code == 401


def test_webhook_accepts_valid_secret(client: TestClient) -> None:
    resp = client.post(
        "/webhooks/news",
        json={"title": "Breaking", "content": "body"},
        headers={"X-Webhook-Secret": "s3cret"},
    )
    assert resp.status_code == 202
    assert resp.json()["event_id"]


def test_webhook_empty_payload_422(client: TestClient) -> None:
    resp = client.post(
        "/webhooks/news", json={"author": "x"}, headers={"X-Webhook-Secret": "s3cret"}
    )
    assert resp.status_code == 422


def test_parse_feeds_setting() -> None:
    assert parse_feeds_setting("a, b ,, c") == ["a", "b", "c"]
    assert parse_feeds_setting("") == []


def test_parse_entry_maps_fields() -> None:
    payload = parse_entry(
        {"id": "g1", "title": "T", "summary": "S", "link": "http://x", "published": "2026-01-01"}
    )
    assert payload["id"] == "g1"
    assert payload["title"] == "T"
    assert payload["content"] == "S"
    assert payload["url"] == "http://x"


@pytest.mark.skipif(
    importlib.util.find_spec("feedparser") is None, reason="feedparser not installed"
)
def test_rss_poll_parses_static_feed(tmp_path) -> None:
    from app.ingestion.rss_poller import _poll_feed_sync

    rss = """<?xml version="1.0"?>
    <rss version="2.0"><channel><title>t</title>
      <item><title>Hello World</title><description>desc</description>
      <link>http://e/1</link><guid>1</guid></item>
    </channel></rss>"""
    feed_file = tmp_path / "feed.xml"
    feed_file.write_text(rss)
    events = _poll_feed_sync(str(feed_file), {})
    assert any(e.title == "Hello World" and e.source == "rss" for e in events)
