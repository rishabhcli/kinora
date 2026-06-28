# API + Realtime transport — DESIGN.md (living roadmap)

**Owner agent:** API + realtime transport.
**Domain:** `backend/app/api/` (routes + the new `realtime/` package) and the
realtime wiring in `app/main.py` (additive only).

Production-grade API + realtime layer that sits *additively* on top of round-1's
gateway. Round-1's `routes/events.py`, `routes/sessions.py`, `routes/director.py`
are NOT rewritten — they are extended through new modules and (where needed) one
additive router registration in `routes/__init__.py`.

The spec we serve: kinora.md §5.1–§5.6 (the event channel + the workspace) and
§4.7 (debounce/dwell/idle — the realtime layer must never thrash).

## Why a new package instead of growing `events.py`

`events.py` is a fine first-cut SSE/WS forwarder, but it has no:

- **resume** (a dropped connection loses every event in the gap),
- **connection lifecycle** accounting (no presence, no idle reaping, no caps),
- **per-route + per-user rate limiting beyond the two coarse buckets**,
- **idempotency** for unsafe POSTs,
- **cursor pagination** for the growing list endpoints,
- **API versioning / deprecation signalling**,
- **multiplayer presence** so two readers can co-watch one session.

These are cross-cutting; they belong in their own package with their own tests.

## Modules (all under `app/api/realtime/`)

| Module | Responsibility |
|---|---|
| `event_log.py` | Per-session **append-only event log** in Redis (a capped stream) keyed by a monotonic id; assigns an `id:` to every SSE frame and replays everything after a `Last-Event-ID` on reconnect (§5.6 resume). |
| `sse.py` | SSE framing helpers (`id:`/`event:`/`data:`/`retry:`), heartbeat comments, and a reusable `EventStream` that wraps subscribe + log-replay + live-tail with backpressure + a max-lifetime. |
| `connections.py` | **Connection registry / lifecycle**: per-session + per-user connection counts, max-connections caps, idle reaping, graceful drain. Backed by Redis so it works across API replicas. |
| `presence.py` | **Multiplayer presence**: who is in a session right now, their cursor/focus word + mode, join/leave + heartbeat, fan-out of `presence` events. TTL'd in Redis. |
| `idempotency.py` | **Idempotency keys** for unsafe POSTs: `Idempotency-Key` header → Redis-stored first response, replayed (`Idempotent-Replayed: true`) on a retry; in-flight collisions → 409. |
| `pagination.py` | **Opaque cursor pagination**: encode/decode a signed base64 cursor, a `Page[T]` envelope, and a `paginate()` helper. |
| `ratelimit.py` | **Per-route + per-user composable limiter** + standard `RateLimit-*` / `Retry-After` headers (extends `deps.RateLimiter` additively; does not replace it). |
| `versioning.py` | **API versioning + deprecation**: `Deprecation`/`Sunset`/`Link` headers, a `@deprecated_route` marker, and a version registry surfaced at `/api/versions`. |
| `envelopes.py` | Shared **structured response envelopes** (`Meta`, `Page`, presence DTOs) reused by the realtime routes. |
| `routes_realtime.py` | The new thin routers: resumable SSE (`/sessions/{id}/stream`), presence (`/sessions/{id}/presence`), connection stats, `/versions`, and a unified multiplayer WS. Mounted additively. |

## Cross-cutting wiring (additive — the exact shared-file edits made)

All four shared files were touched **additively only** (no existing behaviour
changed); the round-1 `events`/`sessions`/`director` routes are byte-for-byte
untouched.

- `app/api/routes/__init__.py` — appended `realtime_router` to the **end** of
  `ROUTERS`. Existing routers keep their order/positions.
- `app/main.py` — added `_realtime_connection_sweeper` + `_start_event_recorder`
  helpers and one `REALTIME_SWEEP_INTERVAL_S` const; the lifespan now collects
  background tasks into a list and *additionally* starts the recorder + sweeper
  (both defensive: a stub/misconfigured container disables them, never crashes
  startup). Suppressible via `app.state.run_realtime_sweeper = False` (mirrors the
  existing `run_idle_sweeper`). The original idle-sweeper start/stop is preserved.
- `app/core/config.py` — added one setting `realtime_recorder_elect_leader: bool
  = False` (horizontal-scale recorder leasing). No existing setting changed.
- `app/api/errors.py` — added a `ConnectionLimitExceeded → 429` exception handler
  (this file is in the API domain I own; the addition is additive).

**Critical bug caught while wiring:** the new dependency `get_realtime` imported
`Request`/`WebSocket` only under `TYPE_CHECKING`, so under `from __future__ import
annotations` FastAPI saw an unresolved `ForwardRef('Request')` and **OpenAPI
generation for the whole app crashed** (would have broken `/docs` + `/openapi.json`
for every team). Fixed by importing them at runtime in `services.py`.

## Invariants honoured

- **KINORA_LIVE_VIDEO stays OFF**; this layer spends zero model credits — it only
  moves already-produced events around.
- **Fail-open on Redis** for non-critical paths (rate-limit, presence), matching
  the existing `RateLimiter` philosophy; **fail-closed on auth/ownership**.
- **Additive on shared files**; round-1 director/session/event routes are untouched.
- Every event the log assigns an id to is the *same* §5.6 event shape already
  published on the Redis channels — the log is a transparent tee, not a new format.

## Tests

- `tests/test_realtime_unit.py` — 22 pure-logic tests (no infra): SSE framing,
  cursor codec + signature, version/deprecation registry, idempotency fingerprint.
- `tests/test_realtime_integration.py` — 27 tests against the isolated stack:
  event-log ids/replay/gap, recorder tee, presence join/move/leave + fan-out,
  connection caps + reaping, idempotency replay/conflict/mismatch/release, the
  route surface (versions, presence, stream/info, paginated history, connection
  stats, per-route limiter headers, idempotent presence-join), the resumable SSE
  stream end-to-end, and the multiplayer WS (resume + presence + fan-out).
- Full backend suite stays green: **1236 passed, 14 skipped** (lint + mypy clean).

## Roadmap / phases (status)

1. ✅ Event log + resumable SSE framing + `/sessions/{id}/stream`.
2. ✅ Connection registry + presence + multiplayer WS + sweeper + recorder tee.
3. ✅ Idempotency keys + applied to the presence-join unsafe POST (reusable
   `IdempotencyGuard` dependency ready for round-2 director-route adoption).
4. ✅ Cursor pagination + the `/events/history` paginated endpoint.
5. ✅ Per-route/per-user limiter w/ standard `RateLimit-*`/`Retry-After` headers
   + versioning/deprecation manifest at `/versions` + `@deprecated` decorator.
6. ✅ OpenAPI generates cleanly for the new surface (40 paths, documented 409/422).
7. Future: WS resume via `Last-Event-ID` *header* (done via `?last_event_id=`),
   server-sent compression negotiation, multi-region presence reconciliation,
   adopting `IdempotencyGuard` on the round-2 Director comment/regen POST, and a
   `RouteRateLimiter` on the regen path once round-2 exposes it.
