"""Risk node.

Étape 2: an APPROVE stub that produces a plausible ``RiskVerdict``. The pure,
fully-tested deterministic rules (sizing, SL/TP, circuit breakers, kill switch)
land in Étape 4 and will be called from here.
"""

from __future__ import annotations

from typing import Any

from app.graph.state import TradingState
from app.graph.timing import timed_node
from app.models.schemas import RiskVerdict

# Fixed defaults for the mock (real values come from config in Étape 4).
_MOCK_POSITION_SIZE_QUOTE = 25.0
_MOCK_STOP_LOSS_PCT = 1.5
_MOCK_TAKE_PROFIT_PCT = 3.0


@timed_node("risk")
async def risk_node(state: TradingState) -> dict[str, Any]:
    signal = state["signal"]
    assert signal is not None  # routing guarantees a tradable signal here

    side = "buy" if signal.sentiment == "BULL" else "sell"
    verdict = RiskVerdict(
        approved=True,
        reject_reason=None,
        side=side,
        position_size_quote=_MOCK_POSITION_SIZE_QUOTE,
        stop_loss_pct=_MOCK_STOP_LOSS_PCT,
        take_profit_pct=_MOCK_TAKE_PROFIT_PCT,
    )
    return {
        "risk": verdict,
        "status": "received" if verdict.approved else "rejected_risk",
    }
