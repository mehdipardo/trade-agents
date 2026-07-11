"""LangGraph topology.

Builds and compiles the trading pipeline:

    entry -> dedup --(duplicate/stale)------------> notifier -> END
                   \\--(new)--> analyst --(neutral/low-conf/null/err)--> notifier
                                        \\--(tradable)--> risk --(rejected)--> notifier
                                                              \\--(approved)--> executor -> notifier

Routing is driven by the ``status`` field, which each node advances:
- ``received``          -> keep flowing to the next node
- ``skipped_duplicate`` -> dedup stop
- ``skipped_neutral``   -> analyst stop
- ``rejected_risk``     -> risk stop
- ``executed``          -> executor success
- ``failed``            -> any node error (isolated by ``timed_node``)
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from langgraph.graph import END, START, StateGraph

from app.graph.nodes.analyst import analyst_node
from app.graph.nodes.dedup import dedup_node
from app.graph.nodes.executor import executor_node
from app.graph.nodes.notifier import notifier_node
from app.graph.nodes.risk import risk_node
from app.graph.state import TradingState


def _route_after_dedup(state: TradingState) -> Literal["analyst", "notifier"]:
    # Only a still-flowing ("received") state proceeds; duplicate/stale stop here.
    return "analyst" if state["status"] == "received" else "notifier"


def _route_after_analyst(state: TradingState) -> Literal["risk", "notifier"]:
    # Only a still-flowing ("received") state carries a tradable signal onward.
    return "risk" if state["status"] == "received" else "notifier"


def _route_after_risk(state: TradingState) -> Literal["executor", "notifier"]:
    return "executor" if state["status"] == "received" else "notifier"


def build_graph():
    """Construct and compile the trading StateGraph."""
    graph = StateGraph(TradingState)

    graph.add_node("dedup", dedup_node)
    graph.add_node("analyst", analyst_node)
    graph.add_node("risk", risk_node)
    graph.add_node("executor", executor_node)
    graph.add_node("notifier", notifier_node)

    graph.add_edge(START, "dedup")
    graph.add_conditional_edges(
        "dedup", _route_after_dedup, {"analyst": "analyst", "notifier": "notifier"}
    )
    graph.add_conditional_edges(
        "analyst", _route_after_analyst, {"risk": "risk", "notifier": "notifier"}
    )
    graph.add_conditional_edges(
        "risk", _route_after_risk, {"executor": "executor", "notifier": "notifier"}
    )
    graph.add_edge("executor", "notifier")
    graph.add_edge("notifier", END)

    return graph.compile()


@lru_cache(maxsize=1)
def get_graph():
    """Return the compiled graph singleton (built once per process)."""
    return build_graph()
