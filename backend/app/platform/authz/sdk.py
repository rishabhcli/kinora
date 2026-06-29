"""The unified ``check(subject, action, resource, context)`` SDK.

This is the public face of the plane. An :class:`AuthorizationPlane` holds an
ordered list of engines (RBAC, ABAC, the policy DSL, the Zanzibar checker, any
adapters), a :mod:`~app.platform.authz.combining` algorithm, a decision
:mod:`~app.platform.authz.cache`, and a decision-log :mod:`~app.platform.authz.audit`
sink. Its ``check`` method:

1. builds the :class:`AuthorizationRequest`;
2. returns a fresh cached decision if one exists (and records the cache hit);
3. otherwise runs every engine (async-aware), folds the results with the
   combining algorithm, caches, audits, and returns the :class:`Decision`.

``check`` is the rich entry point (returns the full decision with reasons +
obligations); ``is_allowed`` is the boolean convenience; ``require`` raises
:class:`AccessDeniedError` on a deny (for FastAPI-dependency-style enforcement);
``list_objects`` delegates to the relationship engine's reverse index. The plane
also exposes targeted cache invalidation so a tuple/role write evicts exactly the
affected entries.

The plane is deliberately engine-agnostic: it does not know whether an engine is
RBAC, an adapter, or a policy module — it just runs them and combines. That is
what lets the *same* plane fold the scattered legacy checks (via adapters)
alongside the new native engines without special-casing any of them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.platform.authz.audit import DecisionLog, NullDecisionLog
from app.platform.authz.cache import DecisionCache, NullDecisionCache
from app.platform.authz.combining import CombiningAlgorithm, combine
from app.platform.authz.engine import AuthorizationEngine
from app.platform.authz.model import (
    AuthorizationRequest,
    Context,
    Decision,
    Effect,
    EngineResult,
    Resource,
    Subject,
)
from app.platform.authz.rebac import RebacEngine


class AccessDeniedError(Exception):
    """Raised by :meth:`AuthorizationPlane.require` when a check is denied.

    Carries the full :class:`Decision` so the caller (e.g. a FastAPI exception
    handler) can render a typed 403 with the explanation.
    """

    def __init__(self, decision: Decision) -> None:
        super().__init__(f"access denied: {decision.request.action}")
        self.decision = decision


class AuthorizationPlane:
    """The composed authorization fabric — one ``check`` over many engines."""

    def __init__(
        self,
        engines: Sequence[AuthorizationEngine],
        *,
        algorithm: CombiningAlgorithm = CombiningAlgorithm.DENY_OVERRIDES,
        cache: DecisionCache | None = None,
        decision_log: DecisionLog | None = None,
    ) -> None:
        self._engines: tuple[AuthorizationEngine, ...] = tuple(engines)
        self._algorithm = algorithm
        # Explicit ``is None`` (not ``or``): an empty InMemoryDecisionLog is falsy
        # (it defines ``__len__``), so ``or`` would silently swap it for a no-op
        # sink and lose the caller's log.
        self._cache: DecisionCache = cache if cache is not None else NullDecisionCache()
        self._log: DecisionLog = (
            decision_log if decision_log is not None else NullDecisionLog()
        )

    # -- construction --------------------------------------------------------- #

    @property
    def engines(self) -> tuple[AuthorizationEngine, ...]:
        return self._engines

    def with_engine(self, engine: AuthorizationEngine) -> AuthorizationPlane:
        """Return a new plane with ``engine`` appended (same cache/log/algorithm)."""
        return AuthorizationPlane(
            (*self._engines, engine),
            algorithm=self._algorithm,
            cache=self._cache,
            decision_log=self._log,
        )

    # -- the request builders ------------------------------------------------- #

    @staticmethod
    def _coerce_subject(subject: Subject | str) -> Subject:
        if isinstance(subject, Subject):
            return subject
        # A bare string is a user id (the common interactive case).
        return Subject.user(subject)

    @staticmethod
    def _coerce_resource(resource: Resource | tuple[str, str]) -> Resource:
        if isinstance(resource, Resource):
            return resource
        type_, id_ = resource
        return Resource.of(type_, id_)

    @staticmethod
    def _coerce_context(
        context: Context | Mapping[str, Any] | None,
    ) -> Context:
        if context is None:
            return Context.empty()
        if isinstance(context, Context):
            return context
        return Context(attributes=dict(context))

    def build_request(
        self,
        subject: Subject | str,
        action: str,
        resource: Resource | tuple[str, str],
        context: Context | Mapping[str, Any] | None = None,
    ) -> AuthorizationRequest:
        """Assemble an :class:`AuthorizationRequest` from loose arguments."""
        return AuthorizationRequest(
            subject=self._coerce_subject(subject),
            action=action,
            resource=self._coerce_resource(resource),
            context=self._coerce_context(context),
        )

    # -- the core check ------------------------------------------------------- #

    async def check(
        self,
        subject: Subject | str,
        action: str,
        resource: Resource | tuple[str, str],
        context: Context | Mapping[str, Any] | None = None,
    ) -> Decision:
        """Resolve the authorization question into a :class:`Decision`.

        Cache-first; on a miss, runs every engine, folds with the combining
        algorithm, caches the result, and audits it.
        """
        request = self.build_request(subject, action, resource, context)
        cached = self._cache.get(request)
        if cached is not None:
            self._log.record(cached)
            return cached
        results = [await engine.aevaluate(request) for engine in self._engines]
        decision = combine(request, results, algorithm=self._algorithm)
        self._cache.put(decision)
        self._log.record(decision)
        return decision

    def check_sync(
        self,
        subject: Subject | str,
        action: str,
        resource: Resource | tuple[str, str],
        context: Context | Mapping[str, Any] | None = None,
    ) -> Decision:
        """Synchronous check — usable only when every engine is pure.

        Runs each engine's :meth:`evaluate`. Raises ``TypeError`` if an engine
        only supports async (an I/O-backed engine). For an all-pure plane (RBAC +
        ABAC + DSL) this is the fast, infra-free path used by tests + simulation.
        """
        request = self.build_request(subject, action, resource, context)
        cached = self._cache.get(request)
        if cached is not None:
            self._log.record(cached)
            return cached
        results: list[EngineResult] = []
        for engine in self._engines:
            evaluate = getattr(engine, "evaluate", None)
            if evaluate is None:  # pragma: no cover - defensive
                raise TypeError(f"engine {engine.name!r} has no sync evaluate()")
            results.append(evaluate(request))
        decision = combine(request, results, algorithm=self._algorithm)
        self._cache.put(decision)
        self._log.record(decision)
        return decision

    async def is_allowed(
        self,
        subject: Subject | str,
        action: str,
        resource: Resource | tuple[str, str],
        context: Context | Mapping[str, Any] | None = None,
    ) -> bool:
        """Boolean convenience over :meth:`check`."""
        decision = await self.check(subject, action, resource, context)
        return decision.allowed

    async def require(
        self,
        subject: Subject | str,
        action: str,
        resource: Resource | tuple[str, str],
        context: Context | Mapping[str, Any] | None = None,
    ) -> Decision:
        """Like :meth:`check` but raise :class:`AccessDeniedError` on a deny."""
        decision = await self.check(subject, action, resource, context)
        if decision.effect is not Effect.ALLOW:
            raise AccessDeniedError(decision)
        return decision

    # -- reverse index -------------------------------------------------------- #

    def list_objects(self, object_type: str, action: str, subject_id: str) -> frozenset[str]:
        """Object ids of ``object_type`` the subject may take ``action`` on.

        Delegates to the first :class:`RebacEngine` in the plane (the reverse
        index lives in the relationship model). Returns an empty set if the plane
        has no relationship engine.
        """
        for engine in self._engines:
            if isinstance(engine, RebacEngine):
                return engine.list_objects(object_type, action, subject_id)
        return frozenset()

    # -- cache invalidation --------------------------------------------------- #

    def invalidate_subject(self, subject_ref: str) -> int:
        """Evict cached decisions concerning ``subject_ref`` (after a role change)."""
        return self._cache.invalidate_subject(subject_ref)

    def invalidate_resource(self, resource_ref: str) -> int:
        """Evict cached decisions concerning ``resource_ref`` (after a share change)."""
        return self._cache.invalidate_resource(resource_ref)

    @property
    def cache(self) -> DecisionCache:
        return self._cache

    @property
    def decision_log(self) -> DecisionLog:
        return self._log


__all__ = ["AccessDeniedError", "AuthorizationPlane"]
