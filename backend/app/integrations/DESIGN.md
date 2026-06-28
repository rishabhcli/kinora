# Third-party integrations & import (`backend/app/integrations/`)

Living roadmap for the integrations domain. Owned by the integrations agent.
Everything here is **additive**: it plugs into the existing ingest public API
(`app.ingest.service.ingest_pdf` via `Container.run_ingest`) and never edits
`backend/app/ingest/`.

## Goal

Let a reader bring material in from where they already keep it — Kindle exports,
Readwise highlights, Notion pages, RSS/OPML feeds, Pocket saves, and arbitrary
web articles — and turn each source item into a Kinora book that flows through
the unchanged §9.1 Phase-A pipeline.

## The "ingest entry format"

`app.ingest.ingest_pdf(book_id, pdf_bytes, ...)` is the one public entry point.
Every connector therefore normalizes its source into a `NormalizedDocument`
(title, author, ordered `NormalizedBlock`s of text/heading/quote). A document is
rendered to **real PDF bytes** with PyMuPDF (`document.render_pdf`, the same
`fitz` HTML→PDF path EPUB upload uses) so the entire downstream pipeline runs
byte-for-byte unchanged. The connectors produce content; the renderer produces
the exact `pdf_bytes` the ingest API already accepts.

## Hard rules honoured

- **No real network calls in code paths under test.** Every connector talks to
  the outside world only through an injected `AsyncHttpClient`; tests inject
  `FakeHttpClient`. The connectors never import `httpx` directly.
- **OAuth secrets** live in config as additive optional settings; token storage
  is a DB table; refresh goes through the same injected HTTP client.
- **KINORA_LIVE_VIDEO stays OFF**; import only ever drives Phase-A ingest, which
  spends zero video-seconds.
- **No edits to `backend/app/ingest/`.** Import calls the public ingest API only.

## Components

| File | Role |
|---|---|
| `errors.py` | Exception hierarchy (`IntegrationError`, `ConnectorError`, `RateLimited`, `AuthExpired`, `TransientError`, `PermanentError`). |
| `models.py` | Normalized import format: `NormalizedBlock`, `NormalizedDocument`, `SourceItem`, `FetchPage`, `SyncCursor`, capability/health enums. |
| `document.py` | `render_pdf(doc)` — normalized document → PDF bytes (PyMuPDF HTML→PDF). Deterministic, offline. |
| `htmlutil.py` | Tiny stdlib HTML→text/readability extractor (no third-party dep). |
| `http.py` | `AsyncHttpClient` protocol + `HttpResponse`; `HttpxClient` (real, lazy) + `FakeHttpClient` (tests). |
| `clock.py` | `Clock` protocol + `SystemClock` / `FakeClock` for deterministic backoff. |
| `backoff.py` | Pure exponential-backoff-with-jitter schedule + retry classification. |
| `crypto.py` | Token blob seal/unseal (Fernet when available; reversible fallback otherwise). |
| `connector.py` | `SourceConnector` ABC, `ConnectorContext`, `Capability`, `ConnectorInfo`. |
| `registry.py` | `ConnectorRegistry` — name → connector-factory; `default_registry()`. |
| `oauth.py` | `OAuth2Config`, `OAuth2Client` (authorize URL, code exchange, refresh) over the injected HTTP client; `TokenSet`. |
| `connectors/readwise.py` | Readwise highlights export (token auth, incremental by `updatedAfter`). |
| `connectors/kindle.py` | Kindle "My Clippings.txt" export parser (file upload, not network). |
| `connectors/notion.py` | Notion page/database import (OAuth/token, block tree → blocks). |
| `connectors/rss.py` | RSS/Atom feed + OPML expansion; per-entry articles. |
| `connectors/pocket.py` | Pocket saved articles (OAuth, incremental by `since`). |
| `connectors/web.py` | Plain web-article extraction (readability-style heuristic, stdlib HTML parser). |
| `sync.py` | `SyncEngine` — incremental fetch, cursor/etag, content-hash dedup, backoff, per-item partial-failure isolation; emits `SyncReport`. |
| `webhooks.py` | `WebhookVerifier` + `WebhookRouter` (HMAC signature verification) for push sources. |
| `health.py` | `connection_health` + `sync_status` projections for the UI surface. |
| `service.py` | `IntegrationsService` facade: connect, list, sync, import-one, disconnect — ties registry + store + ingest. |

## DB (migration `i7a1b2c3d4e5` on head `a1b2c3d4e5f6`)

- `app_connections` — one per (user, provider) connection: sealed token blob
  (JSONB), status, scopes, account label, cursor, etag, health counters.
- `imported_items` — dedup ledger: (connection, source_item_id) → book_id with a
  content_hash; UNIQUE(connection_id, source_item_id) so re-sync never re-imports.
- `sync_runs` — append-only run history: counts, status, error, started/finished.

## Status

- [x] Phase 1 — errors, models, document/html, http/clock/backoff/crypto primitives.
- [x] Phase 2 — connector ABC + registry + OAuth2 client.
- [x] Phase 3 — DB models + repositories + migration.
- [x] Phase 4 — source connectors (readwise, kindle, notion, rss/opml, pocket, web).
- [x] Phase 5 — sync engine (incremental, dedup, backoff, partial-failure).
- [x] Phase 6 — webhook receivers + signature verification.
- [x] Phase 7 — health/sync-status surface + IntegrationsService facade.
- [x] Phase 8 — API route + composition wiring (additive seam) + tests.

## Additive shared-file changes

- `app/core/config.py` — optional OAuth/integration settings (all default-None/off).
- `app/composition.py` — `Container.build_integrations()` lazy seam +
  `_KinoraIngestGateway` (mirrors `POST /books`) + http-client shutdown.
- `app/db/models/__init__.py` — register the three new models.
- `app/api/routes/__init__.py` — mount `integrations.router`.

## Testing

- Unit suite (offline, no infra): `test_integrations_primitives`,
  `test_integrations_connectors`, `test_integrations_sync`,
  `test_integrations_oauth_webhooks` — 71 tests, run anywhere.
- Infra-gated (throwaway Postgres/Redis/MinIO): `test_integrations_service`,
  `test_api_integrations` — 16 tests; skip cleanly when the `KINORA_TEST_*`
  vars are unset. Run against an isolated DB (`kinora_integrations_test` :5433)
  + redis db 15, never the live `kinora` DB.
- The whole-suite-with-infra run has known pre-existing cross-test flakiness
  (shared-redis token-bucket / worker pubsub-drain timing — see the project's
  memory notes); the integrations tests themselves are deterministic.

## Future roadmap (not yet built)

- A scheduled background sync loop (cron/idle-sweeper) driving `sync()` per
  active connection on an interval; the manual `POST /sync` + webhook receiver
  are the building blocks.
- Webhook → automatic sync fan-out (the receiver currently verifies + 202s; the
  fan-out trigger is left to the operator's chosen mechanism).
- Full-text fetch for Pocket items (Pocket returns excerpts only; chain the web
  connector on the stored URL for the body).
- More connectors (Instapaper, Hypothesis, Matter, Apple Books) — each is a new
  `SourceConnector` subclass + a registry line.
