"""The declarative PII inventory / data-map.

A *data-map* is the single source of truth for **where a data subject's personal
data physically lives**: every field, in every store, that holds (or links to)
personal data, annotated with what kind of PII it is, how it must be removed on
erasure, and which retention class governs it. Article 30 GDPR requires
controllers to maintain exactly this record of processing; here it is declarative
metadata so the DSAR-export and right-to-erasure machinery is *generated from the
map* rather than hand-coded per store — add a field to the map and both the
export and the residual scan pick it up automatically.

The map is intentionally pure data (frozen dataclasses): it imports no store, so
it is trivially unit-testable and can be asserted against (coverage tests check
that every :class:`~app.privacy.enums.StoreKind` and retention class is
represented and that append-only stores never declare a destructive strategy).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.privacy.enums import ErasureStrategy, PIICategory, StoreKind
from app.privacy.errors import DataMapError

#: Append-only stores can never hard-delete or anonymise-in-place a row without
#: breaking their integrity chain — the data-map enforces this invariant.
_APPEND_ONLY_STORES: frozenset[StoreKind] = frozenset(
    {StoreKind.EVENT_STORE, StoreKind.AUDIT_LOG}
)
#: The only strategies legal for an append-only store.
_APPEND_ONLY_STRATEGIES: frozenset[ErasureStrategy] = frozenset(
    {ErasureStrategy.CRYPTO_ERASE, ErasureStrategy.REDACT}
)


@dataclass(frozen=True, slots=True)
class PIIField:
    """One personal-data field/locator in one store.

    Attributes:
        store: the physical store this field lives in.
        resource: a logical resource within the store — a table name, an
            object-key prefix, an event ``type``, or an audit category.
        field: the column / JSON key / blob attribute holding the value
            (``"*"`` denotes the whole resource, e.g. an entire blob object).
        category: what kind of PII this is.
        retention_class: the named retention class that governs its TTL
            (resolved against :class:`~app.privacy.retention.RetentionPolicy`).
        erasure: how this field is removed on right-to-erasure.
        subject_locator: how this resource is keyed to a subject — the column /
            key whose value equals (or contains) the subject id. Drives both the
            export query and the residual scan.
        exportable: whether this field is included in a DSAR access/portability
            export (credentials and pure-derived indexes are not).
        description: human-readable note for the Art. 30 record.
    """

    store: StoreKind
    resource: str
    field: str
    category: PIICategory
    retention_class: str
    erasure: ErasureStrategy
    subject_locator: str
    exportable: bool = True
    description: str = ""

    @property
    def key(self) -> str:
        """A stable, unique identifier for this field within the map."""
        return f"{self.store.value}:{self.resource}:{self.field}"

    def __post_init__(self) -> None:
        if self.store in _APPEND_ONLY_STORES and self.erasure not in _APPEND_ONLY_STRATEGIES:
            raise DataMapError(
                f"append-only store {self.store.value!r} field {self.key!r} declares "
                f"destructive strategy {self.erasure.value!r}; only "
                f"{sorted(s.value for s in _APPEND_ONLY_STRATEGIES)} preserve the chain",
            )
        if self.category is PIICategory.CREDENTIAL and self.exportable:
            raise DataMapError(
                f"credential field {self.key!r} must never be exportable (would leak a secret)",
            )


@dataclass(frozen=True, slots=True)
class DataMap:
    """The full, validated inventory of personal-data fields across every store."""

    fields: tuple[PIIField, ...]

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for f in self.fields:
            if f.key in seen:
                raise DataMapError(f"duplicate data-map field {f.key!r}")
            seen.add(f.key)

    def __iter__(self) -> Iterator[PIIField]:
        return iter(self.fields)

    def __len__(self) -> int:
        return len(self.fields)

    # --- queries ------------------------------------------------------------ #

    def by_store(self, store: StoreKind) -> tuple[PIIField, ...]:
        """All fields living in ``store``."""
        return tuple(f for f in self.fields if f.store is store)

    def by_resource(self, store: StoreKind, resource: str) -> tuple[PIIField, ...]:
        """All fields of one resource (e.g. all PII columns of the ``users`` table)."""
        return tuple(f for f in self.fields if f.store is store and f.resource == resource)

    def by_retention_class(self, retention_class: str) -> tuple[PIIField, ...]:
        """All fields governed by a named retention class."""
        return tuple(f for f in self.fields if f.retention_class == retention_class)

    def exportable(self) -> tuple[PIIField, ...]:
        """Fields included in a DSAR access/portability export."""
        return tuple(f for f in self.fields if f.exportable)

    def stores(self) -> frozenset[StoreKind]:
        """The distinct stores referenced by the map."""
        return frozenset(f.store for f in self.fields)

    def resources(self, store: StoreKind) -> tuple[str, ...]:
        """Distinct resources in a store, in first-seen order."""
        out: list[str] = []
        for f in self.fields:
            if f.store is store and f.resource not in out:
                out.append(f.resource)
        return tuple(out)

    def retention_classes(self) -> frozenset[str]:
        """Every retention class named by the map."""
        return frozenset(f.retention_class for f in self.fields)

    def append_only_fields(self) -> tuple[PIIField, ...]:
        """Fields in the append-only stores (event store + audit log)."""
        return tuple(f for f in self.fields if f.store in _APPEND_ONLY_STORES)

    def article30_record(self) -> list[dict[str, Any]]:
        """A serialisable Art. 30 record-of-processing projection of the map."""
        return [
            {
                "key": f.key,
                "store": f.store.value,
                "resource": f.resource,
                "field": f.field,
                "category": f.category.value,
                "retention_class": f.retention_class,
                "erasure": f.erasure.value,
                "subject_locator": f.subject_locator,
                "exportable": f.exportable,
                "description": f.description,
            }
            for f in self.fields
        ]


def merge_maps(*maps: DataMap | Iterable[PIIField]) -> DataMap:
    """Combine partial maps (e.g. per-domain contributions) into one validated map."""
    fields: list[PIIField] = []
    for m in maps:
        fields.extend(m.fields if isinstance(m, DataMap) else m)
    return DataMap(fields=tuple(fields))


# --------------------------------------------------------------------------- #
# Kinora's default data-map. Mirrors the real stores described in AGENTS.md /  #
# kinora.md: Postgres rows, MinIO blobs, the append-only event store, and the  #
# hash-chained audit log. The map drives export + erasure, so adding a column  #
# that holds PII is a one-line edit here.                                      #
# --------------------------------------------------------------------------- #

#: Retention-class names (resolved to TTLs by the retention engine's defaults).
RC_ACCOUNT = "account"
RC_UPLOADED_BOOK = "uploaded_book"
RC_GENERATED_MEDIA = "generated_media"
RC_READING_SESSION = "reading_session"
RC_DIRECTING_PREFERENCE = "directing_preference"
RC_AUDIT_LOG = "audit_log"
RC_EVENT_STREAM = "event_stream"


_DEFAULT_FIELDS: tuple[PIIField, ...] = (
    # --- Relational (Postgres) ---------------------------------------------- #
    PIIField(
        store=StoreKind.RELATIONAL,
        resource="users",
        field="email",
        category=PIICategory.DIRECT_IDENTIFIER,
        retention_class=RC_ACCOUNT,
        erasure=ErasureStrategy.ANONYMIZE,
        subject_locator="id",
        description="The account login email.",
    ),
    PIIField(
        store=StoreKind.RELATIONAL,
        resource="users",
        field="display_name",
        category=PIICategory.DIRECT_IDENTIFIER,
        retention_class=RC_ACCOUNT,
        erasure=ErasureStrategy.ANONYMIZE,
        subject_locator="id",
        description="The reader's display name.",
    ),
    PIIField(
        store=StoreKind.RELATIONAL,
        resource="users",
        field="password_hash",
        category=PIICategory.CREDENTIAL,
        retention_class=RC_ACCOUNT,
        erasure=ErasureStrategy.ANONYMIZE,
        subject_locator="id",
        exportable=False,
        description="Argon2/bcrypt password hash — never exported, cleared on erasure.",
    ),
    PIIField(
        store=StoreKind.RELATIONAL,
        resource="books",
        field="title",
        category=PIICategory.USER_CONTENT,
        retention_class=RC_UPLOADED_BOOK,
        erasure=ErasureStrategy.HARD_DELETE,
        subject_locator="owner_id",
        description="Title of a book the subject uploaded.",
    ),
    PIIField(
        store=StoreKind.RELATIONAL,
        resource="reading_sessions",
        field="trajectory",
        category=PIICategory.BEHAVIOURAL,
        retention_class=RC_READING_SESSION,
        erasure=ErasureStrategy.HARD_DELETE,
        subject_locator="user_id",
        description="Scroll / reading trajectory (behavioural data).",
    ),
    PIIField(
        store=StoreKind.RELATIONAL,
        resource="directing_preferences",
        field="profile",
        category=PIICategory.PREFERENCE,
        retention_class=RC_DIRECTING_PREFERENCE,
        erasure=ErasureStrategy.HARD_DELETE,
        subject_locator="user_id",
        description="The §8.6 learned directing-style profile.",
    ),
    # --- Object storage (MinIO / S3) ---------------------------------------- #
    PIIField(
        store=StoreKind.OBJECT_STORE,
        resource="books/{book_id}/source.pdf",
        field="*",
        category=PIICategory.USER_CONTENT,
        retention_class=RC_UPLOADED_BOOK,
        erasure=ErasureStrategy.HARD_DELETE,
        subject_locator="owner_id",
        description="The uploaded source PDF blob.",
    ),
    PIIField(
        store=StoreKind.OBJECT_STORE,
        resource="clips/{book_id}",
        field="*",
        category=PIICategory.DERIVED_MEDIA,
        retention_class=RC_GENERATED_MEDIA,
        erasure=ErasureStrategy.HARD_DELETE,
        subject_locator="owner_id",
        description="Generated film clips / keyframes / narration audio.",
    ),
    # --- Append-only event store -------------------------------------------- #
    PIIField(
        store=StoreKind.EVENT_STORE,
        resource="book.uploaded",
        field="owner_id",
        category=PIICategory.PSEUDONYMOUS_ID,
        retention_class=RC_EVENT_STREAM,
        erasure=ErasureStrategy.REDACT,
        subject_locator="owner_id",
        description="Subject id embedded in a domain event payload — redacted, chain re-derived.",
    ),
    PIIField(
        store=StoreKind.EVENT_STORE,
        resource="reading.session_recorded",
        field="user_id",
        category=PIICategory.PSEUDONYMOUS_ID,
        retention_class=RC_EVENT_STREAM,
        erasure=ErasureStrategy.CRYPTO_ERASE,
        subject_locator="user_id",
        description="Behavioural event keyed to the subject — crypto-erased (key destroyed).",
    ),
    # --- Hash-chained audit log --------------------------------------------- #
    PIIField(
        store=StoreKind.AUDIT_LOG,
        resource="auth",
        field="email",
        category=PIICategory.DIRECT_IDENTIFIER,
        retention_class=RC_AUDIT_LOG,
        erasure=ErasureStrategy.REDACT,
        subject_locator="subject_id",
        description="Email captured in a security/audit entry — redacted, chain preserved.",
    ),
    PIIField(
        store=StoreKind.AUDIT_LOG,
        resource="auth",
        field="ip",
        category=PIICategory.DIRECT_IDENTIFIER,
        retention_class=RC_AUDIT_LOG,
        erasure=ErasureStrategy.REDACT,
        subject_locator="subject_id",
        description="Client IP in a security/audit entry — redacted, chain preserved.",
    ),
)

#: The default, validated Kinora data-map.
DEFAULT_DATA_MAP: DataMap = DataMap(fields=_DEFAULT_FIELDS)


def default_data_map() -> DataMap:
    """Return Kinora's default PII data-map (the Art. 30 record-of-processing)."""
    return DEFAULT_DATA_MAP


def subject_locators(
    fields: Sequence[PIIField],
) -> Mapping[tuple[StoreKind, str], str]:
    """Map ``(store, resource) -> subject_locator`` for a set of fields.

    A resource may contribute several fields; they must agree on the locator
    (you can't key the same table to the subject two different ways).
    """
    out: dict[tuple[StoreKind, str], str] = {}
    for f in fields:
        rk = (f.store, f.resource)
        existing = out.get(rk)
        if existing is not None and existing != f.subject_locator:
            raise DataMapError(
                f"resource {f.resource!r} in {f.store.value!r} has conflicting subject "
                f"locators {existing!r} vs {f.subject_locator!r}",
            )
        out[rk] = f.subject_locator
    return out


__all__ = [
    "DEFAULT_DATA_MAP",
    "DataMap",
    "PIIField",
    "RC_ACCOUNT",
    "RC_AUDIT_LOG",
    "RC_DIRECTING_PREFERENCE",
    "RC_EVENT_STREAM",
    "RC_GENERATED_MEDIA",
    "RC_READING_SESSION",
    "RC_UPLOADED_BOOK",
    "default_data_map",
    "merge_maps",
    "subject_locators",
]
