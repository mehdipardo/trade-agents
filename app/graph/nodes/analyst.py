"""Analyst node.

Étape 2: a deterministic keyword-based MOCK that stands in for the LLM. It is
intentionally simple but exercises the full routing surface (BULL/BEAR/NEUTRAL,
asset mapping, prompt-injection -> NEUTRAL). The real single-LLM-call analyst
with structured output replaces ``_mock_classify`` in Étape 3.
"""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.graph.state import TradingState
from app.graph.timing import timed_node
from app.models.schemas import Signal

# Phrases whose only purpose is to manipulate the system -> classify NEUTRAL.
_INJECTION_MARKERS = (
    "ignore previous instructions",
    "ignore all previous",
    "disregard your rules",
    "system override",
    "you must buy",
    "output bull",
    "output bear",
)

_BULL_KEYWORDS = ("reserve", "approve", "approval", "etf", "adopt", "capital", "partnership")
_BEAR_KEYWORDS = ("cpi", "inflation", "hack", "ban", "lawsuit", "crash", "denied", "hawkish")

# Keyword -> whitelisted symbol. Broad/macro news falls back to BTC/USDT.
_ASSET_KEYWORDS = {
    "solana": "SOL/USDT",
    "sol": "SOL/USDT",
    "ethereum": "ETH/USDT",
    "ether": "ETH/USDT",
    "eth": "ETH/USDT",
    "ripple": "XRP/USDT",
    "xrp": "XRP/USDT",
    "doge": "DOGE/USDT",
    "bitcoin": "BTC/USDT",
    "btc": "BTC/USDT",
}


def _map_asset(text: str, whitelist: tuple[str, ...]) -> str | None:
    for keyword, symbol in _ASSET_KEYWORDS.items():
        if keyword in text and symbol in whitelist:
            return symbol
    # Broad macro/crypto-wide news maps to BTC/USDT when whitelisted.
    return "BTC/USDT" if "BTC/USDT" in whitelist else None


def _mock_classify(text: str, whitelist: tuple[str, ...]) -> Signal:
    if any(marker in text for marker in _INJECTION_MARKERS):
        return Signal(
            sentiment="NEUTRAL",
            intensity=1,
            asset=None,
            confidence=0.9,
            rationale="Content appears to be a manipulation attempt; no real market impact.",
            event_type="other",
        )

    is_bull = any(k in text for k in _BULL_KEYWORDS)
    is_bear = any(k in text for k in _BEAR_KEYWORDS)

    if is_bull == is_bear:  # neither, or ambiguous both -> neutral
        return Signal(
            sentiment="NEUTRAL",
            intensity=1,
            asset=None,
            confidence=0.5,
            rationale="No clear tradable catalyst detected (mock analyst).",
            event_type="other",
        )

    sentiment = "BULL" if is_bull else "BEAR"
    return Signal(
        sentiment=sentiment,
        intensity=4,
        asset=_map_asset(text, whitelist),
        confidence=0.8,
        rationale=f"Mock keyword classification -> {sentiment}.",
        event_type="macro" if is_bear else "social",
    )


@timed_node("analyst")
async def analyst_node(state: TradingState) -> dict[str, Any]:
    settings = get_settings()
    event = state["event"]
    text = f"{event.title}\n{event.content}".lower()

    signal = _mock_classify(text, settings.asset_whitelist_set)

    tradable = (
        signal.sentiment != "NEUTRAL"
        and signal.confidence >= settings.confidence_threshold
        and signal.asset is not None
    )
    return {
        "signal": signal,
        "status": "received" if tradable else "skipped_neutral",
    }
