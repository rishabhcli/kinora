"""SQLAlchemy ``TypeDecorator``\\ s so ORM models adopt encryption transparently.

Declare an encrypted column exactly like a normal one::

    class User(Base):
        __tablename__ = "users"
        id: Mapped[str] = mapped_column(String(64), primary_key=True)
        # transparently encrypted at rest; reads return the plaintext str
        ssn: Mapped[str] = mapped_column(
            EncryptedType(FieldSpec(kek_id="pii"), table="users", column="ssn")
        )

On INSERT/UPDATE the bound Python value is encrypted to a single ``LargeBinary``
column; on SELECT it is decrypted back to the original type. The application
wires the :class:`~app.zerotrust.crypto.context.CryptoProvider` once
(``registry.set_provider``); the type resolves it lazily.

Associated-data binding
------------------------
AEAD binds record identity into every ciphertext. ``table`` + ``column`` are
known at class-definition time. The **primary key** is the missing piece, and a
``TypeDecorator`` does not see sibling column values during ``process_bind_param``.
Two supported modes:

* **Static AAD (default).** Bind ``(table, column, "")``. Still authenticated,
  still prevents cross-*column* relocation, and is fully transparent. This is the
  recommended default and what the type does with no extra wiring.
* **Row-bound AAD.** Pass ``aad_from=lambda obj: obj.id`` *and* register the
  :func:`bind_row_aad_listener` ORM event on the model. The listener captures the
  PK before flush and threads it through a context var the type reads, giving
  full ``(table, column, pk)`` binding — the strongest guarantee — at the cost of
  one event registration per model. Documented and provided, opt-in.

The searchable artefacts (deterministic ciphertext, blind indexes) are not stored
*inside* this column; they live in companion columns written by the application
or by :class:`SearchableEncryptedType`, which exposes them via a helper so a model
can map ``email_bidx`` / ``email_det`` alongside ``email``.
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

from sqlalchemy import LargeBinary
from sqlalchemy.types import TypeDecorator

from app.zerotrust.crypto.context import AssociatedData
from app.zerotrust.crypto.field import FieldEncryptor, FieldSpec, SearchArtifacts
from app.zerotrust.crypto.registry import get_provider

#: Per-flush row-id override (set by :func:`bind_row_aad_listener`). Keyed by the
#: ``(table, column)`` pair so concurrent encrypted columns don't collide. The
#: default is ``None`` (not an empty dict) — a mutable ContextVar default would be
#: shared across contexts; :func:`_row_aad_map` materialises a fresh dict on first
#: write instead.
_ROW_AAD: ContextVar[dict[tuple[str, str], str] | None] = ContextVar(
    "kinora_row_aad", default=None
)


def _row_aad_map() -> dict[tuple[str, str], str]:
    """Return the current row-AAD map (empty if unset)."""
    return _ROW_AAD.get() or {}


class EncryptedType(TypeDecorator[Any]):
    """A column whose Python value is AEAD-encrypted at rest and back on read.

    ``cache_ok`` is False because the type carries a non-hashable :class:`FieldSpec`
    and per-instance identity (table/column); SQLAlchemy's statement cache must
    not treat two differently-configured encrypted columns as interchangeable.
    """

    impl = LargeBinary
    cache_ok = False

    def __init__(
        self,
        spec: FieldSpec | None = None,
        *,
        table: str,
        column: str,
        aad_from: Callable[[Any], str] | None = None,
    ) -> None:
        super().__init__()
        self._spec = spec or FieldSpec()
        self._table = table
        self._column = column
        self._aad_from = aad_from

    @property
    def spec(self) -> FieldSpec:
        return self._spec

    def _aad(self) -> AssociatedData:
        record_id = _row_aad_map().get((self._table, self._column), "")
        return AssociatedData(table=self._table, column=self._column, record_id=record_id)

    def process_bind_param(self, value: Any, dialect: Any) -> bytes | None:
        if value is None:
            return None
        encryptor = FieldEncryptor(get_provider())
        blob, _artifacts = encryptor.encrypt(self._spec, value, self._aad())
        return blob

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        encryptor = FieldEncryptor(get_provider())
        return encryptor.decrypt(self._spec, bytes(value), self._aad())

    def search_artifacts(self, value: Any) -> SearchArtifacts:
        """Compute the search artefacts for ``value`` (for companion columns).

        These are derived from the *column-stable* search keys, so they are
        identical whether produced at write time or query time — which is exactly
        what makes the companion blind/deterministic columns searchable.
        """
        return FieldEncryptor(get_provider()).search_tokens(self._spec, value)


def search_tokens_for(spec: FieldSpec, value: Any) -> SearchArtifacts:
    """Compute query-probe search tokens for ``value`` under ``spec``.

    Use at query time to build the ``WHERE`` clause against the companion blind/
    deterministic columns (``... WHERE email_eq_bidx = :tok``). Mirrors exactly
    what was stored at write time.
    """
    return FieldEncryptor(get_provider()).search_tokens(spec, value)


def bind_row_aad_listener(
    mapper_class: type[Any], *, table: str, column: str, pk_attr: str
) -> None:
    """Register a ``before_insert``/``before_update`` hook for row-bound AAD.

    Captures ``getattr(obj, pk_attr)`` into the per-flush context var so the
    matching :class:`EncryptedType` binds the full ``(table, column, pk)`` AAD.
    Opt-in; only needed when you want record-level (not just column-level)
    cut-and-paste resistance and the PK is available before flush.
    """
    from sqlalchemy import event

    def _capture(_mapper: Any, _conn: Any, target: Any) -> None:
        pk = getattr(target, pk_attr, None)
        if pk is None:
            return
        current = dict(_row_aad_map())
        current[(table, column)] = str(pk)
        _ROW_AAD.set(current)

    event.listen(mapper_class, "before_insert", _capture)
    event.listen(mapper_class, "before_update", _capture)


__all__ = [
    "EncryptedType",
    "bind_row_aad_listener",
    "search_tokens_for",
]
