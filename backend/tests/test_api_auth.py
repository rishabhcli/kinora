"""Auth endpoint tests — register / login / me + rejection paths (§6)."""

from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import register_login


async def test_register_login_me_roundtrip(api_client: AsyncClient) -> None:
    reg = await api_client.post(
        "/api/auth/register", json={"email": "Reader@Example.com", "password": "hunter2hunter"}
    )
    assert reg.status_code == 201, reg.text
    body = reg.json()
    assert body["email"] == "reader@example.com"  # normalised
    assert body["id"]

    login = await api_client.post(
        "/api/auth/login", json={"email": "reader@example.com", "password": "hunter2hunter"}
    )
    assert login.status_code == 200
    token = login.json()["access_token"]
    assert login.json()["token_type"] == "bearer"

    me = await api_client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "reader@example.com"
    assert me.json()["id"] == body["id"]


async def test_register_rejects_duplicate_email(api_client: AsyncClient) -> None:
    await register_login(api_client, "dupe@example.com")
    second = await api_client.post(
        "/api/auth/register", json={"email": "dupe@example.com", "password": "password123"}
    )
    assert second.status_code == 409
    assert second.json()["error"]["type"] == "email_taken"


async def test_register_validation(api_client: AsyncClient) -> None:
    bad_email = await api_client.post(
        "/api/auth/register", json={"email": "not-an-email", "password": "password123"}
    )
    assert bad_email.status_code == 422
    assert bad_email.json()["error"]["type"] == "validation_error"

    short_pw = await api_client.post(
        "/api/auth/register", json={"email": "x@example.com", "password": "short"}
    )
    assert short_pw.status_code == 422


async def test_login_rejects_bad_credentials(api_client: AsyncClient) -> None:
    await register_login(api_client, "real@example.com", "correct-horse")
    wrong = await api_client.post(
        "/api/auth/login", json={"email": "real@example.com", "password": "wrong-password"}
    )
    assert wrong.status_code == 401
    assert wrong.json()["error"]["type"] == "invalid_credentials"

    missing = await api_client.post(
        "/api/auth/login", json={"email": "nobody@example.com", "password": "whatever12"}
    )
    assert missing.status_code == 401


async def test_me_rejects_missing_and_bad_token(api_client: AsyncClient) -> None:
    no_token = await api_client.get("/api/auth/me")
    assert no_token.status_code == 401
    assert no_token.json()["error"]["type"] == "unauthorized"

    bad = await api_client.get("/api/auth/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert bad.status_code == 401
    assert bad.json()["error"]["type"] == "unauthorized"
