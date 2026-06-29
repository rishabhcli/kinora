"""The workload registration registry (the SPIFFE-server entry table).

A :class:`RegistrationEntry` says *"a workload that attests these selectors is
entitled to this SPIFFE ID"* — the authoritative binding the issuance authority
consults. The :class:`WorkloadRegistry` is the in-memory store of those entries
plus the lookup the issuer uses at registration time.

Matching favours **specificity**: when several entries' required selectors are
all satisfied by an attestation, the one requiring the *most* selectors wins
(a ``k8s:ns:render`` + ``k8s:sa:worker`` entry beats a bare ``k8s:ns:render``
entry), so a narrowly-registered workload always gets its specific identity.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import timedelta

from app.zerotrust.identity.attestation import (
    AttestationResult,
    Selector,
    parse_selectors,
    selectors_satisfy,
)
from app.zerotrust.identity.ca import DEFAULT_LEAF_TTL
from app.zerotrust.identity.errors import IdentityError, UnknownWorkloadError
from app.zerotrust.identity.spiffe import SpiffeId, TrustDomain


@dataclass(frozen=True, slots=True)
class RegistrationEntry:
    """A binding from a selector set to the SPIFFE ID a matching workload gets."""

    spiffe_id: SpiffeId
    selectors: frozenset[Selector] = frozenset()
    #: leaf TTL the issuer should mint for this workload
    svid_ttl: timedelta = DEFAULT_LEAF_TTL
    #: extra DNS SANs to add to the leaf (for legacy TLS clients)
    dns_sans: tuple[str, ...] = ()
    #: SPIFFE IDs this workload is permitted to *federate with* / call.
    #: Consumed by the policy seam; stored here so registration is the single
    #: source of truth.
    downstream: frozenset[SpiffeId] = frozenset()

    @property
    def specificity(self) -> int:
        return len(self.selectors)


@dataclass(slots=True)
class WorkloadRegistry:
    """An in-memory store of :class:`RegistrationEntry` bound to one trust domain."""

    trust_domain: TrustDomain
    _entries: list[RegistrationEntry] = field(default_factory=list)

    def __init__(self, trust_domain: str | TrustDomain) -> None:
        self.trust_domain = (
            trust_domain
            if isinstance(trust_domain, TrustDomain)
            else TrustDomain(trust_domain)
        )
        self._entries = []

    def register(
        self,
        spiffe_id: str | SpiffeId,
        selectors: Iterable[str] | Iterable[Selector] = (),
        *,
        svid_ttl: timedelta = DEFAULT_LEAF_TTL,
        dns_sans: Iterable[str] = (),
        downstream: Iterable[str | SpiffeId] = (),
    ) -> RegistrationEntry:
        """Register (or replace) a workload entry; returns the stored entry."""

        sid = spiffe_id if isinstance(spiffe_id, SpiffeId) else SpiffeId.parse(spiffe_id)
        sid.require_domain(self.trust_domain)
        if sid.is_trust_domain:
            raise IdentityError("cannot register a bare trust-domain id")
        sel_set = _coerce_selectors(selectors)
        down = frozenset(
            d if isinstance(d, SpiffeId) else SpiffeId.parse(d) for d in downstream
        )
        entry = RegistrationEntry(
            spiffe_id=sid,
            selectors=sel_set,
            svid_ttl=svid_ttl,
            dns_sans=tuple(dns_sans),
            downstream=down,
        )
        # replace any entry with the identical (spiffe_id, selectors) key
        self._entries = [
            e
            for e in self._entries
            if not (e.spiffe_id == sid and e.selectors == sel_set)
        ]
        self._entries.append(entry)
        return entry

    def entries(self) -> tuple[RegistrationEntry, ...]:
        return tuple(self._entries)

    def by_id(self, spiffe_id: str | SpiffeId) -> tuple[RegistrationEntry, ...]:
        sid = spiffe_id if isinstance(spiffe_id, SpiffeId) else SpiffeId.parse(spiffe_id)
        return tuple(e for e in self._entries if e.spiffe_id == sid)

    def require_id(self, spiffe_id: str | SpiffeId) -> RegistrationEntry:
        matches = self.by_id(spiffe_id)
        if not matches:
            raise UnknownWorkloadError(f"no registration entry for {spiffe_id}")
        return matches[0]

    def match(self, attestation: AttestationResult) -> RegistrationEntry | None:
        """Most-specific entry whose required selectors the attestation satisfies."""

        best: RegistrationEntry | None = None
        for entry in self._entries:
            if not selectors_satisfy(entry.selectors, attestation.selectors):
                continue
            if best is None or entry.specificity > best.specificity:
                best = entry
        return best

    def require_match(self, attestation: AttestationResult) -> RegistrationEntry:
        entry = self.match(attestation)
        if entry is None:
            raise UnknownWorkloadError(
                "attested selectors match no registration entry: "
                + ", ".join(sorted(str(s) for s in attestation.selectors))
            )
        return entry


def _coerce_selectors(
    selectors: Iterable[str] | Iterable[Selector],
) -> frozenset[Selector]:
    items = list(selectors)
    if not items:
        return frozenset()
    if all(isinstance(i, Selector) for i in items):
        return frozenset(items)  # type: ignore[arg-type]
    return parse_selectors(i for i in items if isinstance(i, str))


__all__ = ["RegistrationEntry", "WorkloadRegistry"]
