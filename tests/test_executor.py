"""Étape 5 tests: executor (offline + mocked exchange) and SL/TP monitor."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.config import get_settings
from app.graph.nodes.executor import client_order_id, executor_node
from app.graph.state import initial_state
from app.models.schemas import NewsEvent, RiskVerdict, Signal
from app.services.exchange import ExchangeClient, set_exchange
from app.services.position_monitor import exit_reason, realized_pnl
from app.services.store import InMemoryStore, get_store, set_store


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("EXCHANGE_SANDBOX", "true")
    get_settings.cache_clear()
    set_store(InMemoryStore())
    set_exchange(None)
    yield
    get_settings.cache_clear()
    set_store(None)
    set_exchange(None)


def _state(side: str = "buy", asset: str = "BTC/USDT"):
    event = NewsEvent(
        id="evt-x", source="simulator", title="t", received_at=datetime.now(UTC)
    )
    st = initial_state(event)
    st["signal"] = Signal(
        sentiment="BULL" if side == "buy" else "BEAR",
        intensity=4,
        asset=asset,
        confidence=0.9,
        rationale="r",
        event_type="macro",
    )
    st["risk"] = RiskVerdict(
        approved=True, side=side, position_size_quote=30.0, stop_loss_pct=1.5, take_profit_pct=3.0
    )
    return st


# --- offline paper fill ---------------------------------------------------


async def test_offline_fill_executes_and_records_position() -> None:
    update = await executor_node(_state("buy"))
    assert update["status"] == "executed"
    assert update["order"].status == "filled"
    assert update["order"].side == "buy"
    store = get_store()
    assert await store.has_open_position("BTC/USDT")
    assert await store.trades_last_hour() == 1


async def test_client_order_id_is_deterministic() -> None:
    assert client_order_id("abc") == client_order_id("abc")
    assert client_order_id("abc") != client_order_id("def")


# --- mocked live exchange -------------------------------------------------


class _FakeCCXT:
    def __init__(self) -> None:
        self.created: list = []

    async def fetch_ticker(self, symbol):  # noqa: ANN001
        return {"last": 100.0}

    def amount_to_precision(self, symbol, amount):  # noqa: ANN001
        return round(amount, 3)

    async def create_order(self, symbol, type_, side, amount, price, params):  # noqa: ANN001
        self.created.append((symbol, side, amount, params))
        return {"id": "oid-1", "average": 100.0, "filled": amount, "status": "closed"}


async def test_live_fill_uses_exchange_and_idempotent_id() -> None:
    fake = _FakeCCXT()
    set_exchange(ExchangeClient(fake))
    update = await executor_node(_state("sell", asset="BTC/USD:USD"))
    assert update["status"] == "executed"
    assert update["order"].side == "sell"
    assert update["order"].order_id == "oid-1"
    # clientOrderId passed through for idempotency.
    assert fake.created[0][3]["clientOrderId"] == client_order_id("evt-x")


# --- pure SL/TP logic -----------------------------------------------------


@pytest.mark.parametrize(
    "side,price,expected",
    [
        ("buy", 98.0, "stop_loss"),   # -2% <= -1.5% SL
        ("buy", 103.5, "take_profit"),  # +3.5% >= +3% TP
        ("buy", 100.5, None),
        ("sell", 102.0, "stop_loss"),  # +2% >= +1.5% SL for short
        ("sell", 96.0, "take_profit"),  # -4% <= -3% TP for short
        ("sell", 99.5, None),
    ],
)
def test_exit_reason(side: str, price: float, expected) -> None:
    assert exit_reason(side, 100.0, price, 1.5, 3.0) == expected


def test_realized_pnl_long_and_short() -> None:
    assert realized_pnl("buy", 100.0, 110.0, 2.0) == pytest.approx(20.0)
    assert realized_pnl("sell", 100.0, 90.0, 2.0) == pytest.approx(20.0)
    assert realized_pnl("sell", 100.0, 110.0, 2.0) == pytest.approx(-20.0)
