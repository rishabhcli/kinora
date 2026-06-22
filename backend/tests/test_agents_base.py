"""Unit tests for ``BaseAgent``: JSON validation, the repair round-trip, the
multimodal path, and the optional Qwen tool-calling loop. No network."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from pydantic import BaseModel, ValidationError

from app.agents.base import BaseAgent
from app.agents.prompts import VersionedPrompt
from app.providers import ChatResult, Providers, ToolCall
from tests.test_agents_support import FakeSkills, JsonSequencer, make_providers

PROMPT = VersionedPrompt(version="test@v1", system="be a test agent")


# A locally-defined fixture (not imported) avoids the import-shadowing lint that
# the module-level _agent(providers=...) helper would otherwise trigger.
@pytest_asyncio.fixture
async def providers() -> AsyncIterator[Providers]:
    aggregate = make_providers()
    try:
        yield aggregate
    finally:
        await aggregate.aclose()


class Out(BaseModel):
    x: int


def _agent(prov: Providers, *, skills: FakeSkills | None = None) -> BaseAgent:
    return BaseAgent(prov, name="t", model="m", prompt=PROMPT, skills=skills)  # type: ignore[arg-type]


async def test_run_json_parses_valid_first_try(providers: Providers) -> None:  # noqa: F811
    seq = JsonSequencer({"x": 1})
    providers.chat.chat_json = seq  # type: ignore[method-assign]
    out = await _agent(providers).run_json({"hello": "world"}, Out)
    assert out == Out(x=1)
    assert seq.calls == 1  # no repair needed


async def test_run_json_repairs_once_then_succeeds(providers: Providers) -> None:  # noqa: F811
    # First reply is valid JSON but fails the schema (missing x); the repair fixes it.
    seq = JsonSequencer({"y": 99}, {"x": 7})
    providers.chat.chat_json = seq  # type: ignore[method-assign]
    out = await _agent(providers).run_json({}, Out)
    assert out == Out(x=7)
    assert seq.calls == 2  # original + exactly one repair round-trip


async def test_run_json_propagates_second_validation_failure(providers: Providers) -> None:  # noqa: F811
    seq = JsonSequencer({"y": 1}, {"z": 2})  # both invalid
    providers.chat.chat_json = seq  # type: ignore[method-assign]
    with pytest.raises(ValidationError):  # the failed repair propagates
        await _agent(providers).run_json({}, Out)
    assert seq.calls == 2


async def test_run_json_vl_validates_multimodal_reply(providers: Providers) -> None:  # noqa: F811
    seq = JsonSequencer({"x": 3})
    providers.vl.analyze_json = seq  # type: ignore[method-assign]
    out = await _agent(providers).run_json_vl([b"\x89PNG"], {"q": "?"}, Out)
    assert out == Out(x=3)
    assert seq.calls == 1


async def test_run_tool_loop_dispatches_then_parses(providers: Providers) -> None:  # noqa: F811
    tool_turn = ChatResult(
        text="",
        model="m",
        tool_calls=[ToolCall(id="c1", name="canon_query", arguments='{"beat_id":"b"}')],
    )
    final = ChatResult(text='{"x": 42}', model="m", tool_calls=[])
    providers.chat.chat = JsonSequencer(tool_turn, final)  # type: ignore[method-assign]
    skills = FakeSkills({"slice": "ok"})
    agent = _agent(providers, skills=skills)

    out = await agent.run_tool_loop({"go": True}, Out, tools=[{"type": "function"}])

    assert out == Out(x=42)
    assert skills.calls == [("canon_query", '{"beat_id":"b"}')]


async def test_run_tool_loop_requires_skills(providers: Providers) -> None:  # noqa: F811
    with pytest.raises(ValueError, match="QwenSkillDispatcher"):
        await _agent(providers).run_tool_loop({}, Out, tools=[{"type": "function"}])
