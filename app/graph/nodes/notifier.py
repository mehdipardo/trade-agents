"""Notifier node.

Runs on EVERY path and formats its output according to the final ``status``.
Étape 2: logs a ``pipeline_done`` record with the full serialized state and
per-node timings (this is what the dashboard will later consume). Slack + the
WebSocket broadcast are added in Étape 6.
"""

from __future__ import annotations

from typing import Any

from app.graph.state import TradingState
from app.graph.timing import timed_node
from app.logging_config import get_logger

log = get_logger("app.graph.notifier")

# Human/emoji hint per terminal status (used by Slack formatting in Étape 6).
_STATUS_EMOJI = {
    "executed": "🟢",
    "rejected_risk": "🚫",
    "skipped_neutral": "⚪",
    "skipped_duplicate": "⚪",
    "failed": "🔴",
    "received": "⚪",
}


def _summarize(state: TradingState) -> dict[str, Any]:
    signal = state["signal"]
    order = state["order"]
    risk = state["risk"]
    return {
        "event_id": state["event"].id,
        "status": state["status"],
        "emoji": _STATUS_EMOJI.get(state["status"], "⚪"),
        "title": state["event"].title,
        "sentiment": signal.sentiment if signal else None,
        "intensity": signal.intensity if signal else None,
        "asset": signal.asset if signal else None,
        "confidence": signal.confidence if signal else None,
        "reject_reason": risk.reject_reason if risk else None,
        "order_status": order.status if order else None,
        "side": order.side if order else None,
        "avg_price": order.avg_price if order else None,
        "amount": order.amount if order else None,
        "error": state["error"],
        "timings_ms": state["timings_ms"],
    }


@timed_node("notifier")
async def notifier_node(state: TradingState) -> dict[str, Any]:
    summary = _summarize(state)
    log.info("pipeline_done", **summary)
    # Étape 6: send Slack + broadcast the WSMessage("pipeline_done") here.
    return {}
