"""Gateway-wrapped provider facades — compose round-1 providers, don't edit them.

The gateway's :meth:`~app.providers.resilience.gateway.ResilientGateway.execute`
takes a bare ``attempt`` thunk. These facades turn a round-1 provider call into one
such thunk and run it through the gateway, so a caller gets per-model breakers,
adaptive rate-limiting, full-jitter retries, response caching, and (for chat)
opt-in hedging — *without* the round-1 ``ChatProvider`` / ``ImageProvider`` knowing
the gateway exists. They are pure composition (wrap), the rule for round-1 files.

Two facades are provided:

* :class:`GatewayChatProvider` — wraps a ``ChatProvider``; chat is idempotent and
  cacheable, so it benefits most. ``chat`` is hedge-eligible; ``chat_json`` is
  cacheable (a re-asked identical structured prompt is dedup'd).
* :class:`GatewayCallable` — a thin generic wrapper for *any* async provider method,
  letting callers route an arbitrary call (image gen, VL analyze, …) through the
  gateway with an explicit :class:`~app.providers.resilience.gateway.GatewayCall`
  policy. Video is intentionally never marked idempotent here.

These are opt-in helpers; nothing constructs them unless a caller chooses to.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from ..chat import ChatProvider, Messages
from ..types import ChatResult
from .gateway import GatewayCall, ResilientGateway

R = TypeVar("R")


class GatewayChatProvider:
    """A drop-in-ish wrapper that routes a :class:`ChatProvider` through the gateway.

    The method surface mirrors the bits of ``ChatProvider`` the crew uses, but each
    call is wrapped in a :meth:`ResilientGateway.execute`. ``chat`` is marked
    idempotent (hedge-eligible for tail latency); ``chat_json`` is cacheable so an
    identical re-asked structured prompt is served from cache at zero token spend.
    """

    def __init__(self, inner: ChatProvider, gateway: ResilientGateway) -> None:
        self._inner = inner
        self._gateway = gateway

    async def chat(
        self,
        messages: Messages,
        model: str,
        *,
        hedge: bool = True,
        cacheable: bool = False,
        **kwargs: Any,
    ) -> ChatResult:
        """Run a chat completion through the gateway (idempotent; hedge-eligible)."""
        payload = {"messages": messages, "model": model, "kwargs": _safe(kwargs)}
        call = GatewayCall(
            model=model,
            op="chat",
            idempotent=hedge,
            cacheable=cacheable,
            cache_payload=payload if cacheable else None,
        )

        async def attempt() -> ChatResult:
            return await self._inner.chat(messages, model, **kwargs)

        return await self._gateway.execute(call, attempt)

    async def chat_json(
        self,
        messages: Messages,
        model: str,
        *,
        cacheable: bool = True,
        **kwargs: Any,
    ) -> Any:
        """Run a structured (JSON) completion through the gateway (cacheable)."""
        payload = {"messages": messages, "model": model, "kwargs": _safe(kwargs)}
        call = GatewayCall(
            model=model,
            op="chat_json",
            idempotent=False,  # structured streaming isn't hedge-safe by default
            cacheable=cacheable,
            cache_payload=payload if cacheable else None,
        )

        async def attempt() -> Any:
            return await self._inner.chat_json(messages, model, **kwargs)

        return await self._gateway.execute(call, attempt)


class GatewayCallable:
    """Wrap *any* async provider method as a gateway-routed callable.

    ``policy`` supplies the per-call :class:`GatewayCall` (model/op/idempotency/
    cache). Use it to route image-gen, VL analyze, embeddings, etc., through the
    gateway while keeping the round-1 provider untouched. **Never** mark a video
    render idempotent (it would risk a double-spend of scarce video-seconds).
    """

    def __init__(self, gateway: ResilientGateway) -> None:
        self._gateway = gateway

    async def run(
        self,
        policy: GatewayCall,
        func: Callable[..., Awaitable[R]],
        *args: Any,
        **kwargs: Any,
    ) -> R:
        async def attempt() -> R:
            return await func(*args, **kwargs)

        return await self._gateway.execute(policy, attempt)


def _safe(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Strip non-cache-stable kwargs (e.g. callables/timeouts) from the cache key.

    Only values that affect the *content* of the result should key the cache;
    transport knobs like ``timeout``/``stream`` do not change the answer, so they
    are dropped to keep cache identity tight.
    """
    drop = {"timeout", "stream"}
    return {k: v for k, v in kwargs.items() if k not in drop}


__all__ = [
    "GatewayCallable",
    "GatewayChatProvider",
]
