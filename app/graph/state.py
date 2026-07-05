"""LangGraph pipeline state.

This TypedDict is the shared state threaded through every node. The exact set
of fields is part of the project contract and must be preserved across steps.
"""

from __future__ import annotations

from typing import Literal, TypedDict

from app.models.schemas import NewsEvent, OrderResult, RiskVerdict, Signal

Status = Literal[
    "received",  # event normalized, entered the graph
    "skipped_duplicate",  # stopped by dedup
    "skipped_neutral",  # NEUTRAL / confidence < threshold / asset null
    "rejected_risk",  # risk engine veto
    "executed",  # order placed on the testnet
    "failed",  # technical error (LLM, exchange, network)
]


class TradingState(TypedDict):
    event: NewsEvent
    is_duplicate: bool
    signal: Signal | None
    risk: RiskVerdict | None
    order: OrderResult | None
    status: Status
    error: str | None
    timings_ms: dict[str, float]  # {"dedup": 4.1, "analyst": 412.0, ...} -> dashboard


def initial_state(event: NewsEvent) -> TradingState:
    """Build the initial graph state for a freshly received event."""
    return {
        "event": event,
        "is_duplicate": False,
        "signal": None,
        "risk": None,
        "order": None,
        "status": "received",
        "error": None,
        "timings_ms": {},
    }
