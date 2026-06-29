"""Typed client stubs — the "codegen" that turns a contract into a callable.

A :class:`ServiceStub` is the *typed* front door to :class:`RpcClient.call`: given
a :class:`ServiceContract` it exposes one bound coroutine per method that takes
the contract's typed request, encodes it, calls through the resilient client, and
decodes the typed response — raising the reconstructed :class:`RpcError` on
failure (the ergonomic raise-on-error path, as opposed to the client's
result-or-error :class:`RpcResponse`).

We deliberately favour a *runtime* stub over generated ``.py`` files: the contract
*is* the schema, and generating Python from it would just be a less-flexible copy
that drifts. :func:`generate_stub_source` still emits a ``.pyi``-style stub for
editors / docs (so a developer sees the typed signatures), but nothing in the
runtime depends on a build step — defining a contract is enough to call it. That
keeps the layer additive and zero-config.

Usage::

    stub = ServiceStub(cinematographer_contract, client)
    spec = await stub.plan_shot(PlanShotReq(shot_hash="…"), context=ctx)

The dynamic ``__getattr__`` returns a bound method for any contract method, so the
stub reads exactly like calling the service object directly — which is the whole
point: the call site is identical whether the service is in-process or remote.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.distributed.rpc.client import CallOptions, RpcClient
from app.distributed.rpc.context import RequestContext
from app.distributed.rpc.contracts import MethodSpec, ServiceContract


class ServiceStub:
    """A typed, resilient client stub generated at runtime from a contract.

    Each contract method becomes an ``async`` attribute:
    ``await stub.<method>(request, *, context, options=None)``. The request is the
    contract's typed request type; the return value is the typed response type. A
    failure raises the reconstructed :class:`RpcError`.
    """

    def __init__(self, contract: ServiceContract, client: RpcClient) -> None:
        self._contract = contract
        self._client = client
        # Register the contract's method specs with the client so its resilience
        # policies see the per-method ``idempotent`` flag without a lookup at the
        # call site.
        self._specs: dict[str, MethodSpec] = dict(contract.methods)

    @property
    def contract(self) -> ServiceContract:
        """The contract this stub fronts."""
        return self._contract

    def _default_context(self) -> RequestContext:
        """A fresh root context off the client's clock + default timeout.

        Used when a caller omits ``context`` — the ergonomic edge-call path. A
        caller that needs propagation (the common case for an in-chain call)
        passes its own context so the trace/deadline carry through.
        """
        return RequestContext.root(
            clock=self._client.clock,
            timeout_s=self._client.default_timeout_s,
        )

    async def invoke(
        self,
        method: str,
        request: Any = None,
        *,
        context: RequestContext | None = None,
        options: CallOptions | None = None,
    ) -> Any:
        """Call ``method`` with a typed request; return the typed response.

        The explicit form behind the dynamic attribute access. Encodes the
        request per the contract, dispatches through the resilient client, and
        decodes (or raises) the result. A missing ``context`` starts a fresh
        root context (an originating edge call).
        """
        spec = self._contract.method(method)
        payload = spec.encode_request(request)
        response = await self._client.call(
            self._contract.name,
            method,
            payload,
            context=context or self._default_context(),
            method_spec=spec,
            options=options,
        )
        body = response.raise_for_status(service=self._contract.name, method=method)
        return spec.decode_response(body)

    def __getattr__(self, name: str) -> Callable[..., Awaitable[Any]]:
        """Return a bound coroutine for a contract method (the ergonomic form)."""
        # ``__getattr__`` only fires for misses, so real attributes are unaffected.
        contract = object.__getattribute__(self, "_contract")
        if not contract.has_method(name):
            raise AttributeError(
                f"{contract.name!r} stub has no method {name!r}; "
                f"known: {sorted(contract.methods)}"
            )

        async def _bound(
            request: Any = None,
            *,
            context: RequestContext | None = None,
            options: CallOptions | None = None,
        ) -> Any:
            return await self.invoke(name, request, context=context, options=options)

        _bound.__name__ = name
        _bound.__qualname__ = f"{contract.name}Stub.{name}"
        return _bound


def generate_stub_source(contract: ServiceContract, *, class_name: str | None = None) -> str:
    """Emit a ``.pyi``-style typed stub for a contract (editor/docs aid only).

    Nothing at runtime needs this — :class:`ServiceStub` is fully dynamic — but a
    generated stub gives editors real signatures and documents the wire surface.
    Returns Python source as a string; callers write it to ``<service>_stub.pyi``
    if they want IDE completion.
    """
    cls = class_name or f"{_pascal(contract.name)}Stub"
    lines: list[str] = [
        '"""Auto-generated typed stub. Do not edit — regenerate from the contract."""',
        "from __future__ import annotations",
        "from typing import Any",
        "from app.distributed.rpc.client import CallOptions",
        "from app.distributed.rpc.context import RequestContext",
        "",
        f"class {cls}:",
        f'    """Typed stub for the {contract.name!r} service (v{contract.version})."""',
    ]
    if not contract.methods:
        lines.append("    ...")
    for mname in sorted(contract.methods):
        spec = contract.methods[mname]
        req_t = _type_ref(spec.request_type)
        resp_t = _type_ref(spec.response_type)
        lines.append(
            f"    async def {mname}(self, request: {req_t}, *, "
            f"context: RequestContext | None = None, "
            f"options: CallOptions | None = None) -> {resp_t}: ..."
        )
    return "\n".join(lines) + "\n"


def _pascal(name: str) -> str:
    return "".join(part.capitalize() for part in name.replace("-", "_").split("_"))


def _type_ref(tp: Any) -> str:
    if tp is None:
        return "None"
    return getattr(tp, "__name__", "Any")


__all__ = ["ServiceStub", "generate_stub_source"]
