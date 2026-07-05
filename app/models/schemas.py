"""Pydantic data models shared across the pipeline.

These schemas are the contract between ingestion, the LangGraph nodes, the
risk engine, the executor and the dashboard/WebSocket layer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Sentiment = Literal["BULL", "BEAR", "NEUTRAL"]
EventType = Literal["macro", "regulation", "social", "exchange", "tech", "other"]

WSMessageType = Literal[
    "event_received",
    "signal",
    "risk_verdict",
    "order",
    "pipeline_done",
    "heartbeat",
]


class NewsEvent(BaseModel):
    """A single, normalized inbound news event."""

    id: str  # uuid4 or a hash provided by the source
    source: Literal["webhook", "rss", "simulator", "social", "economic", "regulatory"]
    author: str | None = None
    title: str
    content: str = ""
    url: str | None = None
    published_at: datetime | None = None
    received_at: datetime  # stamped on reception (UTC)


class Signal(BaseModel):
    """The analyst LLM's structured classification of a news event."""

    sentiment: Sentiment
    intensity: int = Field(ge=1, le=5)
    asset: str | None = None  # must be in ASSET_WHITELIST, otherwise None
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(max_length=250)
    event_type: EventType


class RiskVerdict(BaseModel):
    """The deterministic risk engine's decision for a signal."""

    approved: bool
    reject_reason: str | None = None
    side: Literal["buy", "sell"] | None = None
    position_size_quote: float | None = None  # notional in USDT
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None


class OrderResult(BaseModel):
    """The outcome of an order submission on the exchange sandbox."""

    order_id: str | None = None
    client_order_id: str  # derived from event.id (idempotency)
    symbol: str
    side: str
    amount: float  # base quantity
    avg_price: float | None = None
    status: Literal["filled", "open", "rejected", "error"]
    exchange_latency_ms: int


class WSMessage(BaseModel):
    """WebSocket broadcast envelope (contract for the dashboard)."""

    type: WSMessageType
    event_id: str
    ts: str  # ISO-8601
    payload: dict[str, Any] = Field(default_factory=dict)
    timings_ms: dict[str, float] = Field(default_factory=dict)
