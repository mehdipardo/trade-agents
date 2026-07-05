"""Tests for the source catalog + economic calendar + pre-armed watcher."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.sources import catalog, watcher
from app.sources.economic_calendar import (
    CalendarEvent,
    parse_forexfactory,
    rank_by_volatility,
    volatility_from_impact,
)
from app.sources.watcher import (
    build_release_event,
    in_watch_window,
    is_released,
)

# --- catalog --------------------------------------------------------------


def test_catalog_has_recommended_econ_default_on() -> None:
    ids = {s.id for s in catalog.list_specs()}
    assert {"econ_calendar", "trump_truthsocial", "sec_press", "congress_bills"} <= ids
    assert catalog.is_enabled("econ_calendar") is True  # default on


def test_toggle_source() -> None:
    assert catalog.set_enabled("trump_truthsocial", True) is True
    assert catalog.is_enabled("trump_truthsocial") is True
    assert catalog.set_enabled("trump_truthsocial", False) is False


def test_toggle_unknown_raises() -> None:
    with pytest.raises(KeyError):
        catalog.set_enabled("nope", True)


# --- calendar parsing / scoring ------------------------------------------


def test_volatility_from_impact() -> None:
    assert volatility_from_impact("High") == 5
    assert volatility_from_impact("red") == 5
    assert volatility_from_impact("Medium") == 3
    assert volatility_from_impact("Low") == 1
    assert volatility_from_impact("Holiday") == 0
    assert volatility_from_impact(None) == 0


def test_parse_forexfactory() -> None:
    items = [
        {
            "title": "Non-Farm Payrolls",
            "country": "USD",
            "date": "2026-07-10T12:30:00Z",
            "impact": "High",
            "forecast": "180K",
            "previous": "206K",
        },
        {
            "title": "Bank Holiday",
            "country": "EUR",
            "date": "2026-07-10T00:00:00Z",
            "impact": "Holiday",
        },
        {"title": "no date"},  # skipped
    ]
    events = parse_forexfactory(items)
    assert len(events) == 2
    nfp = events[0]
    assert nfp.title == "Non-Farm Payrolls"
    assert nfp.volatility == 5
    assert nfp.when.tzinfo is not None


def test_rank_by_volatility_orders_high_first_and_filters() -> None:
    now = datetime.now(UTC)
    h = timedelta(hours=1)
    events = [
        CalendarEvent(id="a", title="low", when=now + h, impact="Low", volatility=1),
        CalendarEvent(id="b", title="hi", when=now + 5 * h, impact="High", volatility=5),
        CalendarEvent(id="c", title="past", when=now - h, impact="High", volatility=5),
    ]
    ranked = rank_by_volatility(events, min_volatility=1)
    assert [e.id for e in ranked] == ["b", "a"]  # high first; past dropped


# --- watcher --------------------------------------------------------------


def test_in_watch_window_boundaries() -> None:
    when = datetime(2026, 7, 10, 12, 30, tzinfo=UTC)
    assert in_watch_window(when, when, lead_s=30, trail_s=600)
    assert in_watch_window(when, when - timedelta(seconds=20), lead_s=30, trail_s=600)
    assert not in_watch_window(when, when - timedelta(seconds=60), lead_s=30, trail_s=600)
    assert in_watch_window(when, when + timedelta(seconds=300), lead_s=30, trail_s=600)
    assert not in_watch_window(when, when + timedelta(seconds=900), lead_s=30, trail_s=600)


def test_is_released_and_build_event() -> None:
    ev = CalendarEvent(
        id="x", title="US CPI", when=datetime.now(UTC), impact="High",
        volatility=5, forecast="3.1%", previous="3.0%", actual="4.2%",
    )
    assert is_released(ev)
    news = build_release_event(ev)
    assert news.source == "economic"
    assert "4.2%" in news.title


async def test_poll_once_emits_when_released(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    armed_ev = CalendarEvent(id="cpi", title="US CPI", when=now, impact="High", volatility=5)
    watcher.arm(armed_ev)

    released = armed_ev.model_copy(update={"actual": "4.2%", "forecast": "3.1%"})

    async def fake_fetch(url: str):  # noqa: ANN001
        return [released]

    monkeypatch.setattr(watcher, "fetch_calendar", fake_fetch)
    queue: asyncio.Queue = asyncio.Queue()
    await watcher._poll_once(queue, "http://x")

    assert queue.qsize() == 1
    emitted = queue.get_nowait()
    assert emitted.source == "economic"
    assert not watcher.is_armed("cpi")  # disarmed after emit


# --- API ------------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("EXCHANGE_SANDBOX", "true")
    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()


def test_sources_and_toggle_endpoints(client: TestClient) -> None:
    body = client.get("/api/sources").json()
    assert any(s["id"] == "econ_calendar" and s["enabled"] for s in body["sources"])
    r = client.post("/admin/sources/trump_truthsocial/toggle", json={"enabled": True})
    assert r.status_code == 200 and r.json()["enabled"] is True
    assert client.post("/admin/sources/nope/toggle", json={"enabled": True}).status_code == 404


def test_calendar_and_arm_endpoints(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    ev = CalendarEvent(
        id="nfp1", title="NFP", when=now + timedelta(hours=2), impact="High", volatility=5
    )

    async def fake_fetch(url: str):  # noqa: ANN001
        return [ev]

    monkeypatch.setattr("app.api.routes_sources.fetch_calendar", fake_fetch)
    upcoming = client.get("/api/calendar/upcoming").json()["events"]
    assert upcoming and upcoming[0]["id"] == "nfp1" and upcoming[0]["armed"] is False

    armed = client.post("/admin/calendar/arm", json={"event_id": "nfp1", "armed": True})
    assert armed.status_code == 200 and armed.json()["armed"] is True
    # Unknown id (not cached) -> 404.
    assert client.post("/admin/calendar/arm", json={"event_id": "zzz"}).status_code == 404
