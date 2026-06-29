"""The core authorization vocabulary — the request document and decision types.

This module is the **shared kernel** of the unified authorization plane
(``backend/app/platform/authz/``). Every engine in the plane — the RBAC/ABAC
evaluator, the Rego-style policy DSL, the Zanzibar relationship checker — speaks
in terms of the small value types defined here, and the unified ``check()`` SDK
composes their decisions. Keeping the vocabulary in one I/O-free module means the
whole plane is exhaustively unit-testable without infrastructure.

The design follows the AWS/OPA *request document* shape: an authorization
question is fully described by a four-part tuple ::

    check(subject, action, resource, context) -> Decision

* **Subject** — *who* is asking (a user, an API key, a service). Carries the
  attributes (roles, tenant, groups, custom claims) that ABAC rules read.
* **Action** — *what* verb is attempted (``book:read``, ``workspace:share``).
* **Resource** — *what* is being acted on (a typed, identified object plus its
  own attributes — owner, tenant, visibility).
* **Context** — *environment* facts that are neither subject nor resource (the
  request IP, time of day, MFA freshness, a feature-flag value).

The result is a :class:`Decision`: an :class:`Effect` (ALLOW/DENY), the set of
:class:`Obligation`\\s the caller must honour, and a structured *explanation* of
every policy/relation/role that contributed — so "why was I denied?" is always
answerable. ``Effect`` is a three-valued lattice (ALLOW/DENY/ABSTAIN) so an
engine that has *no opinion* on a request is distinguishable from one that
actively denies — the combining algorithm (:mod:`app.platform.authz.combining`)
needs that distinction to implement deny-overrides correctly.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# --------------------------------------------------------------------------- #
# Effects — the three-valued decision lattice
# --------------------------------------------------------------------------- #


class Effect(enum.StrEnum):
    """The verdict a single rule / engine produces for a request.

    Three-valued on purpose: ``ABSTAIN`` ("no applicable rule / no opinion") is
    distinct from ``DENY`` ("an applicable rule actively forbids this"). The
    combining algorithms treat them very differently — a default-deny base only
    fires when *every* engine abstains, whereas an explicit ``DENY`` overrides
    even a present ``ALLOW``.
    """

    ALLOW = "allow"
    DENY = "deny"
    ABSTAIN = "abstain"

    @property
    def is_decisive(self) -> bool:
        """True for ALLOW/DENY (an opinion); False for ABSTAIN (no opinion)."""
        return self is not Effect.ABSTAIN


# --------------------------------------------------------------------------- #
# Subject / Resource / Context — the request document
# --------------------------------------------------------------------------- #


def _freeze(attrs: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Return an immutable shallow copy of an attribute mapping (or empty)."""
    if not attrs:
        return {}
    return dict(attrs)


@dataclass(frozen=True, slots=True)
class Subject:
    """The principal asking the question — *who* is acting.

    ``type`` + ``id`` together form the Zanzibar-style subject reference
    (``user:alice``, ``apikey:ak_7f3``, ``service:render-worker``). The
    ``attributes`` bag holds everything ABAC and the policy DSL can read:
    ``roles`` (a sequence of role names), ``tenant``, ``groups``, ``mfa``, and
    any custom claim. Attributes are kept as a free-form mapping rather than typed
    fields so an adapter can fold an existing principal (auth ``Principal``,
    workspace user id, MCP token) in without losing information.
    """

    type: str
    id: str
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", _freeze(self.attributes))

    @property
    def ref(self) -> str:
        """The ``type:id`` reference string (the Zanzibar subject form)."""
        return f"{self.type}:{self.id}"

    @property
    def roles(self) -> frozenset[str]:
        """The role names the subject holds (from ``attributes['roles']``)."""
        raw = self.attributes.get("roles", ())
        if isinstance(raw, str):
            return frozenset({raw})
        return frozenset(str(r) for r in raw)

    @property
    def tenant(self) -> str | None:
        """The subject's tenant id, if tenant-scoped (else ``None``)."""
        value = self.attributes.get("tenant")
        return str(value) if value is not None else None

    def attr(self, name: str, default: Any = None) -> Any:
        """Read an arbitrary subject attribute (for ABAC / DSL)."""
        return self.attributes.get(name, default)

    @classmethod
    def user(cls, user_id: str, **attributes: Any) -> Subject:
        """Build a ``user:<id>`` subject (the common interactive case)."""
        return cls(type="user", id=user_id, attributes=attributes)

    @classmethod
    def service(cls, name: str, **attributes: Any) -> Subject:
        """Build a ``service:<name>`` subject (a headless caller)."""
        return cls(type="service", id=name, attributes=attributes)


@dataclass(frozen=True, slots=True)
class Resource:
    """The object being acted upon — *what* is targeted.

    ``type`` selects which relation graph / policy set applies (``book``,
    ``workspace``, ``collection``, ``content``). ``id`` is the row id. The
    ``attributes`` bag carries the resource's own facts (``owner``, ``tenant``,
    ``visibility``, ``status``) that ABAC and DSL conditions compare against the
    subject. A resource with ``id == "*"`` denotes the *type* itself, used for
    type-level / collection-scoped questions ("may I create a book?").
    """

    type: str
    id: str
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", _freeze(self.attributes))

    @property
    def ref(self) -> str:
        """The ``type:id`` object reference (the Zanzibar object form)."""
        return f"{self.type}:{self.id}"

    @property
    def owner(self) -> str | None:
        """The resource owner's user id, if recorded (``attributes['owner']``)."""
        value = self.attributes.get("owner")
        return str(value) if value is not None else None

    @property
    def tenant(self) -> str | None:
        """The resource's tenant id, if tenant-scoped (else ``None``)."""
        value = self.attributes.get("tenant")
        return str(value) if value is not None else None

    def attr(self, name: str, default: Any = None) -> Any:
        """Read an arbitrary resource attribute (for ABAC / DSL)."""
        return self.attributes.get(name, default)

    @classmethod
    def of(cls, type_: str, id_: str, **attributes: Any) -> Resource:
        """Build a typed resource reference with attributes."""
        return cls(type=type_, id=id_, attributes=attributes)

    @classmethod
    def type_level(cls, type_: str, **attributes: Any) -> Resource:
        """A wildcard (``type:*``) resource for type-level questions."""
        return cls(type=type_, id="*", attributes=attributes)


@dataclass(frozen=True, slots=True)
class Context:
    """Environment facts that are neither subject nor resource.

    Request-time signals the policy DSL and ABAC can branch on: the caller IP,
    the wall-clock ``now`` (defaulting to evaluation time), MFA freshness, the
    transport (``rest`` / ``mcp`` / ``ws``), feature-flag values. Kept separate
    from subject/resource so a rule's intent reads clearly ("deny unless
    ``context.mfa == True``").
    """

    attributes: Mapping[str, Any] = field(default_factory=dict)
    now: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", _freeze(self.attributes))

    def attr(self, name: str, default: Any = None) -> Any:
        """Read an arbitrary context attribute."""
        return self.attributes.get(name, default)

    @property
    def timestamp(self) -> datetime:
        """The effective evaluation time (``now`` or the current UTC clock)."""
        return self.now or datetime.now(UTC)

    @classmethod
    def empty(cls) -> Context:
        """An empty context (the default for a question with no environment)."""
        return cls()


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    """A fully-described authorization question — the engine input document.

    Bundles the four parts so engines and the cache take a single value. The
    ``cache_key`` is a stable, content-addressed string over the decisive parts
    (it deliberately ignores ``context.now`` so two otherwise-identical requests
    a millisecond apart still hit the cache).
    """

    subject: Subject
    action: str
    resource: Resource
    context: Context = field(default_factory=Context.empty)

    @property
    def cache_key(self) -> str:
        """A stable identity for the decision cache (excludes wall-clock ``now``)."""
        ctx = sorted(
            (k, _stable(v)) for k, v in self.context.attributes.items() if k != "now"
        )
        subj = sorted((k, _stable(v)) for k, v in self.subject.attributes.items())
        res = sorted((k, _stable(v)) for k, v in self.resource.attributes.items())
        return (
            f"{self.subject.ref}|{self.action}|{self.resource.ref}"
            f"|s={subj}|r={res}|c={ctx}"
        )


def _stable(value: Any) -> Any:
    """Render a value into a stable, hashable shape for the cache key."""
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(sorted(_stable(v) for v in value))
    if isinstance(value, Mapping):
        return tuple(sorted((k, _stable(v)) for k, v in value.items()))
    return value


# --------------------------------------------------------------------------- #
# Explanation + obligations + the final decision
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Obligation:
    """A side condition the caller must honour when access is granted.

    Obligations let a policy say "allow, *but* you must redact field X / log this
    / step-up MFA". They ride on an ALLOW decision; the enforcement point applies
    them. (The plane records them; it does not itself perform side effects.)
    """

    name: str
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", _freeze(self.parameters))


@dataclass(frozen=True, slots=True)
class Reason:
    """One contributing fact in a decision's explanation trail.

    Every engine appends ``Reason``\\s describing *why* it produced its effect —
    a matched policy rule, a granting relation tuple, a satisfied role. The
    aggregated trail is the answer to "why?" and the basis of the decision log.
    """

    source: str  # which engine: "rbac" | "abac" | "policy" | "rebac" | "adapter"
    effect: Effect
    detail: str
    rule: str | None = None  # the policy/rule/relation identifier, if any

    def render(self) -> str:
        """A one-line human rendering for logs / the 403 body."""
        rule = f" [{self.rule}]" if self.rule else ""
        return f"{self.source}:{self.effect.value}{rule} — {self.detail}"


@dataclass(frozen=True, slots=True)
class Decision:
    """The composed answer to a ``check(...)`` — allow/deny + obligations + why.

    ``allowed`` is the boolean callers branch on. ``effect`` preserves the
    three-valued result (an all-ABSTAIN request resolves to the configured
    default, recorded here). ``reasons`` is the full explanation trail;
    ``obligations`` are conditions to honour on an ALLOW.
    """

    request: AuthorizationRequest
    effect: Effect
    reasons: tuple[Reason, ...] = ()
    obligations: tuple[Obligation, ...] = ()
    cached: bool = False
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def allowed(self) -> bool:
        """Whether access is granted (only an explicit ALLOW grants)."""
        return self.effect is Effect.ALLOW

    def __bool__(self) -> bool:  # ``if check(...):`` reads naturally
        return self.allowed

    @property
    def explanation(self) -> str:
        """A multi-line human explanation of every contributing reason."""
        if not self.reasons:
            return f"{self.effect.value} (no applicable rules; default policy)"
        return "\n".join(r.render() for r in self.reasons)

    def with_flag(self, *, cached: bool) -> Decision:
        """Return a copy with the ``cached`` flag set (used by the cache layer)."""
        return Decision(
            request=self.request,
            effect=self.effect,
            reasons=self.reasons,
            obligations=self.obligations,
            cached=cached,
            evaluated_at=self.evaluated_at,
        )


# --------------------------------------------------------------------------- #
# Partial-decision result — the engine's per-engine output before combining
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class EngineResult:
    """One engine's opinion on a request, before the combiner folds them.

    An engine returns its :class:`Effect` plus the reasons/obligations it
    produced. The combining algorithm (:mod:`app.platform.authz.combining`)
    folds a list of these into the final :class:`Decision`.
    """

    effect: Effect
    reasons: tuple[Reason, ...] = ()
    obligations: tuple[Obligation, ...] = ()

    @classmethod
    def abstain(cls, source: str, detail: str = "no applicable rule") -> EngineResult:
        """A no-opinion result (records why the engine abstained)."""
        return cls(
            effect=Effect.ABSTAIN,
            reasons=(Reason(source=source, effect=Effect.ABSTAIN, detail=detail),),
        )

    @classmethod
    def allow(
        cls,
        source: str,
        detail: str,
        *,
        rule: str | None = None,
        obligations: Iterable[Obligation] = (),
    ) -> EngineResult:
        """An ALLOW result with a reason (and optional obligations)."""
        return cls(
            effect=Effect.ALLOW,
            reasons=(Reason(source=source, effect=Effect.ALLOW, detail=detail, rule=rule),),
            obligations=tuple(obligations),
        )

    @classmethod
    def deny(cls, source: str, detail: str, *, rule: str | None = None) -> EngineResult:
        """A DENY result with a reason."""
        return cls(
            effect=Effect.DENY,
            reasons=(Reason(source=source, effect=Effect.DENY, detail=detail, rule=rule),),
        )


__all__ = [
    "AuthorizationRequest",
    "Context",
    "Decision",
    "Effect",
    "EngineResult",
    "Obligation",
    "Reason",
    "Resource",
    "Subject",
]
