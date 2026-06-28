# Notifications & Webhooks platform — DESIGN.md (living roadmap)

Owner: notifications agent. NEW package `backend/app/notifications/` + a route
(`backend/app/api/routes/notifications.py`). Reads kinora.md §5 (events) + §12
(reliability: idempotency, retries-with-backoff, DLQ, circuit-breaking).

## Why this exists

The system already pushes **§5.6 generation events** to the *live UI* over Redis
pub/sub (SSE/WS): `clip_ready`, `budget_low`, `agent_activity`, `conflict_choice`,
`ingest_progress`, … But those are ephemeral — if nobody has the workspace open,
they vanish. There is no **durable, out-of-band notification** path: "your book
finished importing", "a render is done", "budget is running low", "a continuity
conflict needs your decision" delivered by email / push / in-app inbox / outbound
webhook to a third-party integration.

This platform is the durable counterpart to the live event bus: it maps the same
**domain events** onto **notifications**, applies **user preferences + quiet
hours + digest batching**, renders **localized templates**, and delivers them
across **pluggable channels** with §12-grade reliability (idempotent outbox,
exponential-backoff retries, circuit breaking, dead-letter store, delivery-status
tracking). Outbound webhooks are **HMAC-signed**.

## Design at a glance

```
domain event  ──▶  EventRouter (subscriptions)  ──▶  Notification(s)
                                                          │
                              UserPreferences + QuietHours + Digest gating
                                                          │
                                              Outbox (idempotent, durable)
                                                          │
                            Dispatcher  ──▶  Channel(email|push|in_app|webhook)
                                                          │              │
                                       Transport (fake in tests)   WebhookDelivery
                                                          │         (HMAC, retries,
                                              DeliveryRecord (status)  circuit-breaker, DLQ)
```

### Core abstractions (`app/notifications/`)
- `errors.py` — typed exception hierarchy (transient vs permanent transport failures).
- `events.py` — `DomainEvent` enum + `DomainEventEnvelope`; pure mapping helpers.
- `models.py` (Pydantic) — `Notification`, `Recipient`, `RenderedMessage`, `Channel`
  enum, `DeliveryStatus` enum, `NotificationPriority`.
- `backoff.py` — pure exponential backoff w/ jitter + `RetryPolicy`.
- `circuit.py` — circuit breaker (closed→open→half-open) per channel/endpoint.
- `quiet_hours.py` — pure quiet-hours math (is-quiet-now, next-open-at, tz-aware).
- `templates.py` — templated notifications + localization hook (`TemplateRegistry`,
  `Locale`, `{var}` interpolation, per-locale catalogs, default catalog).
- `preferences.py` — `NotificationPreferences`, per-(event,channel) opt-in, digest cadence.
- `digest.py` — digest batching: accumulate, roll up, flush on cadence.
- `transports.py` — pluggable transports incl. **fakes** + real SMTP/HTTP stubs
  (no network in tests; injected).
- `webhooks.py` — `WebhookSigner` (HMAC-SHA256, timestamped, replay-safe header),
  `WebhookEndpoint`, `WebhookDeliveryEngine` (retries+circuit+DLQ).
- `channels.py` — `Channel` protocol; `EmailChannel`, `PushChannel`, `InAppChannel`,
  `WebhookChannel` adapters over transports.
- `outbox.py` — idempotent outbox (dedup on idempotency key), in-memory + repo-backed.
- `deadletter.py` — dead-letter store interface + in-memory impl.
- `delivery.py` — delivery-status tracking value objects + recorder protocol.
- `dispatcher.py` — orchestrates: gate by prefs/quiet-hours/digest → outbox →
  channel.send → record status; retry decisions.
- `subscriptions.py` — `EventRouter`: domain event → which notifications to emit.
- `service.py` — `NotificationService` facade wiring the above (the composition seam).
- `repository.py` — DB persistence (endpoints, deliveries, preferences, outbox, DLQ, inbox).
- `factory.py` — builds the DB-backed `NotificationService` (session-per-call adapter
  stores) + the `NotificationBridge` with DB-backed recipient/title resolvers.
- `bridge.py` — `NotificationBridge`: a **consumer** of the live §5.6 Redis channels
  (`psubscribe kinora:events:*`) that maps wire events → durable notifications.
  Never touches the publishers, so it is purely additive.
- `inapp.py` — the durable in-app inbox store + value type.
- `metrics.py` — Prometheus counters on the default registry (dispatched/webhook/DLQ).

### DB tables (new, migration `n1f2a3b4c5d6` chaining on head `a1b2c3d4e5f6`)
- `notification_preferences` — per user: channel opt-ins (JSONB), quiet hours, digest.
- `webhook_endpoints` — per user: url, secret, subscribed events, active flag.
- `notification_outbox` — idempotent outbox (idempotency_key unique).
- `notification_deliveries` — delivery-status tracking.
- `notification_inbox` — the durable in-app inbox (subject/body/read state).
- `notification_deadletters` — give-ups (channel, payload, last_error).

### Route (`/api/...`)
- `GET/PUT /me/notification-preferences`
- `GET /me/notifications` (in-app inbox) + `POST /me/notifications/{id}/read`
- CRUD `POST/GET/DELETE /me/webhooks` (+ `POST /me/webhooks/{id}/test`)
- `GET /me/notifications/deliveries` (status tracking)

### Additive shared-file changes (DONE — documented per the rules)
- `app/db/models/__init__.py` — export the 6 new models (additive import + `__all__`).
- `app/api/routes/__init__.py` — append `notifications.router` to `ROUTERS` (additive).
- `app/core/config.py` — append `notify_*` settings (additive, all defaulted).
- `app/composition.py` — add `notification_service` seam + lazy `notifications`
  property + `notify_event()` hook + `start_notification_bridge()` (all additive).
- `app/main.py` — one additive line in the lifespan: start the bridge alongside
  the idle sweeper, gated by the same `run_idle_sweeper` app-state flag (so tests
  never start it — keeps the timing-sensitive worker/pubsub tests isolated).
- New Alembic migration `n1f2a3b4c5d6` chaining on `a1b2c3d4e5f6` (verified up/down,
  no model drift from my tables).

## Hard constraints honored
- KINORA_LIVE_VIDEO stays OFF; zero credits.
- No real emails / pushes / webhook HTTP in tests — **fake transports injected**.
- Leave everything in the working tree; do not commit.

## Milestones / phases
- [x] P0 — design, study event infra, scaffold package
- [x] P1 — pure core: errors, backoff, circuit breaker, quiet hours, templates, events
- [x] P2 — models + transports (fakes) + webhook signer + channels
- [x] P3 — outbox + dead-letter + delivery status
- [x] P4 — preferences + digest + subscriptions/EventRouter
- [x] P5 — dispatcher orchestration
- [x] P6 — DB models + repositories + Alembic migration
- [x] P7 — NotificationService facade + composition wiring
- [x] P8 — API route + schemas
- [x] P9 — tests (unit + infra-gated integration), lint+typecheck green
```
