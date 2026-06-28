# Backend Auth & Security — DESIGN.md (living roadmap)

Owner domain (Agent: Backend auth & security). NEW package `backend/app/auth/`,
plus `backend/app/core/security.py` and a rebuilt `backend/app/api/routes/auth.py`.
Builds a production auth/security system on top of the existing minimal
register/login/me flow without breaking the Bearer-token contract that SSE/WS,
the MCP authorizer, and `api.deps.get_current_user` already depend on.

## Compatibility contract (must never break)
- `app.api.security` (`create_access_token`, `decode_access_token`, `hash_password`,
  `verify_password`, `TokenData`, `TokenError`) stays importable with the same
  signatures — `api.deps` and existing tests import it directly. New crypto lives
  in `app.core.security` + `app.auth.*`; `app.api.security` is kept as a thin
  re-export/shim so nothing downstream changes.
- Access tokens remain HS256 JWTs with a `sub` = user id. `get_current_user`
  keeps working with any token minted by the new issuer. New claims
  (`typ`, `jti`, `sid`, `roles`, `scopes`, `tenant`) are ADDITIVE and optional.
- `Settings` only gains additive fields. `composition.py` only gains additive
  Container attributes/seams. `api/routes/__init__.py` already imports `auth`.
  `db/models/__init__.py` only appends new models.

## Additive shared-file changes (per the parallel-agent rules)
- `app/core/config.py`: additive auth/security fields (refresh TTL, hasher choice,
  lockout policy, MFA issuer, API-key pepper, password policy, CSRF, session caps).
  All safe defaults; `_guard_production_secrets` extended to also reject the
  default API-key pepper outside `local`.
- `app/composition.py`: lazy `password_hasher`, `token_service`, `audit_log`,
  `auth_service`, `api_key_service` seams on `Container`.
- `app/api/routes/__init__.py`: `auth.router` already in `ROUTERS` (same module,
  richer surface) — no new import needed.
- `app/db/models/__init__.py`: append new auth models + enums.
- New Alembic migration chaining on current head `a1b2c3d4e5f6`.

## Milestones / phases — STATUS
1. [DONE] core/security.py — crypto foundation (pluggable hasher w/ SHA-256
   pre-hash, password policy + entropy, RFC 6238 TOTP, recovery codes, peppered
   API-key HMAC, secure tokens, constant-time compare, UA/device parsing).
2. [DONE] auth/tokens.py — JWT access (legacy-compatible claims + jti/sid/roles/
   scopes/tenant), refresh-token family rotation, MFA-challenge tokens, jti
   revocation port.
3. [DONE] DB models (app/db/models/auth.py) + migration f7a2b9c4d1e8 chaining on
   a1b2c3d4e5f6 — 10 tables; applied + autogen-verified no-drift on isolated DB.
4. [DONE] Repositories (app/auth/repositories.py) — one per aggregate.
5. [DONE] passwords/mfa/recovery — folded into AuthService + core.security.
6. [DONE] rbac (app/auth/rbac.py) — catalogue, wildcard matching, Principal,
   tenant isolation, scope normalisation.
7. [DONE] sessions — lifecycle + device tracking + cap eviction + revocation.
8. [DONE] api_keys — issue (scope-capped to owner), verify, list, revoke.
9. [DONE] lockout (app/auth/lockout.py) — Redis per-IP throttle + jti revocation
   store; durable per-account lockout in AuthCredentialRepo.
10. [DONE] audit (AuthAuditRepo + AuthEventType) — append-only security log.
11. [DONE] service (app/auth/service.py) — AuthService orchestrator. NB: side
    effects that precede a raise (lockout increment, reuse family-revocation) run
    in their OWN committed unit of work, else the raise rolls them back.
12. [DONE] middleware (app/auth/middleware.py) — CSRF double-submit; wired in
    main.py after the existing SecurityHeadersMiddleware.
13. [DONE] api/routes/auth.py — full surface (register/login/mfa-login/refresh/
    logout/logout-all/sessions/password/mfa/api-keys/rbac/audit); legacy
    register/login/me shape preserved.
14. [DONE] auth/deps.py (additive; api/deps.py untouched) — CurrentPrincipal
    (Bearer OR X-API-Key), require_permission/scope/role/admin factories.
15. [DONE] Tests — unit (test_core_security, test_auth_tokens, test_auth_rbac,
    test_auth_middleware_config) + integration (test_api_auth_security) on the
    isolated DB + redis db15; existing test_api_auth/test_api_ratelimit kept green.

## Test infra (isolated — never the live kinora DB / redis db 0)
- KINORA_TEST_DATABASE_URL=postgresql+asyncpg://kinora:kinora@localhost:5433/kinora_auth_test
- KINORA_TEST_REDIS_URL=redis://localhost:6379/15
- KINORA_TEST_S3_ENDPOINT_URL=http://localhost:9000

## Remaining roadmap (future)
- WebAuthn/passkeys, OAuth2 social login, JWKS rotation for RS256, per-tenant
  rate-limit quotas, anomaly scoring on the audit stream.
