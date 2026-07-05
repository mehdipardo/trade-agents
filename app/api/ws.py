"""WebSocket live feed for the dashboard.

Contract (per the brief):
    { "type": "event_received" | "signal" | "risk_verdict" | "order"
              | "pipeline_done" | "heartbeat",
      "event_id": "...", "ts": "ISO-8601", "payload": {...},
      "timings_ms": {...} }

A module-level ``ConnectionManager`` fans out messages to all connected
clients. Producers (worker + graph nodes) call ``emit`` / ``broadcast``; any
per-client send failure drops that client without affecting the pipeline.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import orjson
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.logging_config import get_logger
from app.models.schemas import WSMessage, WSMessageType

log = get_logger("app.api.ws")

router = APIRouter()


class ConnectionManager:
    """Tracks active WebSocket clients and broadcasts JSON messages."""

    def __init__(self) -> None:
        self._active: set[WebSocket] = set()

    @property
    def count(self) -> int:
        return len(self._active)

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._active.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._active.discard(ws)

    async def broadcast(self, message: dict[str, Any]) -> None:
        data = orjson.dumps(message).decode()
        dead: list[WebSocket] = []
        for ws in list(self._active):
            try:
                await ws.send_text(data)
            except Exception:  # noqa: BLE001 - drop broken clients
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


async def emit(
    type_: WSMessageType,
    event_id: str,
    payload: dict[str, Any] | None = None,
    timings_ms: dict[str, float] | None = None,
) -> None:
    """Broadcast a well-formed ``WSMessage`` to all clients (best-effort)."""
    msg = WSMessage(
        type=type_,
        event_id=event_id,
        ts=datetime.now(UTC).isoformat(),
        payload=payload or {},
        timings_ms=timings_ms or {},
    )
    await manager.broadcast(msg.model_dump())


@router.websocket("/ws/live")
async def ws_live(ws: WebSocket) -> None:
    """Live pipeline feed. Inbound messages are ignored (feed is one-way)."""
    await manager.connect(ws)
    log.info("ws_connected", clients=manager.count)
    try:
        while True:
            await ws.receive_text()  # keep the connection open
    except WebSocketDisconnect:
        manager.disconnect(ws)
        log.info("ws_disconnected", clients=manager.count)
    except Exception:  # noqa: BLE001
        manager.disconnect(ws)
