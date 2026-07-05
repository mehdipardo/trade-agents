"""Structured logging setup.

Configures ``structlog`` to emit JSON log lines. Every log record carries a
timestamp and log level; downstream code is expected to bind an ``event_id``
field on records that belong to a pipeline run.
"""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(*, level: str = "INFO", json_logs: bool = True) -> None:
    """Configure the standard library and ``structlog`` to emit JSON.

    Args:
        level: Root log level name (e.g. ``"INFO"``, ``"DEBUG"``).
        json_logs: When ``True`` render JSON; otherwise a colored console
            renderer is used (handy for local development).
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger."""
    return structlog.get_logger(name)
