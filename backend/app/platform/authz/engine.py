"""The engine protocol every authorization model implements.

The unified plane is a *composition* of independent decision engines: an RBAC
engine, an ABAC engine, the Rego-style policy DSL, the Zanzibar relationship
checker, and any number of adapter engines that wrap an existing subsystem's
check. Each implements the tiny :class:`AuthorizationEngine` protocol — given an
:class:`AuthorizationRequest`, return an :class:`EngineResult` (its effect +
reasons + obligations). The SDK runs the registered engines and folds their
opinions with a :mod:`~app.platform.authz.combining` algorithm.

Engines come in two flavours:

* **pure / synchronous** (RBAC, ABAC, DSL evaluation over an in-document fact
  base) — these implement :meth:`evaluate` and are exhaustively unit-testable
  with no infrastructure;
* **async / I/O-backed** (the Zanzibar checker reading the tuple store, an
  adapter calling a DB-backed service) — these implement :meth:`aevaluate`.

The default :meth:`aevaluate` simply awaits the sync path, so a pure engine
only needs to implement :meth:`evaluate` and is still usable from the async SDK.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.platform.authz.model import AuthorizationRequest, EngineResult


@runtime_checkable
class AuthorizationEngine(Protocol):
    """A single decision engine in the plane.

    ``name`` identifies the engine in the reason trail and in coverage reports.
    An engine MUST be side-effect free with respect to the authorization
    *decision* (it may read state, but must not mutate the thing being authorized).
    """

    name: str

    def evaluate(self, request: AuthorizationRequest) -> EngineResult:
        """Synchronously decide ``request`` (pure / in-memory engines)."""
        ...

    async def aevaluate(self, request: AuthorizationRequest) -> EngineResult:
        """Asynchronously decide ``request`` (I/O-backed engines)."""
        ...


class SyncEngine:
    """Base for pure engines: implement :meth:`evaluate`; async is derived.

    Subclasses set :attr:`name` and override :meth:`evaluate`. The async path
    awaits the sync result, so the engine plugs into the async SDK unchanged.
    """

    name: str = "sync"

    def evaluate(self, request: AuthorizationRequest) -> EngineResult:  # pragma: no cover
        raise NotImplementedError

    async def aevaluate(self, request: AuthorizationRequest) -> EngineResult:
        return self.evaluate(request)


__all__ = ["AuthorizationEngine", "SyncEngine"]
