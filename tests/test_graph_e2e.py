"""Étape 2 tests: the mock graph end-to-end.

The LLM and exchange are mocked (the nodes are still deterministic mocks at
this step), so these run in CI without any external dependency.
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.graph.builder import build_graph
from app.graph.state import initial_state
from app.ingestion.simulator import load_scenario


@pytest.fixture(autouse=True)
def _safe_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("EXCHANGE_SANDBOX", "true")
    monkeypatch.setenv("APP_ENV", "test")
    get_settings.cache_clear()
    # Fresh risk state per test (the store is a process-wide singleton).
    from app.services.store import InMemoryStore, set_store

    set_store(InMemoryStore())
    yield
    get_settings.cache_clear()
    set_store(None)


async def _run(scenario: str) -> dict:
    graph = build_graph()
    event = load_scenario(scenario)
    return await graph.ainvoke(initial_state(event))


async def test_bull_scenario_executes() -> None:
    final = await _run("trump_btc_bull")
    assert final["status"] == "executed"
    assert final["signal"].sentiment == "BULL"
    assert final["signal"].asset == "BTC/USDT"
    assert final["order"].status == "filled"
    assert final["order"].side == "buy"
    # Complete per-node timings recorded.
    for node in ("dedup", "analyst", "risk", "executor"):
        assert node in final["timings_ms"]


async def test_etf_scenario_maps_sol_and_executes() -> None:
    final = await _run("sec_etf_approval")
    assert final["status"] == "executed"
    assert final["signal"].asset == "SOL/USDT"
    assert final["order"].symbol == "SOL/USDT"


async def test_bear_scenario_rejected_no_short_on_spot() -> None:
    # cpi_hot_bear -> BEAR with no open position -> risk veto (spot, no short).
    final = await _run("cpi_hot_bear")
    assert final["status"] == "rejected_risk"
    assert final["signal"].sentiment == "BEAR"
    assert "no short on spot" in final["risk"].reject_reason
    assert final["order"] is None


async def test_neutral_scenario_skipped() -> None:
    final = await _run("neutral_report")
    assert final["status"] == "skipped_neutral"
    assert final["signal"].sentiment == "NEUTRAL"
    assert final["order"] is None
    assert "analyst" in final["timings_ms"]
    assert "risk" not in final["timings_ms"]  # never reached risk


async def test_prompt_injection_is_neutral() -> None:
    final = await _run("prompt_injection")
    assert final["status"] == "skipped_neutral"
    assert final["signal"].sentiment == "NEUTRAL"
    assert final["order"] is None


async def test_timings_are_positive_floats() -> None:
    final = await _run("trump_btc_bull")
    assert all(isinstance(v, float) and v >= 0 for v in final["timings_ms"].values())
