"""Groq LLM usage tracker in the store."""

from __future__ import annotations

from app.services.store import InMemoryStore, get_store, set_store


async def test_llm_usage_starts_empty_and_bumps() -> None:
    set_store(InMemoryStore())
    s = get_store()
    assert (await s.llm_usage())["calls_total"] == 0
    await s.bump_llm(300, 50)
    await s.bump_llm(200, 40)
    u = await s.llm_usage()
    assert u["calls_total"] == 2
    assert u["calls_today"] == 2
    assert u["prompt_tokens"] == 500
    assert u["completion_tokens"] == 90
    assert u["total_tokens"] == 590
