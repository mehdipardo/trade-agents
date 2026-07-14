"""Closed learning loop: SL post-mortems feed back into the analyst prompt."""

from __future__ import annotations

from app.prompts.analyst import build_lessons_block


def test_lessons_block_empty_when_no_critiques() -> None:
    assert build_lessons_block([]) == ""
    # Records with no usable critique text also collapse to empty.
    assert build_lessons_block([{"symbol": "BTC/USDT", "critique": "  "}]) == ""


def test_lessons_block_renders_bounded_flattened_lines() -> None:
    records = [
        {"symbol": "BTC/USDT", "critique": "Chased a faded CPI spike.\nWait for a pullback."},
        {"symbol": "ETH/USDT", "critique": "Leverage on a low-confidence rumor."},
    ]
    block = build_lessons_block(records)
    assert "LESSONS FROM YOUR OWN PAST LOSING TRADES" in block
    assert "[BTC/USDT]" in block and "[ETH/USDT]" in block
    # Newlines in the model-authored critique are flattened to keep one line each.
    assert "\n- [BTC/USDT] Chased a faded CPI spike. Wait for a pullback." in block


def test_lessons_block_caps_count_and_length() -> None:
    records = [{"symbol": "X", "critique": "y" * 400} for _ in range(20)]
    block = build_lessons_block(records)
    # At most 8 lessons, each truncated to 240 chars of critique text.
    assert block.count("\n- [X]") == 8
    assert "y" * 240 in block and "y" * 241 not in block


async def test_recent_lessons_reads_from_store() -> None:
    from app.services.llm import _recent_lessons
    from app.services.store import InMemoryStore, set_store

    set_store(InMemoryStore())
    from app.services.store import get_store

    await get_store().record_critique(
        {"symbol": "BTC/USDT", "reason": "stop_loss", "critique": "Entered too late."}
    )
    block = await _recent_lessons()
    assert "Entered too late." in block
    assert "[BTC/USDT]" in block


async def test_recent_lessons_survives_store_error(monkeypatch) -> None:
    import app.services.llm as llm

    class _BoomStore:
        async def critiques(self, limit: int):  # noqa: ANN001
            raise RuntimeError("redis down")

    monkeypatch.setattr("app.services.store.get_store", lambda: _BoomStore())
    # Enrichment must never break a live decision -> empty block, no raise.
    assert await llm._recent_lessons() == ""


async def test_analyst_prompt_includes_lessons(monkeypatch) -> None:
    """The system prompt sent to the LLM carries the lessons block."""
    from app.config import get_settings
    from app.models.schemas import NewsEvent
    from app.services import llm
    from app.services.store import InMemoryStore, set_store

    set_store(InMemoryStore())
    from app.services.store import get_store

    await get_store().record_critique(
        {"symbol": "OIL/USDT", "critique": "Faded geopolitical spike; size down."}
    )

    captured: dict = {}

    class _FakeLLM:
        async def ainvoke(self, messages, config=None):  # noqa: ANN001
            captured["system"] = messages[0].content
            from app.models.schemas import Signal

            return {
                "parsed": Signal(
                    sentiment="NEUTRAL", intensity=1, confidence=0.0,
                    actionability=1, asset=None, impact_score=1,
                    event_type="other", rationale="n/a",
                ),
                "raw": None,
            }

    from datetime import UTC, datetime

    event = NewsEvent(
        id="e1", source="economic", title="US CPI actual 3.1%",
        content="inflation cooling", author="bls", received_at=datetime.now(UTC),
    )
    await llm.analyze(event, get_settings(), llm=_FakeLLM())
    assert "LESSONS FROM YOUR OWN PAST LOSING TRADES" in captured["system"]
    assert "Faded geopolitical spike" in captured["system"]
