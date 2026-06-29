"""Online, batched key-rotation correctness (fixed keys, no infra).

These are the rotation tests the brief calls for: with a fixed root key, assert
that re-wrap rolls DEKs to the new KEK version while preserving the plaintext and
the bulk ciphertext byte-for-byte; that full re-encryption changes the DEK yet
still decrypts; that the compare-and-set contract makes rotation safe against a
concurrent writer (lost-update-free, idempotent); and that batching is correct.
"""

from __future__ import annotations

from app.zerotrust.crypto.context import AssociatedData, CryptoProvider
from app.zerotrust.crypto.field import FieldEncryptor, FieldSpec, StoredField
from app.zerotrust.crypto.kms import SoftwareKMS
from app.zerotrust.crypto.rotation import RotationOutcome, Rotator, RowRef, batched

ROOT = bytes([0x3E]) * 32


def _setup() -> tuple[SoftwareKMS, CryptoProvider, FieldEncryptor]:
    kms = SoftwareKMS(ROOT)
    kms.register_kek("pii")
    provider = CryptoProvider(kms, kek_id="pii")
    return kms, provider, FieldEncryptor(provider)


class _Store:
    """A tiny in-memory cell store implementing the compare-and-set contract."""

    def __init__(self) -> None:
        self.cells: dict[str, bytes] = {}
        self.conflicts: set[str] = set()  # row ids that a concurrent writer wins

    def cas(self, ref: RowRef, new_payload: bytes) -> bool:
        if ref.row_id in self.conflicts:
            return False  # a concurrent writer changed it; rotator must skip
        if self.cells.get(ref.row_id) != ref.payload:
            return False
        self.cells[ref.row_id] = new_payload
        return True

    def rows(self, table: str, column: str) -> list[RowRef]:
        return [
            RowRef(row_id=rid, payload=payload, table=table, column=column)
            for rid, payload in self.cells.items()
        ]


def _seed(store: _Store, fe: FieldEncryptor, n: int) -> None:
    for i in range(n):
        rid = f"r{i}"
        blob, _ = fe.encrypt(FieldSpec(), f"value-{i}", AssociatedData("users", "ssn", rid))
        store.cells[rid] = blob


def test_rewrap_rolls_dek_version_preserving_plaintext_and_ciphertext() -> None:
    kms, provider, fe = _setup()
    store = _Store()
    _seed(store, fe, 5)
    # Capture the envelopes (bulk ciphertext) before rotation.
    before_envelopes = {
        rid: StoredField.from_bytes(b).ciphertext.envelope for rid, b in store.cells.items()
    }

    kms.rotate_kek("pii")
    rotator = Rotator(provider=provider, compare_and_set=store.cas)
    outcome = rotator.rewrap_batch(store.rows("users", "ssn"))

    assert outcome == RotationOutcome(scanned=5, rotated=5, skipped_current=0, skipped_conflict=0)
    for rid, blob in store.cells.items():
        stored = StoredField.from_bytes(blob)
        # DEK rolled to v2 ...
        assert stored.ciphertext.wrapped_dek.kek_version == 2
        # ... but the bulk ciphertext is untouched (cheap rotation).
        assert stored.ciphertext.envelope == before_envelopes[rid]
        # ... and it still decrypts.
        decrypted = fe.decrypt(FieldSpec(), blob, AssociatedData("users", "ssn", rid))
        assert decrypted == f"value-{rid[1:]}"


def test_rewrap_skips_rows_already_on_active_version() -> None:
    kms, provider, fe = _setup()
    store = _Store()
    _seed(store, fe, 3)  # already on v1 (the active version)
    rotator = Rotator(provider=provider, compare_and_set=store.cas)
    outcome = rotator.rewrap_batch(store.rows("users", "ssn"))
    assert outcome.skipped_current == 3
    assert outcome.rotated == 0


def test_rewrap_is_idempotent() -> None:
    kms, provider, fe = _setup()
    store = _Store()
    _seed(store, fe, 4)
    kms.rotate_kek("pii")
    rotator = Rotator(provider=provider, compare_and_set=store.cas)
    first = rotator.rewrap_batch(store.rows("users", "ssn"))
    second = rotator.rewrap_batch(store.rows("users", "ssn"))
    assert first.rotated == 4
    assert second.rotated == 0  # second pass is a no-op
    assert second.skipped_current == 4


def test_rewrap_respects_concurrent_writer_via_cas() -> None:
    kms, provider, fe = _setup()
    store = _Store()
    _seed(store, fe, 4)
    store.conflicts.add("r2")  # a concurrent writer owns r2
    kms.rotate_kek("pii")
    rotator = Rotator(provider=provider, compare_and_set=store.cas)
    outcome = rotator.rewrap_batch(store.rows("users", "ssn"))
    assert outcome.rotated == 3
    assert outcome.skipped_conflict == 1
    # r2 keeps its old (v1) wrapping — a later pass would pick it up.
    assert StoredField.from_bytes(store.cells["r2"]).ciphertext.wrapped_dek.kek_version == 1


def test_reencrypt_changes_dek_but_round_trips() -> None:
    _kms, provider, fe = _setup()
    store = _Store()
    spec = FieldSpec()
    blob, _ = fe.encrypt(spec, "rotate-me", AssociatedData("users", "ssn", "r0"))
    store.cells["r0"] = blob
    old_wrapped = StoredField.from_bytes(blob).ciphertext.wrapped_dek.ciphertext

    rotator = Rotator(provider=provider, compare_and_set=store.cas)
    outcome = rotator.reencrypt_batch(spec, store.rows("users", "ssn"))

    assert outcome.rotated == 1
    new_blob = store.cells["r0"]
    new_wrapped = StoredField.from_bytes(new_blob).ciphertext.wrapped_dek.ciphertext
    assert new_wrapped != old_wrapped  # fresh DEK
    assert fe.decrypt(spec, new_blob, AssociatedData("users", "ssn", "r0")) == "rotate-me"


def test_reencrypt_respects_cas_conflict() -> None:
    _kms, provider, fe = _setup()
    store = _Store()
    spec = FieldSpec()
    blob, _ = fe.encrypt(spec, "x", AssociatedData("users", "ssn", "r0"))
    store.cells["r0"] = blob
    store.conflicts.add("r0")
    rotator = Rotator(provider=provider, compare_and_set=store.cas)
    outcome = rotator.reencrypt_batch(spec, store.rows("users", "ssn"))
    assert outcome.rotated == 0
    assert outcome.skipped_conflict == 1


def test_outcome_addition() -> None:
    a = RotationOutcome(1, 1, 0, 0)
    b = RotationOutcome(2, 1, 1, 0)
    assert a + b == RotationOutcome(3, 2, 1, 0)


def test_batched_yields_correct_sizes() -> None:
    rows = [RowRef(row_id=str(i), payload=b"", table="t", column="c") for i in range(7)]
    batches = list(batched(rows, 3))
    assert [len(b) for b in batches] == [3, 3, 1]


def test_batched_rejects_nonpositive_size() -> None:
    import pytest

    with pytest.raises(ValueError):
        list(batched([], 0))
