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
    source: Literal[
        "webhook", "rss", "simulator", "social", "economic", "regulatory", "news",
        "technical",
    ]
    author: str | None = None
    title: str
    content: str = ""
    url: str | None = None
    published_at: datetime | None = None
    received_at: datetime  # stamped on reception (UTC)
    # Structured payload for non-news events (e.g. a technical setup carries its
    # direction/confidence here so the analyst can skip the LLM entirely).
    meta: dict[str, Any] | None = None


class Signal(BaseModel):
    """The analyst LLM's structured classification of a news event."""

    sentiment: Sentiment
    intensity: int = Field(ge=1, le=5)
    asset: str | None = None  # must be in ASSET_WHITELIST, otherwise None
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(max_length=250)
    event_type: EventType
    # How cleanly this maps to a directional trade on the mapped asset (ease to
    # long/short): 1 = vague/no clean trade, 5 = obvious directional trade.
    actionability: int = Field(default=3, ge=1, le=5)
    # Expected price-impact magnitude 1-10 (combines intensity, actionability
    # and surprise element). Scores >= high_impact_threshold trigger the
    # strategy's leverage boost.
    impact_score: int = Field(default=5, ge=1, le=10)


class RiskVerdict(BaseModel):
    """The deterministic risk engine's decision for a signal."""

    approved: bool
    reject_reason: str | None = None
    side: Literal["buy", "sell"] | None = None
    position_size_quote: float | None = None  # notional in USDT
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    # Runner mechanism (partial take-profit).
    runner_pct: float | None = None  # fraction of the position kept as runner (0-1)
    runner_tp_pct: float | None = None  # runner take-profit target
    # High-conviction size boost (1 by default, x multiplier on high impact).
    leverage: int | None = None
    # Margin leverage: margin locked = notional / margin_leverage. Frees capital
    # for other triggers without changing the SL/TP value (those are on notional).
    margin_leverage: int | None = None
    margin_quote: float | None = None  # margin actually locked for this position
    # Human-readable summary of the technical/bias confluence applied to sizing
    # (e.g. "trend-aligned; bias-aligned" or "counter-trend: size reduced").
    confluence: str | None = None


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
