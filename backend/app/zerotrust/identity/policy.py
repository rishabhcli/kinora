"""The authorization-policy seam — *which workload may call which*.

Identity answers *"who are you"*; policy answers *"may you, identity X, call
identity Y for action Z"*. This module is a small, **default-deny**, explainable
authorization engine over SPIFFE IDs:

* a :class:`PolicyRule` allows a set of *callers* to perform a set of *actions*
  against a set of *targets*. Callers/targets are matched by :class:`Matcher`s —
  exact SPIFFE ID, path-prefix (``/agents/*`` authorizes ``/agents/critic``),
  whole-trust-domain, or "any". Actions match exactly or via ``*``.
* :class:`AuthorizationPolicy` evaluates a :class:`CallRequest` against its rules
  and returns a :class:`Decision` that carries *why* (the matched rule, or the
  default-deny reason) — so an mTLS gate can both enforce and log a clear cause.
* an optional **conditions** predicate on a rule lets policy depend on request
  attributes (a claim, a time window) without leaving this pure seam.

The engine is deny-by-default: with no matching allow rule, the decision is DENY.
There are also explicit deny rules, evaluated first, so a broad allow can be
carved with a narrow deny (deny-overrides). This is the gate sibling facets call
before honouring a verified peer.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from app.zerotrust.identity.errors import AuthorizationError
from app.zerotrust.identity.spiffe import SpiffeId, TrustDomain


class Effect(enum.StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class Matcher:
    """Base class for SPIFFE-ID matchers (see the concrete subclasses)."""

    def matches(self, sid: SpiffeId) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def describe(self) -> str:  # pragma: no cover - abstract
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class AnyWorkload(Matcher):
    """Matches every identity (use sparingly — it is the wildcard)."""

    def matches(self, sid: SpiffeId) -> bool:  # noqa: ARG002
        return True

    def describe(self) -> str:
        return "*"


@dataclass(frozen=True, slots=True)
class ExactId(Matcher):
    """Matches one specific SPIFFE ID."""

    spiffe_id: SpiffeId

    def matches(self, sid: SpiffeId) -> bool:
        return sid == self.spiffe_id

    def describe(self) -> str:
        return self.spiffe_id.uri


@dataclass(frozen=True, slots=True)
class PathPrefix(Matcher):
    """Matches any identity at-or-below a path prefix in a trust domain."""

    prefix: SpiffeId

    def matches(self, sid: SpiffeId) -> bool:
        return sid.is_under(self.prefix)

    def describe(self) -> str:
        return f"{self.prefix.uri}/*"


@dataclass(frozen=True, slots=True)
class DomainMember(Matcher):
    """Matches any identity in a trust domain."""

    domain: TrustDomain

    def matches(self, sid: SpiffeId) -> bool:
        return sid.member_of(self.domain)

    def describe(self) -> str:
        return f"spiffe://{self.domain.name}/**"


def matcher_for(spec: str) -> Matcher:
    """Parse a matcher spec string into a :class:`Matcher`.

    * ``"*"`` → :class:`AnyWorkload`
    * ``"spiffe://d/a/*"`` → :class:`PathPrefix`
    * ``"spiffe://d/**"`` or ``"spiffe://d"`` → :class:`DomainMember`
    * ``"spiffe://d/a/b"`` → :class:`ExactId`
    """

    if spec == "*":
        return AnyWorkload()
    if spec.endswith("/**"):
        return DomainMember(SpiffeId.parse(spec[: -len("/**")]).domain)
    if spec.endswith("/*"):
        return PathPrefix(SpiffeId.parse(spec[: -len("/*")]))
    parsed = SpiffeId.parse(spec)
    if parsed.is_trust_domain:
        return DomainMember(parsed.domain)
    return ExactId(parsed)


@dataclass(frozen=True, slots=True)
class CallRequest:
    """An attempted call: a caller identity invoking an action on a target."""

    caller: SpiffeId
    target: SpiffeId
    action: str = "call"
    attributes: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PolicyRule:
    """An allow/deny rule over (callers, targets, actions) with conditions."""

    name: str
    callers: tuple[Matcher, ...]
    targets: tuple[Matcher, ...]
    actions: frozenset[str] = frozenset({"*"})
    effect: Effect = Effect.ALLOW
    condition: Callable[[CallRequest], bool] | None = None

    def matches(self, req: CallRequest) -> bool:
        if not any(m.matches(req.caller) for m in self.callers):
            return False
        if not any(m.matches(req.target) for m in self.targets):
            return False
        if "*" not in self.actions and req.action not in self.actions:
            return False
        return not (self.condition is not None and not self.condition(req))

    @classmethod
    def allow(
        cls,
        name: str,
        *,
        callers: Iterable[str],
        targets: Iterable[str],
        actions: Iterable[str] = ("*",),
        condition: Callable[[CallRequest], bool] | None = None,
    ) -> PolicyRule:
        return cls(
            name=name,
            callers=tuple(matcher_for(c) for c in callers),
            targets=tuple(matcher_for(t) for t in targets),
            actions=frozenset(actions),
            effect=Effect.ALLOW,
            condition=condition,
        )

    @classmethod
    def deny(
        cls,
        name: str,
        *,
        callers: Iterable[str],
        targets: Iterable[str],
        actions: Iterable[str] = ("*",),
        condition: Callable[[CallRequest], bool] | None = None,
    ) -> PolicyRule:
        return cls(
            name=name,
            callers=tuple(matcher_for(c) for c in callers),
            targets=tuple(matcher_for(t) for t in targets),
            actions=frozenset(actions),
            effect=Effect.DENY,
            condition=condition,
        )


@dataclass(frozen=True, slots=True)
class Decision:
    """The result of evaluating a :class:`CallRequest` (carries the reason)."""

    allowed: bool
    reason: str
    matched_rule: str | None = None

    def require(self) -> None:
        """Raise :class:`AuthorizationError` if the decision is DENY."""

        if not self.allowed:
            raise AuthorizationError(self.reason)


@dataclass(slots=True)
class AuthorizationPolicy:
    """A default-deny, deny-overrides authorization engine over SPIFFE IDs."""

    _rules: list[PolicyRule] = field(default_factory=list)

    def add(self, rule: PolicyRule) -> AuthorizationPolicy:
        self._rules.append(rule)
        return self

    def extend(self, rules: Iterable[PolicyRule]) -> AuthorizationPolicy:
        for r in rules:
            self.add(r)
        return self

    def rules(self) -> tuple[PolicyRule, ...]:
        return tuple(self._rules)

    def evaluate(self, req: CallRequest) -> Decision:
        """Evaluate *req*: explicit deny wins, else an allow, else default-deny."""

        matched_allow: PolicyRule | None = None
        for rule in self._rules:
            if not rule.matches(req):
                continue
            if rule.effect is Effect.DENY:
                return Decision(
                    allowed=False,
                    reason=(
                        f"denied by rule {rule.name!r}: "
                        f"{req.caller.uri} -> {req.target.uri} [{req.action}]"
                    ),
                    matched_rule=rule.name,
                )
            if matched_allow is None:
                matched_allow = rule
        if matched_allow is not None:
            return Decision(
                allowed=True,
                reason=f"allowed by rule {matched_allow.name!r}",
                matched_rule=matched_allow.name,
            )
        return Decision(
            allowed=False,
            reason=(
                f"default-deny: no rule permits {req.caller.uri} -> "
                f"{req.target.uri} [{req.action}]"
            ),
        )

    def is_allowed(self, req: CallRequest) -> bool:
        return self.evaluate(req).allowed

    def authorize(self, req: CallRequest) -> None:
        """Evaluate and raise :class:`AuthorizationError` on DENY."""

        self.evaluate(req).require()

    def authorizer_for(self, target: SpiffeId, action: str = "call") -> Callable[[SpiffeId], bool]:
        """A ``caller -> bool`` predicate for the mTLS verifier's ``authorize`` arg.

        Bridges policy into :func:`mtls.simulate_handshake` /
        :meth:`SvidVerifier.verify_peer` so a verified peer is also policy-checked
        in one pass.
        """

        def _predicate(caller: SpiffeId) -> bool:
            return self.is_allowed(CallRequest(caller=caller, target=target, action=action))

        return _predicate


__all__ = [
    "AnyWorkload",
    "AuthorizationPolicy",
    "CallRequest",
    "Decision",
    "DomainMember",
    "Effect",
    "ExactId",
    "Matcher",
    "PathPrefix",
    "PolicyRule",
    "matcher_for",
]
