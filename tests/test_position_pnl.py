"""Live unrealized PnL enrichment + triggering-news capture on positions."""

from __future__ import annotations

import pytest

from app.api.routes_dashboard import _enrich_position


@pytest.fixture(autouse=True)
def _mark(monkeypatch: pytest.MonkeyPatch):
    """Patch the price provider so enrichment is hermetic (no network)."""
    from app.services import prices

    async def fake_get_price(symbol: str):  # noqa: ANN202
        return {"BTC/USDT": 64724.0}.get(symbol)

    monkeypatch.setattr(prices, "get_price", fake_get_price)
    monkeypatch.setattr(prices, "last_source", lambda s: "binance")
    yield


async def test_long_in_profit_reports_positive_unrealized() -> None:
    pos = {
        "asset": "BTC/USDT", "side": "buy", "entry_price": 64043.99,
        "amount": 666.67 / 64043.99, "margin_quote": 133.33,
    }
    out = await _enrich_position(pos)
    assert out["mark_price"] == 64724.0
    assert out["mark_source"] == "binance"
    # ~+$7 gross on ~0.0104 BTC * ~680 move, minus a small round-trip fee -> still +.
    assert out["unrealized_pnl"] > 0
    assert out["price_move_pct"] > 0  # long, price up
    assert out["unrealized_pct"] > 0  # ROE on margin


async def test_short_when_price_up_reports_loss() -> None:
    pos = {
        "asset": "BTC/USDT", "side": "sell", "entry_price": 63047.4,
        "amount": 0.01, "margin_quote": 133.33,
    }
    out = await _enrich_position(pos)
    assert out["unrealized_pnl"] < 0  # short, price rose
    assert out["price_move_pct"] < 0


async def test_no_mark_price_yields_null_fields() -> None:
    pos = {"asset": "DOGE/USDT", "side": "buy", "entry_price": 0.15, "amount": 100}
    out = await _enrich_position(pos)  # no mark for DOGE in the fake
    assert out["mark_price"] is None
    assert out["unrealized_pnl"] is None
    assert out["unrealized_pct"] is None


def test_position_detail_captures_triggering_news() -> None:
    from datetime import UTC, datetime

    from app.graph.nodes.executor import _news_ref, _position_detail
    from app.models.schemas import NewsEvent

    event = NewsEvent(
        id="e1", source="social", title="Trump: tariffs incoming",
        url="https://truthsocial.com/@realDonaldTrump/1", content="…",
        published_at=datetime(2026, 7, 17, 17, 11, tzinfo=UTC),
        received_at=datetime.now(UTC),
    )
    detail = _position_detail(
        "BTC/USDT", "buy", 64043.99, 0.01, 1.5, 3.0, 0.2, 50.0, 3, 5, 8,
        {"sentiment": "BULL"}, _news_ref(event),
    )
    news = detail["news"]
    assert news["title"] == "Trump: tariffs incoming"
    assert news["url"].endswith("/1")
    assert news["source"] == "social"
    assert news["published_at"].startswith("2026-07-17T17:11")


def test_notifier_summary_has_date_and_source() -> None:
    from datetime import UTC, datetime

    from app.graph.nodes.notifier import _summarize
    from app.graph.state import initial_state
    from app.models.schemas import NewsEvent

    event = NewsEvent(
        id="e2", source="rss", title="CPI cooler than expected",
        url="https://example.com/cpi", content="…",
        published_at=datetime(2026, 7, 17, 12, 30, tzinfo=UTC),
        received_at=datetime.now(UTC),
    )
    state = initial_state(event)
    state["status"] = "skipped_neutral"
    summary = _summarize(state)
    assert summary["source"] == "rss"
    assert summary["url"] == "https://example.com/cpi"
    assert summary["published_at"].startswith("2026-07-17T12:30")
