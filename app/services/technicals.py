"""Deterministic technical analysis on OHLC candles.

Pure functions only (EMA / RSI / swing levels / trade-side scoring) so every
rule is unit-testable — the same discipline as the risk engine. No indicator
here is exotic: trend (EMA20/50), momentum exhaustion (RSI-14) and recent
swing support/resistance. That combination is the auditable core of most
"trend + levels" bots; we implement it in the open instead of trusting a
closed backtest.

Used by two consumers:
- the RISK GATE: a news signal fighting the technicals gets its risk budget
  reduced (or vetoed when weak AND strongly counter-trend);
- the SETUP SCANNER: emits standalone technical setups (breakout/breakdown
  in the trend direction) into the normal pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.logging_config import get_logger

log = get_logger("app.services.technicals")

MIN_CANDLES = 60  # below this, refuse to opine (return None, never guess)


def ema(values: list[float], period: int) -> float | None:
    """Classic exponential moving average of ``values`` (last value returned)."""
    if period <= 0 or len(values) < period:
        return None
    k = 2.0 / (period + 1)
    avg = sum(values[:period]) / period  # seed with the SMA
    for v in values[period:]:
        avg = v * k + avg * (1 - k)
    return avg


def rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder's RSI. Returns None when there is not enough history."""
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        gains += max(0.0, delta)
        losses += max(0.0, -delta)
    avg_gain, avg_loss = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(0.0, delta)) / period
        avg_loss = (avg_loss * (period - 1) + max(0.0, -delta)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def swing_levels(
    candles: list[dict], lookback: int = 50, exclude: int = 2
) -> tuple[float, float] | None:
    """(support, resistance) from recent swing lows/highs.

    The last ``exclude`` candles are left out so a breakout candle does not
    define the very level it is breaking.
    """
    if len(candles) < exclude + 5:
        return None
    window = candles[-(lookback + exclude) : -exclude] if exclude else candles[-lookback:]
    if not window:
        return None
    support = min(float(c["low"]) for c in window)
    resistance = max(float(c["high"]) for c in window)
    return support, resistance


@dataclass
class TechnicalView:
    """Snapshot of the technical state, scored for one trade side."""

    close: float
    ema_fast: float
    ema_slow: float
    rsi: float
    support: float
    resistance: float
    trend: str  # "up" | "down" | "flat"
    score: int  # -2 (strongly against the side) .. +2 (strongly aligned)
    reasons: list[str] = field(default_factory=list)


def _trend(ema_fast: float, ema_slow: float) -> str:
    # 0.1% dead-band so a hair's difference doesn't flip the trend label.
    if ema_fast > ema_slow * 1.001:
        return "up"
    if ema_fast < ema_slow * 0.999:
        return "down"
    return "flat"


def assess(candles: list[dict], side: str) -> TechnicalView | None:
    """Score the technical alignment of taking ``side`` ("buy"/"sell") now.

    Score components (each -1/0/+1, summed):
    - TREND: trading with the EMA20/50 trend is +1, against it -1.
    - MOMENTUM ROOM: entering long into RSI>=70 (or short into RSI<=30) is -1
      (chasing an exhausted move); comfortable RSI room is +1.
    Returns None when there is not enough data to have an opinion.
    """
    if len(candles) < MIN_CANDLES:
        return None
    closes = [float(c["close"]) for c in candles]
    ema_fast, ema_slow = ema(closes, 20), ema(closes, 50)
    r = rsi(closes)
    levels = swing_levels(candles)
    if ema_fast is None or ema_slow is None or r is None or levels is None:
        return None
    support, resistance = levels
    trend = _trend(ema_fast, ema_slow)

    score = 0
    reasons: list[str] = []
    long_side = side == "buy"

    if trend == ("up" if long_side else "down"):
        score += 1
        reasons.append(f"trend-aligned ({trend})")
    elif trend == ("down" if long_side else "up"):
        score -= 1
        reasons.append(f"counter-trend ({trend})")

    if long_side:
        if r >= 70:
            score -= 1
            reasons.append(f"RSI exhausted ({r:.0f})")
        elif r <= 60:
            score += 1
            reasons.append(f"RSI room ({r:.0f})")
    else:
        if r <= 30:
            score -= 1
            reasons.append(f"RSI exhausted ({r:.0f})")
        elif r >= 40:
            score += 1
            reasons.append(f"RSI room ({r:.0f})")

    return TechnicalView(
        close=closes[-1], ema_fast=ema_fast, ema_slow=ema_slow, rsi=r,
        support=support, resistance=resistance, trend=trend,
        score=score, reasons=reasons,
    )


async def assess_symbol(symbol: str, side: str) -> TechnicalView | None:
    """Fetch candles for ``symbol`` and assess ``side``. Best-effort: None on
    any missing data (TradFi symbols have no kline source yet) — a missing
    view NEVER blocks a trade, it just means no technical adjustment."""
    from app.services.prices import klines

    candles = await klines(symbol, interval="5m", limit=200)
    if not candles:
        return None
    return assess(candles, side)


def detect_setup(candles: list[dict]) -> dict | None:
    """Detect a standalone technical setup on the LAST closed candle.

    Deliberately strict — all three must hold:
    - BREAKOUT/BREAKDOWN: the close crosses the recent swing level (the
      previous close was still inside the range);
    - TREND: EMA20 vs EMA50 agrees with the direction;
    - NOT EXHAUSTED: RSI hasn't already overshot (<=72 for longs, >=28 shorts).
    Returns {direction, level, close, rsi, reason} or None.
    """
    if len(candles) < MIN_CANDLES:
        return None
    closes = [float(c["close"]) for c in candles]
    ema_fast, ema_slow = ema(closes, 20), ema(closes, 50)
    r = rsi(closes)
    levels = swing_levels(candles)
    if ema_fast is None or ema_slow is None or r is None or levels is None:
        return None
    support, resistance = levels
    last, prev = closes[-1], closes[-2]

    if last > resistance and prev <= resistance and ema_fast > ema_slow and r <= 72:
        return {
            "direction": "BULL",
            "level": resistance,
            "close": last,
            "rsi": r,
            "reason": (
                f"5m close {last:.6g} broke above swing resistance {resistance:.6g} "
                f"with EMA20>EMA50 (uptrend), RSI {r:.0f}"
            ),
        }
    if last < support and prev >= support and ema_fast < ema_slow and r >= 28:
        return {
            "direction": "BEAR",
            "level": support,
            "close": last,
            "rsi": r,
            "reason": (
                f"5m close {last:.6g} broke below swing support {support:.6g} "
                f"with EMA20<EMA50 (downtrend), RSI {r:.0f}"
            ),
        }
    return None
