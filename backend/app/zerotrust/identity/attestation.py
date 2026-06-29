"""Workload attestation — proving a caller *is* the workload it claims to be.

Before the issuance authority hands a workload an SVID, the workload must prove
its identity by presenting **selectors** (``k8s:ns:render``, ``unix:uid:1000``,
``docker:image:kinora/render-worker@sha256:...``). A node/agent attestor verifies
those selectors out-of-band; this module models the *result* of that verification
as a pure value the issuer can match against registration entries.

The seam is a :class:`WorkloadAttestor` protocol so a real deployment can plug in
a Kubernetes / AWS-IID / Unix-PID attestor, while tests use
:class:`StaticAttestor` (returns a fixed selector set) to drive issuance
deterministically. The matching rule (:func:`selectors_satisfy`) is the same in
both worlds: an entry's required selectors must be a **subset** of the attested
selectors — extra attested selectors are fine, missing required ones are not.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Selector:
    """A single attested fact about a workload (``type``:``value``).

    ``type`` names the attestor family (``k8s``, ``unix``, ``docker``, ``aws``);
    ``value`` is the colon-joined remainder (``ns:render``, ``uid:1000``).
    """

    type: str
    value: str

    def __post_init__(self) -> None:
        if not self.type or ":" in self.type:
            raise ValueError("selector type must be non-empty and contain no ':'")
        if not self.value:
            raise ValueError("selector value must be non-empty")

    @classmethod
    def parse(cls, raw: str) -> Selector:
        """Parse ``type:value`` (value may itself contain colons)."""

        head, sep, tail = raw.partition(":")
        if not sep:
            raise ValueError(f"selector {raw!r} must be 'type:value'")
        return cls(head, tail)

    def __str__(self) -> str:
        return f"{self.type}:{self.value}"


def parse_selectors(raw: Iterable[str]) -> frozenset[Selector]:
    """Parse an iterable of ``type:value`` strings into a selector set."""

    return frozenset(Selector.parse(r) for r in raw)


@dataclass(frozen=True, slots=True)
class AttestationResult:
    """The proven outcome of attesting a workload."""

    selectors: frozenset[Selector]
    #: opaque attestor metadata (node name, attestation method, etc.)
    metadata: Mapping[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.metadata is None:
            object.__setattr__(self, "metadata", {})

    def has(self, selector: str | Selector) -> bool:
        sel = Selector.parse(selector) if isinstance(selector, str) else selector
        return sel in self.selectors


def selectors_satisfy(
    required: frozenset[Selector],
    attested: frozenset[Selector],
) -> bool:
    """Whether *attested* satisfies *required* (required ⊆ attested).

    An empty ``required`` set means "any attested workload" — used for catch-all
    registration entries. A non-empty set must be fully present.
    """

    return required <= attested


class WorkloadAttestor(Protocol):
    """Produces an :class:`AttestationResult` for a presenting workload."""

    def attest(self, evidence: Mapping[str, str]) -> AttestationResult:  # pragma: no cover
        ...


@dataclass(slots=True)
class StaticAttestor:
    """A deterministic attestor that always returns a fixed selector set.

    The default test attestor: construct with the selectors the workload should
    be proven to have, then feed it to the issuer.
    """

    selectors: frozenset[Selector]
    metadata: Mapping[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.metadata is None:
            self.metadata = {}

    @classmethod
    def of(cls, *selectors: str) -> StaticAttestor:
        return cls(parse_selectors(selectors))

    def attest(self, evidence: Mapping[str, str]) -> AttestationResult:  # noqa: ARG002
        return AttestationResult(self.selectors, dict(self.metadata))


__all__ = [
    "AttestationResult",
    "Selector",
    "StaticAttestor",
    "WorkloadAttestor",
    "parse_selectors",
    "selectors_satisfy",
]
