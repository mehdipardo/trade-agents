"""Notifier node.

Runs on EVERY path. Formats its output from the final ``status``, broadcasts a
``pipeline_done`` WebSocket message with the full serialized state + per-node
timings, and fires a Slack notification as a non-blocking background task (the
graph never waits on Slack).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from app.api.ws import emit
from app.config import get_settings
from app.graph.state import TradingState
from app.graph.timing import timed_node
from app.logging_config import get_logger
from app.services.slack import format_slack_message, send_slack

log = get_logger("app.graph.notifier")

_STATUS_EMOJI = {
    "executed": "🟢",
    "rejected_risk": "🚫",
    "skipped_neutral": "⚪",
    "skipped_duplicate": "⚪",
    "skipped_stale": "🕒",
    "failed": "🔴",
    "received": "⚪",
}


def _summarize(state: TradingState) -> dict[str, Any]:
    signal = state["signal"]
    order = state["order"]
    risk = state["risk"]
    event = state["event"]
    total_latency_ms = (
        datetime.now(UTC) - event.received_at
    ).total_seconds() * 1000
    return {
        "event_id": event.id,
        "status": state["status"],
        "emoji": _STATUS_EMOJI.get(state["status"], "⚪"),
        "title": event.title,
        "url": event.url,
        "source": event.source,
        "published_at": event.published_at.isoformat() if event.published_at else None,
        "received_at": event.received_at.isoformat(),
        "sentiment": signal.sentiment if signal else None,
        "intensity": signal.intensity if signal else None,
        "asset": signal.asset if signal else None,
        "confidence": signal.confidence if signal else None,
        "rationale": signal.rationale if signal else None,
        "side": risk.side if risk else None,
        "position_size_quote": risk.position_size_quote if risk else None,
        "reject_reason": risk.reject_reason if risk else None,
        "order_status": order.status if order else None,
        "avg_price": order.avg_price if order else None,
        "amount": order.amount if order else None,
        "error": state["error"],
        "total_latency_ms": round(total_latency_ms, 1),
        "timings_ms": state["timings_ms"],
    }


@timed_node("notifier")
async def notifier_node(state: TradingState) -> dict[str, Any]:
    summary = _summarize(state)
    log.info("pipeline_done", **summary)

    # Persist to the dashboard history buffer.
    from app.services.store import get_store

    await get_store().record_history(summary)

    # Broadcast the full pipeline result to dashboard clients.
    await emit(
        "pipeline_done",
        event_id=summary["event_id"],
        payload=summary,
        timings_ms=state["timings_ms"],
    )

    # Slack, fire-and-forget (do not block the graph).
    settings = get_settings()
    if settings.slack_webhook_url:
        text = format_slack_message(summary)
        asyncio.create_task(send_slack(settings.slack_webhook_url, text))

    return {}
