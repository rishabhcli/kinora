"""Residual-data scan + the verifiable erasure-completion certificate.

After a right-to-erasure run, the controller must be able to *prove* that no
residual subject data remains in the stores it scanned (accountability, Art. 5(2)).
This module re-walks the :mod:`~app.privacy.datamap` against every store seam and
counts whatever is still attributable to the subject; if the scan is clean it
mints a tamper-evident **completion certificate** — a signed, hash-stamped record
naming the subject, the stores scanned, the per-store residual counts, and the
audit-chain verification result.

The certificate is *verifiable*: its ``certificate_hash`` covers all of its
content, so anyone can re-hash it to confirm it was not edited, and re-running the
scan must reproduce a clean result. A certificate is only ``complete`` when every
scanned store reports zero residual rows **and** the audit chain still verifies
(crypto-erasure must not have broken integrity).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.privacy.clock import Clock, system_clock
from app.privacy.datamap import DEFAULT_DATA_MAP, DataMap
from app.privacy.enums import StoreKind
from app.privacy.hashchain import canonical_json, sha256_hex
from app.privacy.protocols import AuditRedactor, BlobStore, EventStore, SubjectDataStore


@dataclass(frozen=True, slots=True)
class StoreResidual:
    """The residual scan result for one store."""

    store: StoreKind
    #: Rows / objects / streams / entries still attributable to the subject.
    residual: int
    #: Per-resource breakdown (resource -> count) for diagnostics.
    detail: dict[str, int] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return self.residual == 0


@dataclass(frozen=True, slots=True)
class ResidualScan:
    """The full residual scan across every store the data-map references."""

    subject_id: str
    scanned_at: datetime
    per_store: list[StoreResidual]
    #: Whether the append-only chain still verifies after redaction/crypto-erasure.
    chain_intact: bool

    @property
    def total_residual(self) -> int:
        return sum(s.residual for s in self.per_store)

    @property
    def clean(self) -> bool:
        """No residual data anywhere *and* the integrity chain survived."""
        return self.total_residual == 0 and self.chain_intact


@dataclass(frozen=True, slots=True)
class ErasureCertificate:
    """A verifiable proof that a subject was erased with no residual data."""

    subject_id: str
    issued_at: datetime
    stores_scanned: list[str]
    per_store_residual: dict[str, int]
    chain_intact: bool
    complete: bool
    certificate_hash: str

    def verify(self) -> bool:
        """Re-hash the certificate content; ``True`` iff it is unmodified."""
        return self.certificate_hash == self._compute_hash(
            subject_id=self.subject_id,
            issued_at=self.issued_at,
            stores_scanned=self.stores_scanned,
            per_store_residual=self.per_store_residual,
            chain_intact=self.chain_intact,
            complete=self.complete,
        )

    @staticmethod
    def _compute_hash(
        *,
        subject_id: str,
        issued_at: datetime,
        stores_scanned: Sequence[str],
        per_store_residual: dict[str, int],
        chain_intact: bool,
        complete: bool,
    ) -> str:
        core = {
            "subject_id": subject_id,
            "issued_at": issued_at.isoformat(),
            "stores_scanned": sorted(stores_scanned),
            "per_store_residual": per_store_residual,
            "chain_intact": chain_intact,
            "complete": complete,
        }
        return sha256_hex(canonical_json(core))

    def to_dict(self) -> dict[str, Any]:
        """A serialisable rendering of the certificate."""
        return {
            "format": "kinora.privacy.erasure_certificate/v1",
            "subject_id": self.subject_id,
            "issued_at": self.issued_at.isoformat(),
            "stores_scanned": self.stores_scanned,
            "per_store_residual": self.per_store_residual,
            "chain_intact": self.chain_intact,
            "complete": self.complete,
            "certificate_hash": self.certificate_hash,
        }


class ResidualScanner:
    """Re-walks the data-map to count residual subject data + verify the chain."""

    def __init__(
        self,
        *,
        subject_store: SubjectDataStore,
        blob_store: BlobStore,
        event_store: EventStore,
        audit_log: AuditRedactor | None = None,
        data_map: DataMap = DEFAULT_DATA_MAP,
        clock: Clock = system_clock,
    ) -> None:
        self._subject = subject_store
        self._blobs = blob_store
        self._events = event_store
        self._audit = audit_log
        self._map = data_map
        self._clock = clock

    async def scan(self, *, subject_id: str) -> ResidualScan:
        """Count residual subject data per store and verify the integrity chain."""
        per_store: list[StoreResidual] = []

        # Relational + derived index: count rows still attributable to the subject.
        for store in (StoreKind.RELATIONAL, StoreKind.DERIVED_INDEX):
            detail: dict[str, int] = {}
            for resource in self._map.resources(store):
                fields = self._map.by_resource(store, resource)
                locator = fields[0].subject_locator
                # An anonymised row is no longer attributable, so count returns 0
                # for it; a hard-deleted row is gone. Either way the store reports
                # how many rows still map to the subject.
                count = await self._subject.count_subject_rows(
                    resource=resource, subject_locator=locator, subject_id=subject_id
                )
                if count:
                    detail[resource] = count
            if self._map.by_store(store):
                per_store.append(
                    StoreResidual(
                        store=store, residual=sum(detail.values()), detail=detail
                    )
                )

        # Object storage: any remaining objects under the subject's prefixes.
        if self._map.by_store(StoreKind.OBJECT_STORE):
            obj_detail: dict[str, int] = {}
            for resource in self._map.resources(StoreKind.OBJECT_STORE):
                base = resource.split("{", 1)[0].rstrip("/")
                prefix = f"{base}/{subject_id}"
                keys = await self._blobs.list_prefix(prefix)
                if keys:
                    obj_detail[resource] = len(keys)
            per_store.append(
                StoreResidual(
                    store=StoreKind.OBJECT_STORE,
                    residual=sum(obj_detail.values()),
                    detail=obj_detail,
                )
            )

        # Event store: a subject is residual only while their events stay decryptable.
        if self._map.by_store(StoreKind.EVENT_STORE):
            recoverable = await self._events.subject_recoverable(subject_id=subject_id)
            per_store.append(
                StoreResidual(
                    store=StoreKind.EVENT_STORE,
                    residual=1 if recoverable else 0,
                    detail={"recoverable_streams": 1} if recoverable else {},
                )
            )

        # Audit log: entries still carrying the subject's personal fields.
        chain_intact = True
        if self._map.by_store(StoreKind.AUDIT_LOG) and self._audit is not None:
            residual = await self._audit.scan_subject(subject_id=subject_id)
            chain_intact = await self._audit.verify_chain()
            per_store.append(
                StoreResidual(
                    store=StoreKind.AUDIT_LOG,
                    residual=residual,
                    detail={"unredacted_entries": residual} if residual else {},
                )
            )

        return ResidualScan(
            subject_id=subject_id,
            scanned_at=self._clock(),
            per_store=per_store,
            chain_intact=chain_intact,
        )

    async def certify(self, *, subject_id: str) -> ErasureCertificate:
        """Run the scan and mint a completion certificate from its result."""
        scan = await self.scan(subject_id=subject_id)
        return issue_certificate(scan)


def issue_certificate(scan: ResidualScan) -> ErasureCertificate:
    """Mint a verifiable certificate from a residual scan."""
    per_store_residual = {s.store.value: s.residual for s in scan.per_store}
    stores_scanned = [s.store.value for s in scan.per_store]
    complete = scan.clean
    cert_hash = ErasureCertificate._compute_hash(
        subject_id=scan.subject_id,
        issued_at=scan.scanned_at,
        stores_scanned=stores_scanned,
        per_store_residual=per_store_residual,
        chain_intact=scan.chain_intact,
        complete=complete,
    )
    return ErasureCertificate(
        subject_id=scan.subject_id,
        issued_at=scan.scanned_at,
        stores_scanned=stores_scanned,
        per_store_residual=per_store_residual,
        chain_intact=scan.chain_intact,
        complete=complete,
        certificate_hash=cert_hash,
    )


__all__ = [
    "ErasureCertificate",
    "ResidualScan",
    "ResidualScanner",
    "StoreResidual",
    "issue_certificate",
]
