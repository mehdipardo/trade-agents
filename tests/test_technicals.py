"""Technical layer: indicators, setup detection, confluence rules, bias."""

from __future__ import annotations

import pytest

from app.models.schemas import Signal
from app.risk.rules import RiskConfig, RiskContext, evaluate
from app.services.technicals import assess, detect_setup, ema, rsi, swing_levels

# --- candle builders -------------------------------------------------------


def _candles(closes: list[float], spread: float = 0.5) -> list[dict]:
    return [
        {"time": i, "open": c, "high": c + spread, "low": c - spread, "close": c}
        for i, c in enumerate(closes)
    ]


def _uptrend(n: int = 80, start: float = 100.0) -> list[float]:
    # +1.0 / -0.6 alternation: net rise, but enough down-closes to keep RSI ~64
    # (out of the exhausted zone) so the trend component is what's under test.
    out = [start]
    for i in range(n - 1):
        out.append(out[-1] + (1.0 if i % 2 == 0 else -0.6))
    return out


def _downtrend(n: int = 80, start: float = 130.0) -> list[float]:
    out = [start]
    for i in range(n - 1):
        out.append(out[-1] - (1.0 if i % 2 == 0 else -0.6))
    return out


# --- indicators ------------------------------------------------------------


def test_ema_and_rsi_basics() -> None:
    closes = [float(i) for i in range(1, 61)]  # steady rise
    assert ema(closes, 20) is not None
    assert ema(closes, 20) > ema(closes, 50)  # fast above slow in an uptrend
    assert rsi(closes) == 100.0  # no down closes -> max RSI
    assert rsi([1.0, 2.0], period=14) is None  # not enough history
    assert ema([1.0], 20) is None


def test_swing_levels_exclude_breakout_candle() -> None:
    closes = [100.0] * 60
    candles = _candles(closes)
    candles[-1]["high"] = 999.0  # the breakout candle itself
    levels = swing_levels(candles, lookback=50, exclude=2)
    assert levels is not None
    support, resistance = levels
    # The excluded last candle must not define resistance.
    assert resistance == pytest.approx(100.5)
    assert support == pytest.approx(99.5)


# --- assess (confluence view) ---------------------------------------------


def test_assess_long_in_uptrend_is_positive() -> None:
    view = assess(_candles(_uptrend()), "buy")
    assert view is not None
    assert view.trend == "up"
    assert view.score >= 1


def test_assess_long_in_downtrend_flags_counter_trend() -> None:
    view = assess(_candles(_downtrend()), "buy")
    assert view is not None
    assert view.trend == "down"
    # Counter-trend (-1) but RSI has room (+1) -> net 0: penalized vs an aligned
    # trade (+1/+2) yet not vetoed — buying a dip is not chasing.
    assert view.score <= 0
    assert any("counter-trend" in r for r in view.reasons)


def test_assess_short_into_exhausted_fall_gets_no_bonus() -> None:
    # Chasing: shorting AFTER a straight-line collapse (RSI ~0) loses the
    # momentum point despite the aligned trend — the late-chase the gate exists
    # for. Trend +1, RSI exhausted -1 -> net 0, never a boost.
    closes = [200.0 - i * 1.5 for i in range(80)]  # monotonic fall -> RSI 0
    view = assess(_candles(closes), "sell")
    assert view is not None
    assert view.trend == "down" and view.rsi <= 30
    assert view.score == 0
    assert any("exhausted" in r for r in view.reasons)


def test_assess_needs_enough_candles() -> None:
    assert assess(_candles(_uptrend(20)), "buy") is None


# --- detect_setup ----------------------------------------------------------


def test_breakout_setup_detected() -> None:
    closes = _uptrend(78)
    top = max(c + 0.5 for c in closes)  # resistance incl. candle highs
    closes += [top - 0.2, top + 2.0]  # prev inside range, last breaks out
    setup = detect_setup(_candles(closes))
    assert setup is not None and setup["direction"] == "BULL"
    assert "resistance" in setup["reason"]


def test_breakdown_setup_detected() -> None:
    closes = _downtrend(78)
    bottom = min(c - 0.5 for c in closes)
    closes += [bottom + 0.2, bottom - 2.0]
    setup = detect_setup(_candles(closes))
    assert setup is not None and setup["direction"] == "BEAR"


def test_no_setup_inside_range() -> None:
    closes = [100.0 + (i % 5) * 0.3 for i in range(80)]  # flat chop
    assert detect_setup(_candles(closes)) is None


# --- risk-engine confluence -----------------------------------------------


def _signal(conf: float = 0.8) -> Signal:
    return Signal(
        sentiment="BULL", intensity=4, asset="BTC/USDT", confidence=conf,
        rationale="test", event_type="macro", actionability=4, impact_score=5,
    )


def _ctx(**kw) -> RiskContext:
    base = dict(
        equity_quote=1000.0, trades_last_hour=0, daily_pnl_quote=0.0,
        kill_switch_active=False, asset_in_cooldown=False,
        open_position_on_asset=False, free_capital_quote=1000.0,
    )
    base.update(kw)
    return RiskContext(**base)


def test_counter_bias_halves_risk() -> None:
    cfg = RiskConfig()
    aligned = evaluate(_signal(), _ctx(operator_bias="BULL"), cfg)
    against = evaluate(_signal(), _ctx(operator_bias="BEAR"), cfg)
    assert aligned.approved and against.approved
    assert against.position_size_quote == pytest.approx(
        aligned.position_size_quote / 2, abs=0.01
    )
    assert "counter-bias" in (against.confluence or "")


def test_weak_signal_strongly_counter_trend_is_vetoed() -> None:
    verdict = evaluate(_signal(conf=0.65), _ctx(technical_score=-2), RiskConfig())
    assert not verdict.approved
    assert "technicals strongly against" in (verdict.reject_reason or "")


def test_confident_signal_survives_counter_trend_with_reduced_size() -> None:
    cfg = RiskConfig()
    neutral = evaluate(_signal(conf=0.8), _ctx(), cfg)
    counter = evaluate(_signal(conf=0.8), _ctx(technical_score=-2), cfg)
    assert counter.approved
    assert counter.position_size_quote < neutral.position_size_quote


def test_trend_confirmed_boosts_risk() -> None:
    cfg = RiskConfig()
    neutral = evaluate(_signal(), _ctx(), cfg)
    confirmed = evaluate(_signal(), _ctx(technical_score=2), cfg)
    assert confirmed.position_size_quote > neutral.position_size_quote
    assert "trend-confirmed" in (confirmed.confluence or "")


def test_no_technical_data_means_no_adjustment() -> None:
    cfg = RiskConfig()
    a = evaluate(_signal(), _ctx(), cfg)
    b = evaluate(_signal(), _ctx(technical_score=None), cfg)
    assert a.position_size_quote == b.position_size_quote
    assert b.approved


# --- technical events bypass the LLM --------------------------------------


def test_technical_signal_built_from_meta() -> None:
    from datetime import UTC, datetime

    from app.config import get_settings
    from app.models.schemas import NewsEvent
    from app.services.llm import technical_signal

    event = NewsEvent(
        id="ta-1", source="technical", title="Technical setup: BTC/USDT breakout",
        received_at=datetime.now(UTC),
        meta={"symbol": "BTC/USDT", "direction": "BULL", "confidence": 0.7,
              "impact": 5, "reason": "close broke above resistance"},
    )
    sig = technical_signal(event, get_settings())
    assert sig.sentiment == "BULL"
    assert sig.asset == "BTC/USDT"
    assert sig.confidence == 0.7
    assert "resistance" in sig.rationale


def test_technical_signal_off_whitelist_asset_nulled() -> None:
    from datetime import UTC, datetime

    from app.config import get_settings
    from app.models.schemas import NewsEvent
    from app.services.llm import technical_signal

    event = NewsEvent(
        id="ta-2", source="technical", title="setup", received_at=datetime.now(UTC),
        meta={"symbol": "SHIB/USDT", "direction": "BULL", "confidence": 0.7},
    )
    assert technical_signal(event, get_settings()).asset is None


# --- scanner ---------------------------------------------------------------


async def test_scan_once_emits_setup_and_respects_cooldown(monkeypatch) -> None:
    import asyncio

    from app.services.store import InMemoryStore, set_store
    from app.sources import technical_scanner as scanner

    scanner.reset_state()
    set_store(InMemoryStore())

    closes = _uptrend(78)
    top = max(c + 0.5 for c in closes)
    breakout = _candles(closes + [top - 0.2, top + 2.0])

    async def fake_klines(symbol, interval="5m", limit=200):  # noqa: ANN001
        return breakout if symbol == "BTC/USDT" else None

    monkeypatch.setattr("app.services.prices.klines", fake_klines)
    queue: asyncio.Queue = asyncio.Queue()

    assert await scanner.scan_once(queue) == 1
    event = queue.get_nowait()
    assert event.source == "technical"
    assert event.meta["direction"] == "BULL"
    assert event.meta["symbol"] == "BTC/USDT"

    # Second sweep inside the emit cooldown -> nothing new.
    assert await scanner.scan_once(queue) == 0


async def test_scan_once_skips_open_positions(monkeypatch) -> None:
    import asyncio

    from app.services.store import InMemoryStore, set_store
    from app.sources import technical_scanner as scanner

    scanner.reset_state()
    store = InMemoryStore()
    set_store(store)
    await store.set_position("BTC/USDT", is_open=True, detail={"side": "buy"})

    called = []

    async def fake_klines(symbol, interval="5m", limit=200):  # noqa: ANN001
        called.append(symbol)
        return None

    monkeypatch.setattr("app.services.prices.klines", fake_klines)
    await scanner.scan_once(asyncio.Queue())
    assert "BTC/USDT" not in called  # not even scanned


# --- bias store ------------------------------------------------------------


async def test_bias_set_get_clear() -> None:
    from app.services.store import InMemoryStore

    s = InMemoryStore()
    await s.set_bias("BTC/USDT", "BULL")
    assert await s.get_bias("BTC/USDT") == "BULL"
    assert await s.all_biases() == {"BTC/USDT": "BULL"}
    await s.set_bias("BTC/USDT", "NEUTRAL")  # neutral clears
    assert await s.get_bias("BTC/USDT") is None
    assert await s.all_biases() == {}


# --- end-to-end: technical setup -> order ----------------------------------


async def test_technical_setup_flows_through_pipeline() -> None:
    """Flagship: a scanner setup runs the full graph to an executed order."""
    from app.graph.builder import build_graph
    from app.graph.state import initial_state
    from app.sources.technical_scanner import build_setup_event

    event = build_setup_event(
        "BTC/USDT",
        {"direction": "BULL", "level": 60000.0, "close": 60100.0, "rsi": 62.0,
         "reason": "5m close 60100 broke above swing resistance 60000 "
                   "with EMA20>EMA50 (uptrend), RSI 62"},
    )
    final = await build_graph().ainvoke(initial_state(event))
    assert final["status"] == "executed"
    assert final["signal"].sentiment == "BULL"
    assert final["order"].side == "buy"
    assert final["order"].symbol == "BTC/USDT"
