"""The privacy-subsystem facade — one entry point for data-subject rights.

Composes the data-map, retention engine, consent tracker, DSAR exporter,
right-to-erasure orchestrator and completion certificate into a single service a
DSAR fulfiller (or an API route) calls. It is a thin coordinator: each capability
lives in its own module and is independently testable; this class just wires the
store seams and exposes the regulator-facing verbs.

It deliberately mirrors the ``Fulfiller`` shape the sibling
:mod:`app.compliance.dsar.service` expects (``export`` / ``erase`` returning a
machine-readable summary), so the governance workflow can delegate the actual data
work here once both packages land — without either importing the other.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from app.privacy.certificate import ErasureCertificate, ResidualScan, ResidualScanner
from app.privacy.clock import Clock, system_clock
from app.privacy.consent import ConsentTracker
from app.privacy.datamap import DEFAULT_DATA_MAP, DataMap
from app.privacy.erasure import ErasureOrchestrator, ErasureRun
from app.privacy.export import DSARExporter, ExportBundle
from app.privacy.protocols import AuditRedactor, BlobStore, EventStore, SubjectDataStore
from app.privacy.retention import LegalHold, RetentionPolicy, default_retention_policy


class PrivacyService:
    """Coordinates DSAR export, right-to-erasure, retention + the certificate."""

    def __init__(
        self,
        *,
        subject_store: SubjectDataStore,
        blob_store: BlobStore,
        event_store: EventStore,
        audit_log: AuditRedactor | None = None,
        data_map: DataMap = DEFAULT_DATA_MAP,
        retention: RetentionPolicy | None = None,
        consent: ConsentTracker | None = None,
        consent_purposes: Sequence[str] = (),
        clock: Clock = system_clock,
        redaction_marker: str = "[REDACTED]",
    ) -> None:
        self._map = data_map
        self._retention = retention or default_retention_policy(clock=clock)
        self._consent = consent
        self._consent_purposes = tuple(consent_purposes)
        self._exporter = DSARExporter(
            subject_store=subject_store,
            blob_store=blob_store,
            event_store=event_store,
            audit_log=audit_log,
            data_map=data_map,
            consent=consent,
            consent_purposes=consent_purposes,
            clock=clock,
        )
        self._orchestrator = ErasureOrchestrator(
            subject_store=subject_store,
            blob_store=blob_store,
            event_store=event_store,
            audit_log=audit_log,
            data_map=data_map,
            retention=self._retention,
            consent=consent,
            clock=clock,
            redaction_marker=redaction_marker,
        )
        self._scanner = ResidualScanner(
            subject_store=subject_store,
            blob_store=blob_store,
            event_store=event_store,
            audit_log=audit_log,
            data_map=data_map,
            clock=clock,
        )

    @property
    def data_map(self) -> DataMap:
        return self._map

    @property
    def retention(self) -> RetentionPolicy:
        return self._retention

    async def export_subject(self, *, subject_id: str) -> ExportBundle:
        """DSAR access/portability (Art. 15 / Art. 20): a portable copy of the data."""
        return await self._exporter.export(subject_id=subject_id)

    async def erase_subject(
        self,
        *,
        subject_id: str,
        holds: Iterable[LegalHold] = (),
        run: ErasureRun | None = None,
        certify: bool = True,
    ) -> ErasureRun:
        """Right-to-erasure (Art. 17): coherent, idempotent, resumable deletion."""
        return await self._orchestrator.erase(
            subject_id=subject_id, holds=holds, run=run, certify=certify
        )

    async def scan_subject(self, *, subject_id: str) -> ResidualScan:
        """Residual-data scan: what (if anything) still maps to the subject."""
        return await self._scanner.scan(subject_id=subject_id)

    async def certificate(self, *, subject_id: str) -> ErasureCertificate:
        """Mint a verifiable erasure-completion certificate from a fresh scan."""
        return await self._scanner.certify(subject_id=subject_id)

    def article30_record(self) -> list[dict[str, Any]]:
        """The Art. 30 record-of-processing projection of the data-map."""
        return self._map.article30_record()


__all__ = ["PrivacyService"]
