"""Analyst node.

The single LLM call per event. Delegates to ``app.services.llm.analyze`` which
handles structured output, retries, the NEUTRAL fallback and asset
post-validation. When no provider key is configured, that service transparently
uses a deterministic offline classifier so the pipeline still runs.
"""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.graph.state import TradingState
from app.graph.timing import timed_node
from app.services.llm import analyze


@timed_node("analyst")
async def analyst_node(state: TradingState) -> dict[str, Any]:
    settings = get_settings()
    signal = await analyze(state["event"], settings)

    tradable = (
        signal.sentiment != "NEUTRAL"
        and signal.confidence >= settings.confidence_threshold
        and signal.asset is not None
    )
    return {
        "signal": signal,
        "status": "received" if tradable else "skipped_neutral",
    }
