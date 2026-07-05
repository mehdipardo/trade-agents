"""Scenario simulator.

Loads canonical demo scenarios from ``data/scenarios/*.json`` and turns them
into ``NewsEvent`` objects (source = ``simulator``). This is the primary driver
of the demo: it lets us replay the full pipeline without any external feed.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.ingestion.normalizer import normalize_payload
from app.models.schemas import NewsEvent

# data/scenarios lives at the repository root: app/ingestion/ -> repo root.
SCENARIOS_DIR = Path(__file__).resolve().parents[2] / "data" / "scenarios"


def list_scenarios() -> list[str]:
    """Return the available scenario names (filenames without ``.json``)."""
    if not SCENARIOS_DIR.is_dir():
        return []
    return sorted(p.stem for p in SCENARIOS_DIR.glob("*.json"))


def load_scenario(name: str) -> NewsEvent:
    """Load a scenario by name and normalize it into a ``NewsEvent``.

    Args:
        name: Scenario name (e.g. ``"trump_btc_bull"``).

    Returns:
        A ``NewsEvent`` with ``source="simulator"`` and a fresh id/received_at.

    Raises:
        FileNotFoundError: If no scenario file matches ``name``.
    """
    path = SCENARIOS_DIR / f"{name}.json"
    if not path.is_file():
        available = ", ".join(list_scenarios()) or "(none)"
        raise FileNotFoundError(
            f"unknown scenario '{name}'. Available scenarios: {available}"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    # The simulator always owns id/received_at; drop any id baked into the file
    # so repeated injections produce distinct events (dedup is tested elsewhere).
    payload.pop("id", None)
    return normalize_payload(payload, source="simulator")
