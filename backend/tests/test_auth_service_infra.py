"""Service-level + Redis-backed auth tests (isolated infra).

Covers the bits the HTTP integration suite doesn't reach directly: the Redis
login throttle + jti revocation store, API-key principal building, the retention
sweep, and the per-user session cap. Requires KINORA_TEST_* (skips otherwise).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.auth.lockout import LoginThrottle, RevocationStore
from app.auth.repositories import RefreshTokenRepo
from app.auth.service import LoginContext

PW = "Service-Level-Passphrase-3"


# --------------------------------------------------------------------------- #
# Redis throttle + revocation
# --------------------------------------------------------------------------- #


async def test_login_throttle_counts_and_blocks(container: Any) -> None:
    throttle = LoginThrottle(container.redis, max_attempts=3, window_s=60)
    for expected in (1, 2, 3, 4):
        count = await throttle.hit("1.2.3.4")
        assert count == expected
    assert await throttle.is_blocked("1.2.3.4")
    assert not await throttle.is_blocked("5.6.7.8")
    await throttle.reset("1.2.3.4")
    assert not await throttle.is_blocked("1.2.3.4")


async def test_revocation_store_roundtrip(container: Any) -> None:
    store = RevocationStore(container.redis)
    assert not await store.is_revoked("jti-1")
    await store.revoke("jti-1", ttl_s=60)
    assert await store.is_revoked("jti-1")


# --------------------------------------------------------------------------- #
# API-key principal carries scopes (not roles)
# --------------------------------------------------------------------------- #


async def test_api_key_principal_has_scopes_only(container: Any) -> None:
    svc = container.auth_service
    user = await svc.register("svckey@example.com", PW)
    secret, _meta = await svc.create_api_key(
        user.id, name="svc", scopes=["books:read", "metrics:read"]
    )
    principal = await svc.authenticate_api_key(secret)
    assert principal.is_api_key
    assert principal.user_id == user.id
    assert principal.has_permission("books:read")
    assert not principal.has_permission("books:write")
    assert principal.roles == frozenset()  # API keys don't carry roles


async def test_disabled_user_api_key_rejected(container: Any) -> None:
    from app.auth.errors import AccountDisabled

    svc = container.auth_service
    user = await svc.register("svcdisabled@example.com", PW)
    secret, _ = await svc.create_api_key(user.id, name="svc", scopes=["books:read"])
    await svc.set_account_disabled(user.id, True)
    with pytest.raises(AccountDisabled):
        await svc.authenticate_api_key(secret)


# --------------------------------------------------------------------------- #
# Session cap eviction
# --------------------------------------------------------------------------- #


async def test_session_cap_evicts_oldest(container: Any) -> None:
    # Shrink the cap so the test is fast.
    container.settings.max_sessions_per_user = 2
    svc = container.auth_service
    user = await svc.register("capped@example.com", PW)
    b1 = await svc.login("capped@example.com", PW)
    await svc.login("capped@example.com", PW)
    # The third login should evict the oldest (b1)'s session.
    await svc.login("capped@example.com", PW)
    sessions = await svc.list_sessions(user.id)
    assert len(sessions) == 2
    # b1's refresh token no longer works (its session was evicted).
    from app.auth.errors import TokenInvalid, TokenReused

    with pytest.raises((TokenInvalid, TokenReused)):
        await svc.refresh(b1.refresh_token)


# --------------------------------------------------------------------------- #
# Retention sweep
# --------------------------------------------------------------------------- #


async def test_retention_sweep_purges_expired_refresh(container: Any) -> None:
    svc = container.auth_service
    user = await svc.register("sweep@example.com", PW)
    await svc.login("sweep@example.com", PW)
    # Backdate a refresh token to the past so the sweep removes it.
    async with container.session_factory() as db:
        repo = RefreshTokenRepo(db)
        await repo.create(
            user_id=user.id,
            token_digest="deadbeef" * 8,
            family_id="oldfam",
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
    result = await svc.run_retention_sweep()
    assert result["refresh_tokens"] >= 1


# --------------------------------------------------------------------------- #
# Audit log content
# --------------------------------------------------------------------------- #


async def test_audit_log_records_login_failure(container: Any) -> None:
    from app.auth.errors import InvalidCredentials

    svc = container.auth_service
    user = await svc.register("auditfail@example.com", PW)
    with pytest.raises(InvalidCredentials):
        await svc.login("auditfail@example.com", "wrong", ctx=LoginContext(ip="9.9.9.9"))
    events = await svc.read_audit_log(user.id, limit=20)
    kinds = {e["event"] for e in events}
    assert "login_failure" in kinds
    assert "register" in kinds
