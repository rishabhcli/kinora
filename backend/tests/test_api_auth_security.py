"""Integration tests for the production auth & security surface (§6, §12).

Exercises the full flow against the isolated throwaway infra: register → login
(access+refresh) → whoami/RBAC → refresh rotation + reuse detection → sessions →
API keys + scope enforcement → MFA enrol/login/disable → password change →
logout/logout-all → audit log. Requires KINORA_TEST_DATABASE_URL/_REDIS_URL/
_S3_ENDPOINT_URL (skips cleanly otherwise).
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from app.core import security as crypto

STRONG_PW = "Sufficiently-Long-Passphrase-9"


async def _register(client: AsyncClient, email: str, pw: str = STRONG_PW) -> dict[str, Any]:
    r = await client.post("/api/auth/register", json={"email": email, "password": pw})
    assert r.status_code == 201, r.text
    return r.json()


async def _login(client: AsyncClient, email: str, pw: str = STRONG_PW) -> dict[str, Any]:
    r = await client.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------- #
# Login returns access + refresh, whoami carries roles/scopes
# --------------------------------------------------------------------------- #


async def test_login_returns_access_and_refresh(api_client: AsyncClient) -> None:
    await _register(api_client, "alice@example.com")
    body = await _login(api_client, "alice@example.com")
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["session_id"]
    assert body["token_type"] == "bearer"
    assert body["mfa_required"] is False


async def test_whoami_has_default_reader_role(api_client: AsyncClient) -> None:
    await _register(api_client, "bob@example.com")
    tokens = await _login(api_client, "bob@example.com")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    who = await api_client.get("/api/auth/whoami", headers=headers)
    assert who.status_code == 200, who.text
    data = who.json()
    assert "reader" in data["roles"]
    assert "books:read" in data["permissions"]
    assert data["is_api_key"] is False
    assert data["session_id"]


# --------------------------------------------------------------------------- #
# Refresh rotation + reuse detection
# --------------------------------------------------------------------------- #


async def test_refresh_rotates_token(api_client: AsyncClient) -> None:
    await _register(api_client, "carol@example.com")
    tokens = await _login(api_client, "carol@example.com")
    first_refresh = tokens["refresh_token"]
    r = await api_client.post("/api/auth/refresh", json={"refresh_token": first_refresh})
    assert r.status_code == 200, r.text
    rotated = r.json()
    assert rotated["refresh_token"] != first_refresh
    assert rotated["access_token"]


async def test_refresh_reuse_revokes_family(api_client: AsyncClient) -> None:
    """Replaying a consumed refresh token revokes the whole family (breach signal)."""
    await _register(api_client, "dave@example.com")
    tokens = await _login(api_client, "dave@example.com")
    first = tokens["refresh_token"]
    # Use it once (rotates to a child).
    r1 = await api_client.post("/api/auth/refresh", json={"refresh_token": first})
    assert r1.status_code == 200
    child = r1.json()["refresh_token"]
    # Replay the now-consumed parent → reuse detected, 401.
    r2 = await api_client.post("/api/auth/refresh", json={"refresh_token": first})
    assert r2.status_code == 401
    assert r2.json()["error"]["type"] == "token_reuse_detected"
    # The child is now revoked too (family burned).
    r3 = await api_client.post("/api/auth/refresh", json={"refresh_token": child})
    assert r3.status_code == 401


async def test_refresh_rejects_unknown_token(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/auth/refresh", json={"refresh_token": "nope-not-a-token"})
    assert r.status_code == 401
    assert r.json()["error"]["type"] == "token_invalid"


# --------------------------------------------------------------------------- #
# Sessions + logout
# --------------------------------------------------------------------------- #


async def test_sessions_list_and_revoke(api_client: AsyncClient) -> None:
    await _register(api_client, "erin@example.com")
    t1 = await _login(api_client, "erin@example.com")
    t2 = await _login(api_client, "erin@example.com")
    headers = {"Authorization": f"Bearer {t2['access_token']}"}
    listing = await api_client.get("/api/auth/sessions", headers=headers)
    assert listing.status_code == 200
    sessions = listing.json()["sessions"]
    assert len(sessions) >= 2
    # Revoke the first session; its refresh token must then fail.
    target = t1["session_id"]
    rev = await api_client.delete(f"/api/auth/sessions/{target}", headers=headers)
    assert rev.status_code == 204
    bad = await api_client.post("/api/auth/refresh", json={"refresh_token": t1["refresh_token"]})
    assert bad.status_code == 401


async def test_logout_revokes_current_session(api_client: AsyncClient) -> None:
    await _register(api_client, "frank@example.com")
    tokens = await _login(api_client, "frank@example.com")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    out = await api_client.post("/api/auth/logout", headers=headers)
    assert out.status_code == 204
    # The refresh token tied to that session is dead.
    bad = await api_client.post(
        "/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert bad.status_code == 401
    # The access token is also rejected: its session was revoked (the principal
    # builder fails the session-active check even before the token expires).
    who = await api_client.get("/api/auth/whoami", headers=headers)
    assert who.status_code == 401


async def test_logout_all(api_client: AsyncClient) -> None:
    await _register(api_client, "grace@example.com")
    await _login(api_client, "grace@example.com")
    tokens = await _login(api_client, "grace@example.com")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    out = await api_client.post("/api/auth/logout-all", headers=headers)
    assert out.status_code == 200
    assert out.json()["revoked_sessions"] >= 2


# --------------------------------------------------------------------------- #
# Password change
# --------------------------------------------------------------------------- #


async def test_change_password_then_login_with_new(api_client: AsyncClient) -> None:
    await _register(api_client, "heidi@example.com")
    tokens = await _login(api_client, "heidi@example.com")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    new_pw = "An-Even-Better-Passphrase-42"
    chg = await api_client.post(
        "/api/auth/password/change",
        headers=headers,
        json={"current_password": STRONG_PW, "new_password": new_pw},
    )
    assert chg.status_code == 204, chg.text
    # Old refresh token is revoked by the change.
    bad = await api_client.post(
        "/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert bad.status_code == 401
    # New password works; old one doesn't.
    await _login(api_client, "heidi@example.com", new_pw)
    wrong = await api_client.post(
        "/api/auth/login", json={"email": "heidi@example.com", "password": STRONG_PW}
    )
    assert wrong.status_code == 401


async def test_change_password_wrong_current(api_client: AsyncClient) -> None:
    await _register(api_client, "ivan@example.com")
    tokens = await _login(api_client, "ivan@example.com")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    chg = await api_client.post(
        "/api/auth/password/change",
        headers=headers,
        json={"current_password": "wrong-one", "new_password": "Another-Strong-One-7"},
    )
    assert chg.status_code == 401


async def test_password_reset_flow(api_client: AsyncClient) -> None:
    await _register(api_client, "judy@example.com")
    req = await api_client.post(
        "/api/auth/password/reset-request", json={"email": "judy@example.com"}
    )
    assert req.status_code == 202
    token = req.json().get("reset_token")  # returned only in local env
    assert token
    new_pw = "Reset-To-This-Strong-One-5"
    reset = await api_client.post(
        "/api/auth/password/reset", json={"reset_token": token, "new_password": new_pw}
    )
    assert reset.status_code == 204
    await _login(api_client, "judy@example.com", new_pw)


async def test_password_reset_request_unknown_email_is_202(api_client: AsyncClient) -> None:
    req = await api_client.post(
        "/api/auth/password/reset-request", json={"email": "nobody@example.com"}
    )
    assert req.status_code == 202
    assert "reset_token" not in req.json()  # no enumeration


# --------------------------------------------------------------------------- #
# MFA
# --------------------------------------------------------------------------- #


async def test_mfa_enroll_confirm_login_disable(api_client: AsyncClient) -> None:
    await _register(api_client, "mallory@example.com")
    tokens = await _login(api_client, "mallory@example.com")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    enroll = await api_client.post("/api/auth/mfa/enroll", headers=headers)
    assert enroll.status_code == 200, enroll.text
    secret = enroll.json()["secret"]
    recovery = enroll.json()["recovery_codes"]
    assert len(recovery) == 10
    assert enroll.json()["provisioning_uri"].startswith("otpauth://")

    # Confirm with a valid TOTP code.
    confirm = await api_client.post(
        "/api/auth/mfa/confirm", headers=headers, json={"code": crypto.totp_now(secret)}
    )
    assert confirm.status_code == 204, confirm.text

    # Now login is two-step: password returns an mfa challenge.
    step1 = await api_client.post(
        "/api/auth/login", json={"email": "mallory@example.com", "password": STRONG_PW}
    )
    assert step1.status_code == 200
    assert step1.json()["mfa_required"] is True
    mfa_token = step1.json()["mfa_token"]

    # Complete with a TOTP code.
    step2 = await api_client.post(
        "/api/auth/mfa/login", json={"mfa_token": mfa_token, "code": crypto.totp_now(secret)}
    )
    assert step2.status_code == 200, step2.text
    assert step2.json()["access_token"]

    # Disable MFA with a current code.
    disable = await api_client.post(
        "/api/auth/mfa/disable", headers=headers, json={"code": crypto.totp_now(secret)}
    )
    assert disable.status_code == 204, disable.text
    # Login is single-step again.
    again = await _login(api_client, "mallory@example.com")
    assert again["access_token"]


async def test_mfa_login_with_recovery_code(api_client: AsyncClient) -> None:
    await _register(api_client, "niaj@example.com")
    tokens = await _login(api_client, "niaj@example.com")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    enroll = (await api_client.post("/api/auth/mfa/enroll", headers=headers)).json()
    secret, recovery = enroll["secret"], enroll["recovery_codes"]
    await api_client.post(
        "/api/auth/mfa/confirm", headers=headers, json={"code": crypto.totp_now(secret)}
    )
    step1 = await api_client.post(
        "/api/auth/login", json={"email": "niaj@example.com", "password": STRONG_PW}
    )
    mfa_token = step1.json()["mfa_token"]
    # Use a recovery code instead of TOTP.
    step2 = await api_client.post(
        "/api/auth/mfa/login", json={"mfa_token": mfa_token, "code": recovery[0]}
    )
    assert step2.status_code == 200, step2.text
    # The same recovery code is single-use: a second login with it must fail.
    step1b = await api_client.post(
        "/api/auth/login", json={"email": "niaj@example.com", "password": STRONG_PW}
    )
    step2b = await api_client.post(
        "/api/auth/mfa/login",
        json={"mfa_token": step1b.json()["mfa_token"], "code": recovery[0]},
    )
    assert step2b.status_code == 401


async def test_mfa_confirm_rejects_bad_code(api_client: AsyncClient) -> None:
    await _register(api_client, "olivia@example.com")
    tokens = await _login(api_client, "olivia@example.com")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    await api_client.post("/api/auth/mfa/enroll", headers=headers)
    bad = await api_client.post("/api/auth/mfa/confirm", headers=headers, json={"code": "000000"})
    assert bad.status_code == 401
    assert bad.json()["error"]["type"] == "mfa_invalid"


# --------------------------------------------------------------------------- #
# API keys + scope enforcement
# --------------------------------------------------------------------------- #


async def test_api_key_create_use_revoke(api_client: AsyncClient) -> None:
    await _register(api_client, "peggy@example.com")
    tokens = await _login(api_client, "peggy@example.com")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    created = await api_client.post(
        "/api/auth/api-keys",
        headers=headers,
        json={"name": "ci-key", "scopes": ["books:read", "library:read"]},
    )
    assert created.status_code == 201, created.text
    secret = created.json()["secret"]
    assert secret.startswith("kino_sk_")
    meta = created.json()["api_key"]
    assert set(meta["scopes"]) == {"books:read", "library:read"}

    # The key authenticates whoami with its scopes (no roles).
    who = await api_client.get("/api/auth/whoami", headers={"X-API-Key": secret})
    assert who.status_code == 200, who.text
    assert who.json()["is_api_key"] is True
    assert "books:read" in who.json()["permissions"]

    # List shows the key (no secret).
    listing = await api_client.get("/api/auth/api-keys", headers=headers)
    assert listing.status_code == 200
    assert any(k["id"] == meta["id"] for k in listing.json()["api_keys"])

    # Revoke; the key no longer authenticates.
    rev = await api_client.delete(f"/api/auth/api-keys/{meta['id']}", headers=headers)
    assert rev.status_code == 204
    dead = await api_client.get("/api/auth/whoami", headers={"X-API-Key": secret})
    assert dead.status_code == 401


async def test_api_key_scopes_capped_to_owner_permissions(api_client: AsyncClient) -> None:
    """A reader cannot mint a key with admin scope it doesn't itself hold."""
    await _register(api_client, "quentin@example.com")
    tokens = await _login(api_client, "quentin@example.com")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    created = await api_client.post(
        "/api/auth/api-keys",
        headers=headers,
        json={"name": "overreach", "scopes": ["books:read", "admin:rbac"]},
    )
    assert created.status_code == 201
    granted = created.json()["api_key"]["scopes"]
    assert "books:read" in granted
    assert "admin:rbac" not in granted  # dropped — owner lacks it


async def test_bad_api_key_rejected(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/auth/whoami", headers={"X-API-Key": "kino_sk_deadbeef_xyz"})
    assert r.status_code == 401
    assert r.json()["error"]["type"] == "api_key_invalid"


# --------------------------------------------------------------------------- #
# RBAC admin gating
# --------------------------------------------------------------------------- #


async def test_rbac_admin_requires_admin(api_client: AsyncClient) -> None:
    await _register(api_client, "reader1@example.com")
    tokens = await _login(api_client, "reader1@example.com")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    # A plain reader cannot read the catalogue or grant roles.
    cat = await api_client.get("/api/auth/rbac/catalogue", headers=headers)
    assert cat.status_code == 403
    grant = await api_client.post(
        "/api/auth/rbac/grant",
        headers=headers,
        json={"user_id": "someone", "role": "editor"},
    )
    assert grant.status_code == 403


async def test_admin_can_grant_role(api_client: AsyncClient, container: Any) -> None:
    """An admin (granted directly in the DB) can grant a role to another user."""
    admin = await _register(api_client, "root@example.com")
    target = await _register(api_client, "promote-me@example.com")
    # Grant the admin role directly via the service (bootstrap the first admin).
    await container.auth_service.grant_role(user_id=admin["id"], role="admin")
    tokens = await _login(api_client, "root@example.com")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    cat = await api_client.get("/api/auth/rbac/catalogue", headers=headers)
    assert cat.status_code == 200
    grant = await api_client.post(
        "/api/auth/rbac/grant",
        headers=headers,
        json={"user_id": target["id"], "role": "editor"},
    )
    assert grant.status_code == 204, grant.text
    # The promoted user now has editor permissions.
    t2 = await _login(api_client, "promote-me@example.com")
    who = await api_client.get(
        "/api/auth/whoami", headers={"Authorization": f"Bearer {t2['access_token']}"}
    )
    assert "editor" in who.json()["roles"]
    assert "books:write" in who.json()["permissions"]


async def test_admin_disable_account_blocks_login(api_client: AsyncClient, container: Any) -> None:
    admin = await _register(api_client, "root2@example.com")
    victim = await _register(api_client, "victim@example.com")
    await container.auth_service.grant_role(user_id=admin["id"], role="admin")
    tokens = await _login(api_client, "root2@example.com")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    disable = await api_client.post(
        "/api/auth/admin/account-disabled",
        headers=headers,
        json={"user_id": victim["id"], "disabled": True},
    )
    assert disable.status_code == 204, disable.text
    blocked = await api_client.post(
        "/api/auth/login", json={"email": "victim@example.com", "password": STRONG_PW}
    )
    assert blocked.status_code == 403
    assert blocked.json()["error"]["type"] == "account_disabled"


# --------------------------------------------------------------------------- #
# Audit log
# --------------------------------------------------------------------------- #


async def test_audit_log_records_events(api_client: AsyncClient) -> None:
    await _register(api_client, "sam@example.com")
    tokens = await _login(api_client, "sam@example.com")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    audit = await api_client.get("/api/auth/audit", headers=headers)
    assert audit.status_code == 200
    events = {e["event"] for e in audit.json()["events"]}
    assert "register" in events
    assert "login_success" in events


# --------------------------------------------------------------------------- #
# Account lockout (per-account, durable)
# --------------------------------------------------------------------------- #


async def test_account_locks_after_repeated_failures(
    api_client: AsyncClient, container: Any
) -> None:
    """5 bad passwords lock the account; the typed error is account_locked."""
    await _register(api_client, "trent@example.com")
    # Drive failures directly through the service to avoid the route rate-limiter.
    from app.auth.errors import AccountLocked, InvalidCredentials

    svc = container.auth_service
    for _ in range(container.settings.login_max_failures):
        with pytest.raises(InvalidCredentials):
            await svc.login("trent@example.com", "wrong-password")
    # The next attempt — even with the RIGHT password — is locked out.
    with pytest.raises(AccountLocked):
        await svc.login("trent@example.com", STRONG_PW)
