"""DSAR access / portability export — assemble a subject's whole footprint.

GDPR Art. 15 (right of access) + Art. 20 (data portability) entitle a subject to a
copy of all the personal data the controller holds about them, in a structured,
machine-readable form. This module walks the :mod:`~app.privacy.datamap`, queries
every store seam for the subject's data, and assembles it into one portable bundle
— grouped by store and resource, with a manifest that proves *coverage* (which
mapped fields contributed and from which store).

Credential fields (``exportable=False`` in the data-map) are never included — a
DSAR export must not leak a password hash. Append-only stores are summarised
(stream ids / entry counts) rather than dumped verbatim, since their raw form is
not personal-data-portable and may be crypto-erased.

The assembler is store-agnostic (it only knows the protocols), so the test suite
drives it entirely with in-memory fakes and asserts completeness against the map.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.privacy.clock import Clock, system_clock
from app.privacy.consent import ConsentTracker, PurposeConsent
from app.privacy.datamap import DEFAULT_DATA_MAP, DataMap, PIIField
from app.privacy.enums import StoreKind
from app.privacy.hashchain import canonical_json, sha256_hex
from app.privacy.protocols import AuditRedactor, BlobStore, EventStore, SubjectDataStore


@dataclass(frozen=True, slots=True)
class ExportSection:
    """One store's contribution to the export bundle."""

    store: StoreKind
    resource: str
    #: The exported rows / object keys / summary records for this resource.
    records: list[dict[str, Any]]
    #: The data-map field keys that sourced this section (coverage proof).
    field_keys: list[str]


@dataclass(frozen=True, slots=True)
class ExportBundle:
    """The assembled, portable copy of everything a subject's data touches."""

    subject_id: str
    generated_at: datetime
    sections: list[ExportSection]
    consent: list[PurposeConsent]
    #: data-map field key -> store it was satisfied from (coverage map).
    coverage: dict[str, str]
    #: Stable digest over the bundle's content (integrity + dedupe).
    digest: str

    @property
    def record_count(self) -> int:
        """Total exported records across all sections."""
        return sum(len(s.records) for s in self.sections)

    def to_portable_dict(self) -> dict[str, Any]:
        """A JSON-serialisable, machine-readable rendering (Art. 20)."""
        return {
            "format": "kinora.dsar.export/v1",
            "subject_id": self.subject_id,
            "generated_at": self.generated_at.isoformat(),
            "digest": self.digest,
            "coverage": self.coverage,
            "consent": [
                {
                    "purpose": c.purpose,
                    "status": c.status.value,
                    "last_action_at": c.last_action_at.isoformat()
                    if c.last_action_at
                    else None,
                    "policy_version": c.policy_version,
                }
                for c in self.consent
            ],
            "sections": [
                {
                    "store": s.store.value,
                    "resource": s.resource,
                    "field_keys": s.field_keys,
                    "records": s.records,
                }
                for s in self.sections
            ],
        }


class DSARExporter:
    """Assembles a subject's full data export from every store seam."""

    def __init__(
        self,
        *,
        subject_store: SubjectDataStore,
        blob_store: BlobStore,
        event_store: EventStore,
        audit_log: AuditRedactor | None = None,
        data_map: DataMap = DEFAULT_DATA_MAP,
        consent: ConsentTracker | None = None,
        consent_purposes: Sequence[str] = (),
        clock: Clock = system_clock,
    ) -> None:
        self._subject = subject_store
        self._blobs = blob_store
        self._events = event_store
        self._audit = audit_log
        self._map = data_map
        self._consent = consent
        self._consent_purposes = tuple(consent_purposes)
        self._clock = clock

    async def export(self, *, subject_id: str) -> ExportBundle:
        """Walk the data-map and assemble the subject's portable bundle."""
        sections: list[ExportSection] = []
        coverage: dict[str, str] = {}
        fields = self._map.exportable()

        # Group exportable fields by (store, resource) so each resource is one
        # section, and we query each store the minimum number of times.
        for store in StoreKind:
            store_fields = [f for f in fields if f.store is store]
            if not store_fields:
                continue
            by_resource: dict[str, list[PIIField]] = {}
            for f in store_fields:
                by_resource.setdefault(f.resource, []).append(f)
            for resource, rfields in by_resource.items():
                records = await self._export_resource(
                    store=store,
                    resource=resource,
                    fields=rfields,
                    subject_id=subject_id,
                )
                field_keys = [f.key for f in rfields]
                for k in field_keys:
                    coverage[k] = store.value
                sections.append(
                    ExportSection(
                        store=store,
                        resource=resource,
                        records=records,
                        field_keys=field_keys,
                    )
                )

        consent_snapshot: list[PurposeConsent] = []
        if self._consent is not None and self._consent_purposes:
            consent_snapshot = self._consent.snapshot(
                subject_id=subject_id, purposes=self._consent_purposes
            )

        generated_at = self._clock()
        digest = self._digest(subject_id, sections, consent_snapshot)
        return ExportBundle(
            subject_id=subject_id,
            generated_at=generated_at,
            sections=sections,
            consent=consent_snapshot,
            coverage=coverage,
            digest=digest,
        )

    async def _export_resource(
        self,
        *,
        store: StoreKind,
        resource: str,
        fields: list[PIIField],
        subject_id: str,
    ) -> list[dict[str, Any]]:
        """Query one store-resource for the subject's data."""
        locator = fields[0].subject_locator
        if store in (StoreKind.RELATIONAL, StoreKind.DERIVED_INDEX):
            rows = await self._subject.fetch_subject_rows(
                resource=resource, subject_locator=locator, subject_id=subject_id
            )
            # Project to only the exportable columns the map declares (+ any id).
            wanted = {f.field for f in fields} | {locator}
            return [{k: v for k, v in row.items() if k in wanted} for row in rows]
        if store is StoreKind.OBJECT_STORE:
            prefix = self._object_prefix(resource, subject_id)
            keys = await self._blobs.list_prefix(prefix)
            return [{"object_key": k} for k in keys]
        if store is StoreKind.EVENT_STORE:
            streams = await self._events.list_subject_streams(subject_id=subject_id)
            # Summarised, not dumped (raw events are not portable personal data).
            return [{"stream_id": s, "summarised": True} for s in streams]
        if store is StoreKind.AUDIT_LOG:
            if self._audit is None:
                return []
            count = await self._audit.scan_subject(subject_id=subject_id)
            return [{"audit_entries": count, "summarised": True}] if count else []
        return []

    @staticmethod
    def _object_prefix(resource: str, subject_id: str) -> str:
        """Render an object-store resource template into a subject-scoped prefix.

        The data-map's object resources use ``{book_id}``-style templates; for the
        subject scope we prefix with the subject id so a fake / real store lists
        only that subject's objects. (A real adapter would resolve the subject's
        books first; the seam keeps that detail out of the exporter.)
        """
        base = resource.split("{", 1)[0].rstrip("/")
        return f"{base}/{subject_id}"

    @staticmethod
    def _digest(
        subject_id: str,
        sections: Sequence[ExportSection],
        consent: Sequence[PurposeConsent],
    ) -> str:
        """A deterministic content digest over the assembled bundle."""
        core = {
            "subject_id": subject_id,
            "sections": [
                {
                    "store": s.store.value,
                    "resource": s.resource,
                    "records": s.records,
                }
                for s in sections
            ],
            "consent": [
                {"purpose": c.purpose, "status": c.status.value} for c in consent
            ],
        }
        return sha256_hex(canonical_json(core))


__all__ = [
    "DSARExporter",
    "ExportBundle",
    "ExportSection",
]
