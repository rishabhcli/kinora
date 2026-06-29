"""Static KV secrets (sealed) + dynamic-secret lease lifecycle tests."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.zerotrust.identity import (
    DynamicSecretEngine,
    DynamicSecretRole,
    GeneratedSecret,
    LeaseExpiredError,
    LeaseRevokedError,
    LocalKms,
    ManualClock,
    SecretNotFoundError,
    SecretStore,
)
from app.zerotrust.identity.errors import LeaseError
from tests.zerotrust.conftest import FIXED_KEK


def _store(clock: ManualClock) -> SecretStore:
    kms = LocalKms(clock=clock)
    kms.create_key("kek", material=FIXED_KEK)
    return SecretStore(kms=kms, key_id="kek", clock=clock)


# --------------------------------------------------------------------------- #
# Static KV
# --------------------------------------------------------------------------- #


def test_put_get_map(clock: ManualClock) -> None:
    store = _store(clock)
    store.put("providers/dashscope", {"api_key": "sk-abc"})
    assert store.get_map("providers/dashscope")["api_key"] == "sk-abc"


def test_versioning(clock: ManualClock) -> None:
    store = _store(clock)
    assert store.put("p", {"v": "1"}) == 1
    assert store.put("p", {"v": "2"}) == 2
    assert store.get_map("p")["v"] == "2"
    assert store.get_map("p", version=1)["v"] == "1"
    assert store.list_versions("p") == (1, 2)
    assert store.latest_version("p") == 2


def test_sealed_at_rest(clock: ManualClock) -> None:
    """The store must not hold plaintext — only KMS-wrapped blobs."""

    store = _store(clock)
    store.put("p", b"super-secret-bytes")
    # the wrapped blob does not contain the plaintext
    wrapped = store._version("p", None).wrapped
    assert b"super-secret-bytes" not in wrapped.ciphertext
    assert store.get("p") == b"super-secret-bytes"


def test_destroy_version(clock: ManualClock) -> None:
    store = _store(clock)
    store.put("p", {"v": "1"})
    store.put("p", {"v": "2"})
    store.destroy_version("p", 1)
    with pytest.raises(SecretNotFoundError):
        store.get("p", version=1)
    assert store.get_map("p")["v"] == "2"  # latest still readable


def test_missing_secret(clock: ManualClock) -> None:
    store = _store(clock)
    with pytest.raises(SecretNotFoundError):
        store.get("nope")


def test_rewrap_all_after_rotation(clock: ManualClock) -> None:
    kms = LocalKms(clock=clock)
    kms.create_key("kek", material=FIXED_KEK)
    store = SecretStore(kms=kms, key_id="kek", clock=clock)
    store.put("p", {"v": "1"})
    store.put("q", b"bytes")
    kms.rotate_key("kek")
    count = store.rewrap_all()
    assert count == 2
    # everything still readable post-rewrap, now under v2
    assert store.get_map("p")["v"] == "1"
    assert store.get("q") == b"bytes"


def test_delete(clock: ManualClock) -> None:
    store = _store(clock)
    store.put("p", {"v": "1"})
    store.delete("p")
    with pytest.raises(SecretNotFoundError):
        store.get("p")


# --------------------------------------------------------------------------- #
# Dynamic secrets + leases
# --------------------------------------------------------------------------- #


def _engine(clock: ManualClock, revoked: list[str]) -> DynamicSecretEngine:
    eng = DynamicSecretEngine(clock=clock)
    counter = {"n": 0}

    def gen() -> GeneratedSecret:
        counter["n"] += 1
        return GeneratedSecret({"user": f"v-{counter['n']}"}, handle=f"v-{counter['n']}")

    eng.register_role(
        DynamicSecretRole(
            name="db-readonly",
            generate=gen,
            revoke=lambda s: revoked.append(s.handle),
            default_ttl=timedelta(minutes=30),
            max_ttl=timedelta(hours=2),
        )
    )
    return eng


def test_issue_lease(clock: ManualClock) -> None:
    revoked: list[str] = []
    eng = _engine(clock, revoked)
    lease = eng.issue("db-readonly")
    assert eng.get_secret(lease.lease_id)["user"] == "v-1"
    assert lease.is_active(clock.now())
    assert lease.remaining(clock.now()) == timedelta(minutes=30)


def test_renew_extends_capped_at_max(clock: ManualClock) -> None:
    revoked: list[str] = []
    eng = _engine(clock, revoked)
    lease = eng.issue("db-readonly")  # expires +30m, max +2h
    clock.advance(minutes=20)
    eng.renew(lease.lease_id)  # now+30 = +50m
    assert lease.remaining(clock.now()) == timedelta(minutes=30)
    # keep renewing past the max-TTL ceiling
    for _ in range(10):
        clock.advance(minutes=20)
        try:
            eng.renew(lease.lease_id)
        except LeaseExpiredError:
            break
    # never extends beyond max_expires_at
    assert lease.expires_at <= lease.max_expires_at


def test_expired_lease_cannot_renew(clock: ManualClock) -> None:
    revoked: list[str] = []
    eng = _engine(clock, revoked)
    lease = eng.issue("db-readonly")
    clock.advance(hours=1)  # past the 30m TTL
    with pytest.raises(LeaseExpiredError):
        eng.renew(lease.lease_id)


def test_revoke_runs_hook(clock: ManualClock) -> None:
    revoked: list[str] = []
    eng = _engine(clock, revoked)
    lease = eng.issue("db-readonly")
    eng.revoke(lease.lease_id)
    assert revoked == ["v-1"]
    with pytest.raises(LeaseRevokedError):
        eng.get_secret(lease.lease_id)
    # idempotent
    eng.revoke(lease.lease_id)
    assert revoked == ["v-1"]


def test_sweep_expired_revokes(clock: ManualClock) -> None:
    revoked: list[str] = []
    eng = _engine(clock, revoked)
    eng.issue("db-readonly")
    eng.issue("db-readonly")
    clock.advance(hours=1)
    swept = eng.sweep_expired()
    assert swept == 2
    assert sorted(revoked) == ["v-1", "v-2"]


def test_revoke_all(clock: ManualClock) -> None:
    revoked: list[str] = []
    eng = _engine(clock, revoked)
    eng.issue("db-readonly")
    eng.issue("db-readonly")
    assert eng.revoke_all() == 2
    assert eng.revoke_all() == 0  # nothing left active


def test_active_leases(clock: ManualClock) -> None:
    revoked: list[str] = []
    eng = _engine(clock, revoked)
    eng.issue("db-readonly")
    assert len(eng.active_leases()) == 1
    clock.advance(hours=1)
    assert len(eng.active_leases()) == 0


def test_unknown_role(clock: ManualClock) -> None:
    eng = DynamicSecretEngine(clock=clock)
    with pytest.raises(LeaseError):
        eng.issue("nope")


def test_get_secret_expired(clock: ManualClock) -> None:
    revoked: list[str] = []
    eng = _engine(clock, revoked)
    lease = eng.issue("db-readonly")
    clock.advance(hours=1)
    with pytest.raises(LeaseExpiredError):
        eng.get_secret(lease.lease_id)


def test_ttl_capped_at_max_on_issue(clock: ManualClock) -> None:
    revoked: list[str] = []
    eng = _engine(clock, revoked)
    lease = eng.issue("db-readonly", ttl=timedelta(hours=10))
    assert lease.remaining(clock.now()) == timedelta(hours=2)  # capped at max_ttl
