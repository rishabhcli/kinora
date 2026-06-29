"""The ABAC engine — attribute conditions over the request document.

Attribute-based access control answers the questions roles can't: "the *owner*
of a resource may always edit it", "a tenant member may read only resources in
*their* tenant", "rendering needs *fresh MFA*". These are predicates over the
subject/resource/context attributes, not over role membership.

The engine evaluates an ordered list of :class:`AbacRule`\\s. Each rule binds an
**action matcher**, a **condition** (a predicate over the request document), and
an **effect** (ALLOW or DENY) with optional obligations. The first rule whose
action matcher and condition both hold decides; if none match, the engine
abstains (letting another engine speak). Conditions are built from a small,
composable, side-effect-free predicate algebra (:class:`Condition` subclasses)
so every branch is exhaustively unit-testable and so the same predicate tree can
back the policy DSL.

The attribute reference syntax (``subject.tenant``, ``resource.owner``,
``context.mfa``) is shared with the DSL: :func:`resolve_attr` is the single
resolver both use, so a condition written in the DSL and one built here read the
same document the same way.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from app.platform.authz.engine import SyncEngine
from app.platform.authz.model import (
    AuthorizationRequest,
    EngineResult,
    Obligation,
    Reason,
)

# --------------------------------------------------------------------------- #
# Attribute resolution — the shared document accessor
# --------------------------------------------------------------------------- #

#: The three roots an attribute path may address.
_ROOTS = frozenset({"subject", "resource", "context", "action"})


def resolve_attr(request: AuthorizationRequest, path: str) -> Any:
    """Resolve a dotted attribute path against the request document.

    Paths address one of four roots: ``subject.*``, ``resource.*``,
    ``context.*``, or the bare ``action``. ``subject.id`` / ``subject.type`` /
    ``resource.id`` / ``resource.type`` read the structural fields; any other
    leaf reads the corresponding ``attributes`` bag. Returns ``None`` for an
    unknown path (so a missing attribute is falsy rather than an error).
    """
    if path == "action":
        return request.action
    head, _, tail = path.partition(".")
    if head not in _ROOTS or not tail:
        return None
    if head == "subject":
        return _read(request.subject.type, request.subject.id, request.subject.attributes, tail)
    if head == "resource":
        return _read(
            request.resource.type, request.resource.id, request.resource.attributes, tail
        )
    # context has no structural id/type
    return request.context.attributes.get(tail)


def _read(type_: str, id_: str, attributes: Any, leaf: str) -> Any:
    if leaf == "id":
        return id_
    if leaf == "type":
        return type_
    return attributes.get(leaf)


# --------------------------------------------------------------------------- #
# The condition algebra (shared with the DSL)
# --------------------------------------------------------------------------- #


class Condition(ABC):
    """A side-effect-free predicate over an authorization request."""

    @abstractmethod
    def holds(self, request: AuthorizationRequest) -> bool:
        """Whether the predicate is satisfied for ``request``."""

    def describe(self) -> str:
        """A short human rendering of the predicate (for reasons / coverage)."""
        return self.__class__.__name__

    # Boolean combinators (operator sugar reads naturally in tests/policies).
    def __and__(self, other: Condition) -> Condition:
        return AllOf([self, other])

    def __or__(self, other: Condition) -> Condition:
        return AnyOf([self, other])

    def __invert__(self) -> Condition:
        return Not(self)


@dataclass(frozen=True)
class Always(Condition):
    """The constant-true predicate (an unconditional rule)."""

    def holds(self, request: AuthorizationRequest) -> bool:
        return True

    def describe(self) -> str:
        return "true"


@dataclass(frozen=True)
class Attr(Condition):
    """``<path> <op> <value>`` — compare an attribute to a literal.

    ``op`` is one of ``eq``, ``ne``, ``in``, ``contains``, ``gt``, ``ge``,
    ``lt``, ``le``. A missing attribute resolves to ``None`` and only satisfies
    an ``ne`` against a non-None literal (otherwise the comparison is False).
    """

    path: str
    op: str
    value: Any = None

    def holds(self, request: AuthorizationRequest) -> bool:
        left = resolve_attr(request, self.path)
        return _compare(left, self.op, self.value)

    def describe(self) -> str:
        return f"{self.path} {self.op} {self.value!r}"


@dataclass(frozen=True)
class AttrEqAttr(Condition):
    """``<left path> == <right path>`` — compare two attributes to each other.

    The workhorse of ownership/tenancy ABAC: ``subject.id == resource.owner`` or
    ``subject.tenant == resource.tenant``. A relational comparison can also be
    requested via ``op``.
    """

    left: str
    right: str
    op: str = "eq"

    def holds(self, request: AuthorizationRequest) -> bool:
        left = resolve_attr(request, self.left)
        right = resolve_attr(request, self.right)
        return _compare(left, self.op, right)

    def describe(self) -> str:
        return f"{self.left} {self.op} {self.right}"


@dataclass(frozen=True)
class AllOf(Condition):
    """Logical AND over child conditions (vacuously true when empty)."""

    children: Sequence[Condition]

    def holds(self, request: AuthorizationRequest) -> bool:
        return all(c.holds(request) for c in self.children)

    def describe(self) -> str:
        return "(" + " AND ".join(c.describe() for c in self.children) + ")"


@dataclass(frozen=True)
class AnyOf(Condition):
    """Logical OR over child conditions (vacuously false when empty)."""

    children: Sequence[Condition]

    def holds(self, request: AuthorizationRequest) -> bool:
        return any(c.holds(request) for c in self.children)

    def describe(self) -> str:
        return "(" + " OR ".join(c.describe() for c in self.children) + ")"


@dataclass(frozen=True)
class Not(Condition):
    """Logical NOT of a child condition."""

    child: Condition

    def holds(self, request: AuthorizationRequest) -> bool:
        return not self.child.holds(request)

    def describe(self) -> str:
        return f"NOT {self.child.describe()}"


def _compare(left: Any, op: str, right: Any) -> bool:
    """Evaluate ``left <op> right`` with safe handling of missing values."""
    if op == "eq":
        return left == right
    if op == "ne":
        return left != right
    if op == "in":
        return _is_member(left, right)
    if op == "contains":
        return _is_member(right, left)
    if left is None or right is None:
        return False
    try:
        if op == "gt":
            return left > right
        if op == "ge":
            return left >= right
        if op == "lt":
            return left < right
        if op == "le":
            return left <= right
    except TypeError:
        return False
    raise ValueError(f"unknown comparison operator: {op!r}")


def _is_member(needle: Any, haystack: Any) -> bool:
    if haystack is None:
        return False
    if isinstance(haystack, (str, bytes)):
        return needle in haystack if needle is not None else False
    try:
        return needle in haystack
    except TypeError:
        return False


# --------------------------------------------------------------------------- #
# Rules + the engine
# --------------------------------------------------------------------------- #


class AbacEffect(enum.StrEnum):
    """An ABAC rule's verdict when it matches (ALLOW or explicit DENY)."""

    ALLOW = "allow"
    DENY = "deny"


def action_matches(pattern: str, action: str) -> bool:
    """Whether an action matches a rule's pattern (``*`` and ``ns:*`` aware)."""
    if pattern == "*":
        return True
    if pattern == action:
        return True
    if pattern.endswith(":*"):
        return action.split(":", 1)[0] == pattern[:-2]
    return False


@dataclass(frozen=True)
class AbacRule:
    """One attribute-based rule: match an action + condition → an effect.

    ``actions`` is a set of action patterns the rule applies to (``*`` matches
    any). ``condition`` is the predicate that must hold. ``effect`` is what to
    return on a match. ``obligations`` ride on an ALLOW match.
    """

    name: str
    actions: frozenset[str]
    condition: Condition
    effect: AbacEffect = AbacEffect.ALLOW
    obligations: tuple[Obligation, ...] = ()
    description: str = ""

    def applies_to(self, action: str) -> bool:
        return any(action_matches(p, action) for p in self.actions)


class AbacEngine(SyncEngine):
    """First-applicable ABAC over an ordered rule list; abstain if none match.

    Rules are evaluated in order. A DENY rule that matches short-circuits with an
    explicit DENY; an ALLOW rule that matches returns ALLOW with its obligations.
    If no rule both applies to the action and has a satisfied condition, the
    engine abstains.
    """

    name = "abac"

    def __init__(self, rules: Iterable[AbacRule]) -> None:
        self._rules: tuple[AbacRule, ...] = tuple(rules)

    @property
    def rules(self) -> tuple[AbacRule, ...]:
        return self._rules

    def evaluate(self, request: AuthorizationRequest) -> EngineResult:
        for rule in self._rules:
            if not rule.applies_to(request.action):
                continue
            if not rule.condition.holds(request):
                continue
            detail = rule.description or rule.condition.describe()
            if rule.effect is AbacEffect.DENY:
                return EngineResult.deny(self.name, detail, rule=rule.name)
            return EngineResult.allow(
                self.name, detail, rule=rule.name, obligations=rule.obligations
            )
        return EngineResult(
            effect=EngineResult.abstain(self.name).effect,
            reasons=(
                Reason(
                    source=self.name,
                    effect=EngineResult.abstain(self.name).effect,
                    detail=f"no ABAC rule applies to '{request.action}'",
                ),
            ),
        )


# Convenience builders for the common ownership/tenancy predicates ----------- #


def is_owner(owner_path: str = "resource.owner", subject_path: str = "subject.id") -> Condition:
    """``subject is the resource owner`` — the canonical personal-ownership rule."""
    return AttrEqAttr(left=subject_path, right=owner_path)


def same_tenant() -> Condition:
    """``subject.tenant == resource.tenant`` — tenancy isolation."""
    return AttrEqAttr(left="subject.tenant", right="resource.tenant")


def has_attr(path: str, value: Any) -> Condition:
    """``<path> == value`` — a simple attribute equality."""
    return Attr(path=path, op="eq", value=value)


__all__ = [
    "AbacEffect",
    "AbacEngine",
    "AbacRule",
    "AllOf",
    "Always",
    "AnyOf",
    "Attr",
    "AttrEqAttr",
    "Condition",
    "Not",
    "action_matches",
    "has_attr",
    "is_owner",
    "resolve_attr",
    "same_tenant",
]
