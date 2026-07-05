"""Golden evaluation set loader.

The golden set is a JSONL file of labeled news items:
    {"id","title","content","author","expected_sentiment",
     "expected_asset","expected_tradable"}
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

GOLDEN_PATH = Path(__file__).resolve().parents[2] / "data" / "golden" / "eval_set.jsonl"


@dataclass(frozen=True)
class GoldenItem:
    id: str
    title: str
    content: str
    author: str | None
    expected_sentiment: str
    expected_asset: str | None
    expected_tradable: bool


def load_golden(path: Path | None = None) -> list[GoldenItem]:
    """Load and validate the golden set."""
    p = path or GOLDEN_PATH
    items: list[GoldenItem] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        raw = json.loads(line)
        items.append(
            GoldenItem(
                id=raw["id"],
                title=raw["title"],
                content=raw.get("content", ""),
                author=raw.get("author"),
                expected_sentiment=raw["expected_sentiment"],
                expected_asset=raw.get("expected_asset"),
                expected_tradable=bool(raw.get("expected_tradable", False)),
            )
        )
    return items
