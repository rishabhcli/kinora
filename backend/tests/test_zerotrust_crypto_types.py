"""SQLAlchemy ``EncryptedType`` transparency over real SQLite (no external infra).

Uses an in-memory SQLite engine (stdlib ``sqlite3`` via SQLAlchemy) so the type
decorator is exercised through actual bind/result processing — the value is
encrypted on INSERT and decrypted on SELECT, and the on-disk column never holds
plaintext. Also covers row-bound AAD via the opt-in event listener and the
process-wide provider registry guard.
"""

from __future__ import annotations

import pytest
from sqlalchemy import String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.zerotrust.crypto import registry
from app.zerotrust.crypto.context import CryptoProvider
from app.zerotrust.crypto.errors import CryptoConfigError
from app.zerotrust.crypto.field import FieldSpec
from app.zerotrust.crypto.kms import SoftwareKMS
from app.zerotrust.crypto.types import EncryptedType, bind_row_aad_listener

ROOT = bytes([0x6D]) * 32


class _Base(DeclarativeBase):
    pass


class _Account(_Base):
    __tablename__ = "t_account"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ssn: Mapped[str] = mapped_column(
        EncryptedType(FieldSpec(kek_id="pii"), table="t_account", column="ssn")
    )


def _provider() -> CryptoProvider:
    kms = SoftwareKMS(ROOT)
    kms.register_kek("pii")
    return CryptoProvider(kms, kek_id="pii")


@pytest.fixture
def engine_and_provider():  # type: ignore[no-untyped-def]
    provider = _provider()
    engine = create_engine("sqlite://")  # in-memory, stdlib sqlite3
    _Base.metadata.create_all(engine)
    with registry.use_provider(provider):
        yield engine, provider


def test_encrypted_column_round_trips(engine_and_provider) -> None:  # type: ignore[no-untyped-def]
    engine, _ = engine_and_provider
    with Session(engine) as s:
        s.add(_Account(id="a1", ssn="123-45-6789"))
        s.commit()
    with Session(engine) as s:
        row = s.get(_Account, "a1")
        assert row is not None
        assert row.ssn == "123-45-6789"


def test_column_holds_ciphertext_not_plaintext(engine_and_provider) -> None:  # type: ignore[no-untyped-def]
    engine, _ = engine_and_provider
    with Session(engine) as s:
        s.add(_Account(id="a2", ssn="987-65-4321"))
        s.commit()
    # Read the raw stored bytes bypassing the type decorator.
    with engine.connect() as conn:
        raw = conn.exec_driver_sql(
            "SELECT ssn FROM t_account WHERE id = 'a2'"
        ).scalar_one()
    assert b"987-65-4321" not in (raw if isinstance(raw, bytes) else raw.encode())


def test_null_passes_through(engine_and_provider) -> None:  # type: ignore[no-untyped-def]
    engine, _ = engine_and_provider

    class _Nullable(_Base):
        __tablename__ = "t_nullable"
        id: Mapped[str] = mapped_column(String(64), primary_key=True)
        note: Mapped[str | None] = mapped_column(
            EncryptedType(FieldSpec(kek_id="pii"), table="t_nullable", column="note"),
            nullable=True,
        )

    _Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(_Nullable(id="n1", note=None))
        s.commit()
    with Session(engine) as s:
        assert s.get(_Nullable, "n1").note is None  # type: ignore[union-attr]


def test_missing_provider_raises() -> None:
    # Outside a use_provider() block, the registry must refuse (never store plaintext).
    spec = FieldSpec(kek_id="pii")
    etype = EncryptedType(spec, table="t", column="c")
    # Ensure no provider is set in this context.
    with pytest.raises(CryptoConfigError):
        etype.process_bind_param("value", dialect=None)


def test_search_artifacts_match_query_probe(engine_and_provider) -> None:  # type: ignore[no-untyped-def]
    _engine, _provider_ = engine_and_provider
    spec = FieldSpec(kek_id="pii", searchable_equality=True, blind_equality=True)
    etype = EncryptedType(spec, table="t_account", column="email")
    stored = etype.search_artifacts("Alice@Example.com")
    probe = etype.search_artifacts("Alice@Example.com")
    assert stored.deterministic == probe.deterministic
    assert stored.equality_index == probe.equality_index


def test_row_bound_aad_listener_binds_pk(engine_and_provider) -> None:  # type: ignore[no-untyped-def]
    engine, _ = engine_and_provider

    class _Rowbound(_Base):
        __tablename__ = "t_rowbound"
        id: Mapped[str] = mapped_column(String(64), primary_key=True)
        secret: Mapped[str] = mapped_column(
            EncryptedType(
                FieldSpec(kek_id="pii"),
                table="t_rowbound",
                column="secret",
                aad_from=lambda obj: obj.id,
            )
        )

    bind_row_aad_listener(_Rowbound, table="t_rowbound", column="secret", pk_attr="id")
    _Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(_Rowbound(id="rb1", secret="top-secret"))
        s.commit()
    with Session(engine) as s:
        assert s.get(_Rowbound, "rb1").secret == "top-secret"  # type: ignore[union-attr]
