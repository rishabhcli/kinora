# Workspaces & Teams — `backend/app/workspaces/`

A multi-user **workspace / collaboration-ownership** subsystem layered *on top of*
the existing single-user identity (`users`) and per-row book ownership
(`books.user_id`, kinora.md §5.1). It does **not** touch the auth domain: it
composes with the existing `User` identity and adds organizations, teams,
membership, invitations, role-based sharing of books + collections, a transferable
ownership model, per-workspace settings + quotas, an activity feed, and seat
management — all behind a single clean `can(user, action, resource)` authorization
API that any other domain can call.

> **Read first:** kinora.md §5 (the product & UI — the shelf, the workspace, the
> Director's canon edits). The shelf today is *per user* (`books.user_id`); this
> subsystem makes a shelf **shareable** by attaching books to a workspace and
> granting roles to that workspace's members.

## Design principles

1. **Additive & fail-closed.** Legacy unowned books resolve to "owned by nobody."
   A workspace grant is *additive* on top of the durable `books.user_id` owner —
   the personal owner always retains owner-level access; a workspace can grant
   *additional* members editor/commenter/viewer. No existing row is mutated by
   importing this package.
2. **Pure policy engine.** `policy.py` is a side-effect-free decision function —
   `decide(principal, action, grants) -> Decision`. It is unit-testable with zero
   infra. The DB-backed `AuthorizationService` resolves a principal's effective
   grants and delegates the *decision* to the pure engine.
3. **Role lattice.** `OWNER > EDITOR > COMMENTER > VIEWER`. Roles map to a set of
   capabilities (actions). The most-permissive applicable grant wins (a user may
   hold a direct share *and* a workspace-membership-derived role).
4. **One unit of work.** Repositories `flush` but never `commit`; the request /
   service boundary owns the transaction (matches `BaseRepository`).

## Data model (Alembic: `f2a9c4d10e7b_workspaces_and_teams`, on head `a1b2c3d4e5f6`)

| Table | Purpose |
|---|---|
| `organizations` | top-level tenant; owns seats + plan; `owner_user_id` |
| `workspaces` | a shared library inside an org; `org_id`, `slug`, settings JSONB |
| `workspace_members` | (workspace, user) → role + status; the membership edge |
| `workspace_invitations` | email-token accept flow; pending/accepted/revoked/expired |
| `resource_shares` | polymorphic (resource_type, resource_id) → principal grant |
| `collections` | a named bundle of books inside a workspace |
| `collection_items` | (collection, book) membership |
| `ownership_transfers` | audited transfer-of-ownership requests + their resolution |
| `workspace_activity` | append-only activity feed (who did what, when) |

All ids are 32-char hex (`StrIdMixin`); FKs cascade or set-null per the existing
conventions (user delete → `SET NULL` so removed accounts orphan, not cascade).

## Enums (`app/workspaces/roles.py`)

- `Role`: `owner | editor | commenter | viewer` — a totally-ordered lattice.
- `Action`: the verbs the engine arbitrates (view / comment / edit / render /
  manage_members / share / transfer_ownership / delete / manage_settings / …).
- `MemberStatus`: `active | invited | suspended | removed`.
- `InvitationStatus`: `pending | accepted | revoked | expired`.
- `ResourceType`: `book | collection | workspace`.
- `TransferStatus`: `pending | accepted | declined | cancelled`.

## Public API surface

- **`workspaces.authz.AuthorizationService`** — the composable engine:
  `can(user, action, resource) -> bool`, `require(...)`, `effective_role(...)`,
  `accessible_book_ids(user, workspace_id)`. Other domains call this.
- **`workspaces.service.WorkspaceService`** — org/workspace/member/invitation/
  collection/transfer/seat operations, each emitting an activity row.
- **`app/api/routes/workspaces.py`** — REST surface (additive router).

## Milestones / roadmap

- [x] **M1** — roles + actions lattice + pure policy engine (`roles.py`, `policy.py`).
- [x] **M2** — ORM models + Alembic migration + repositories.
- [x] **M3** — `AuthorizationService` (effective-grant resolution + `can`).
- [x] **M4** — `WorkspaceService` (CRUD, invites, shares, transfer, seats, feed).
- [x] **M5** — invitation token machinery (sign/verify, accept flow).
- [x] **M6** — quotas + seat management policy.
- [x] **M7** — REST router + DTOs + composition wiring.
- [x] **M8** — comprehensive tests (pure-policy unit + infra-gated integration):
  129 passing — 88 pure-unit (`test_workspaces_policy/invitations/quotas.py`,
  run anywhere) + 41 infra-gated (`test_workspaces_service.py`,
  `test_api_workspaces.py`, skip without `KINORA_TEST_*`). Verified against an
  isolated DB (`kinora_workspaces_test` on :5433) + redis db 15 + minio.
- [ ] **M9 (future)** — per-collection nested permissions (collection-level direct
  shares, currently only workspace-derived); activity-feed pagination cursor +
  SSE push onto the existing live-event channel; bulk seat invite + CSV import;
  audit export; SSO/SCIM-provisioned orgs; a per-workspace video-seconds budget
  scope wired into `BudgetService`.

## Additive shared-file changes (documented per the parallel-agent contract)

- `app/db/models/__init__.py` — import the new models (table registration).
- `app/api/routes/__init__.py` — append `workspaces.router` to `ROUTERS`.
- `migrations/versions/f2a9c4d10e7b_*.py` — new migration on head `a1b2c3d4e5f6`.
- No edits to `core/config.py` or `composition.py` are required (the service is
  request-scoped and built from the container's `session_factory`).
