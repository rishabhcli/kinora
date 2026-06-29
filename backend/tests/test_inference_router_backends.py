"""Tests for app.inference.router.backends + factory — transport adapters.

EchoBackend is the network-free default; ChatProviderBackend bridges to a
fake ``ChatProvider`` (no live call) and proves per-request kwargs passthrough,
token accounting, and per-request error isolation (one failure doesn't sink the
batch). The factory builds a wired, off-gate router with the Echo backend.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.inference.router.backends import ChatProviderBackend, EchoBackend
from app.inference.router.errors import BackendError
from app.inference.router.factory import build_multi_model_router, build_router
from app.inference.router.request import InferenceRequest
from app.inference.router.worker import WorkerConfig


def _req(rid: str, **md: Any) -> InferenceRequest:
    return InferenceRequest(
        request_id=rid, model="m", prompt_tokens=100, max_output_tokens=32, metadata=md
    )


# -- EchoBackend ---------------------------------------------------------- #


async def test_echo_backend_caps_output_at_request_max() -> None:
    backend = EchoBackend("m", output_tokens=1000)
    results = await backend.execute_batch([_req("a")])
    assert results[0].output_tokens == 32  # capped by request max_output_tokens
    assert results[0].prompt_tokens == 100
    assert results[0].ok


async def test_echo_backend_batch() -> None:
    backend = EchoBackend("m", output_tokens=8)
    results = await backend.execute_batch([_req("a"), _req("b"), _req("c")])
    assert [r.request_id for r in results] == ["a", "b", "c"]
    assert all(r.output_tokens == 8 for r in results)


def test_echo_backend_requires_model() -> None:
    with pytest.raises(BackendError):
        EchoBackend("")


# -- ChatProviderBackend -------------------------------------------------- #


class _FakeChatResult:
    def __init__(self, output_tokens: int, input_tokens: int) -> None:
        self.output_tokens = output_tokens
        self.input_tokens = input_tokens


class _FakeChatProvider:
    """A fake ChatProvider: records calls, never touches the network."""

    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.calls: list[tuple[list[Any], str, dict[str, Any]]] = []
        self._fail_on = fail_on or set()

    async def chat(self, messages: list[Any], model: str, **kwargs: Any) -> _FakeChatResult:
        self.calls.append((messages, model, kwargs))
        marker = messages[0]["content"] if messages else ""
        if marker in self._fail_on:
            raise RuntimeError(f"provider boom for {marker}")
        return _FakeChatResult(output_tokens=len(messages) * 5, input_tokens=42)


async def test_chat_backend_passes_messages_and_kwargs() -> None:
    provider = _FakeChatProvider()
    backend = ChatProviderBackend(provider, "qwen-max")  # type: ignore[arg-type]
    req = _req("a", messages=[{"role": "user", "content": "hi"}], temperature=0.2, max_tokens=128)
    results = await backend.execute_batch([req])
    assert results[0].ok
    assert results[0].output_tokens == 5
    assert results[0].prompt_tokens == 42
    # kwargs forwarded, model forced.
    _msgs, model, kwargs = provider.calls[0]
    assert model == "qwen-max"
    assert kwargs == {"temperature": 0.2, "max_tokens": 128}


async def test_chat_backend_missing_messages_is_per_request_error() -> None:
    provider = _FakeChatProvider()
    backend = ChatProviderBackend(provider, "qwen-max")  # type: ignore[arg-type]
    results = await backend.execute_batch([_req("a")])  # no messages in metadata
    assert not results[0].ok
    assert "messages" in (results[0].error or "")


async def test_chat_backend_isolates_per_request_failure() -> None:
    provider = _FakeChatProvider(fail_on={"bad"})
    backend = ChatProviderBackend(provider, "qwen-max")  # type: ignore[arg-type]
    good = _req("good", messages=[{"role": "user", "content": "ok"}])
    bad = _req("bad", messages=[{"role": "user", "content": "bad"}])
    results = await backend.execute_batch([good, bad])
    by_id = {r.request_id: r for r in results}
    assert by_id["good"].ok
    assert not by_id["bad"].ok and "boom" in (by_id["bad"].error or "")


# -- factory -------------------------------------------------------------- #


async def test_build_router_defaults_to_echo_and_runs_off_gate() -> None:
    router = build_router("m", n_workers=2, worker=WorkerConfig(token_capacity=10_000, max_slots=4))
    fut = await router.submit(_req("a", messages=[{"role": "user", "content": "x"}]))
    await router.run_until_idle()
    res = await fut
    assert res.ok  # Echo backend, no network, no credit spent


def test_build_router_creates_n_workers() -> None:
    router = build_router("m", n_workers=3)
    assert len(router._pool.workers()) == 3  # noqa: SLF001 - test introspection


async def test_build_multi_model_router() -> None:
    multi = build_multi_model_router({"qwen-max": 1, "qwen-vl": 2})
    assert set(multi.models) == {"qwen-max", "qwen-vl"}
    assert len(multi.router_for("qwen-vl")._pool.workers()) == 2  # noqa: SLF001
