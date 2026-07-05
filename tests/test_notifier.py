"""Étape 6 tests: Slack formatting/sending and the WebSocket live feed."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.config import get_settings
from app.services.slack import _escape, format_slack_message, send_slack


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("EXCHANGE_SANDBOX", "true")
    monkeypatch.setenv("APP_ENV", "test")
    get_settings.cache_clear()
    from app.services.exchange import set_exchange
    from app.services.store import InMemoryStore, set_store

    set_store(InMemoryStore())
    set_exchange(None)
    yield
    get_settings.cache_clear()
    set_store(None)


# --- Slack ----------------------------------------------------------------


def test_escape_neutralizes_slack_controls() -> None:
    assert _escape("<b>a&b>") == "&lt;b&gt;a&amp;b&gt;"


def test_format_includes_latency_and_escapes_title() -> None:
    text = format_slack_message(
        {
            "emoji": "🟢",
            "status": "executed",
            "title": "Buy <script>",
            "sentiment": "BULL",
            "intensity": 4,
            "confidence": 0.85,
            "side": "buy",
            "position_size_quote": 20.0,
            "avg_price": 60000.0,
            "total_latency_ms": 412.0,
        }
    )
    assert "EXECUTED" in text
    assert "&lt;script&gt;" in text  # escaped
    assert "latency news→order: 412ms" in text
    assert "★★★★☆" in text


async def test_send_slack_noop_without_url() -> None:
    assert await send_slack(None, "hi") is False


@respx.mock
async def test_send_slack_posts_payload() -> None:
    route = respx.post("https://hooks.slack.test/xxx").mock(
        return_value=httpx.Response(200)
    )
    ok = await send_slack("https://hooks.slack.test/xxx", "hello")
    assert ok is True
    assert route.called
    assert b"hello" in route.calls.last.request.content


# --- WebSocket ------------------------------------------------------------


async def test_connection_manager_broadcast() -> None:
    from app.api.ws import ConnectionManager

    sent: list[str] = []

    class _FakeWS:
        async def send_text(self, data: str) -> None:
            sent.append(data)

    mgr = ConnectionManager()
    ws = _FakeWS()
    mgr._active.add(ws)  # type: ignore[arg-type]
    await mgr.broadcast({"type": "heartbeat", "event_id": "x"})
    assert sent and "heartbeat" in sent[0]


def test_ws_live_receives_pipeline_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("EXCHANGE_SANDBOX", "true")
    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app()) as client:
        with client.websocket_connect("/ws/live") as ws:
            resp = client.post("/admin/inject", json={"scenario": "trump_btc_bull"})
            assert resp.status_code == 202

            types_seen: list[str] = []
            for _ in range(12):
                msg = ws.receive_json()
                types_seen.append(msg["type"])
                assert msg["event_id"]
                if msg["type"] == "pipeline_done":
                    break

    assert "event_received" in types_seen
    assert "pipeline_done" in types_seen
    # pipeline_done carries the full timings.
    get_settings.cache_clear()
