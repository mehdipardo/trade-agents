"""Node instrumentation.

``timed_node`` wraps an async node so that:
- its wall-clock latency is recorded in ``timings_ms[node_name]``;
- any exception is caught and converted into ``status="failed"`` + ``error``
  (the graph must never crash the worker).

Wrapped nodes return a partial state update *without* worrying about timing or
error handling.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any

from app.graph.state import TradingState
from app.logging_config import get_logger

log = get_logger("app.graph")

NodeFn = Callable[[TradingState], Awaitable[dict[str, Any]]]


def timed_node(name: str) -> Callable[[NodeFn], NodeFn]:
    """Decorate a node function with timing and error isolation."""

    def decorator(fn: NodeFn) -> NodeFn:
        @functools.wraps(fn)
        async def wrapper(state: TradingState) -> dict[str, Any]:
            t0 = perf_counter()
            base_timings = dict(state.get("timings_ms", {}))
            try:
                update = await fn(state)
            except Exception as exc:  # noqa: BLE001 - isolate node failures
                dt = (perf_counter() - t0) * 1000
                base_timings[name] = round(dt, 2)
                log.error(
                    "node_failed",
                    node=name,
                    event_id=state["event"].id,
                    error=str(exc),
                )
                return {
                    "status": "failed",
                    "error": f"{name}: {exc}",
                    "timings_ms": base_timings,
                }
            dt = (perf_counter() - t0) * 1000
            # Merge this node's timing with any timings the node itself added.
            merged = {**base_timings, name: round(dt, 2), **update.get("timings_ms", {})}
            update["timings_ms"] = merged
            return update

        return wrapper

    return decorator
