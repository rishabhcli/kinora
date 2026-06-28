# Changelog & versioning

## Versioning model

The API surface this portal documents is versioned independently of the backend
build, as **`API_VERSION`** (declared once in the source-of-truth catalog,
`clients/spec/catalog.mjs`). Both SDKs and this site report that version.

- **TypeScript SDK** `@kinora/sdk` and **Python SDK** `kinora` track the same
  major version as the documented API surface.
- The SDKs are **forward-compatible**: response models carry an open shape, so a
  newer backend adding a field never breaks a pinned SDK. New *endpoints* require
  an SDK release — the [contract-drift test](#contract-drift) is how we catch the
  need for one.
- Breaking changes (removed/renamed endpoints or fields) bump the major version.

## Single source of truth

Everything is generated from one catalog:

```
clients/spec/catalog.mjs           # the ONE declaration of the surface
  -> clients/spec/openapi.json      # node clients/spec/generate.mjs
  -> clients/typescript/src/spec.ts # node clients/spec/sync-ts.mjs
  -> clients/python/.../spec.py      # node clients/spec/sync-py.mjs
  -> docs/portal (API reference)     # node docs/portal/build/build.mjs
```

Regenerate all derived artifacts after editing the catalog:

```bash
node clients/spec/generate.mjs
node clients/spec/sync-ts.mjs
node clients/spec/sync-py.mjs
node docs/portal/build/build.mjs
```

## Contract drift

A checker re-derives the live route surface from the backend and diffs it against
the catalog, failing when the SDKs/spec have fallen behind:

```bash
python clients/contract-drift/check_drift.py            # static (no backend install)
python clients/contract-drift/check_drift.py --dynamic   # imports the FastAPI app
python clients/contract-drift/check_drift.py --json
```

Wire it into CI so an added backend route trips the build until the catalog (and
therefore both SDKs + this reference) is updated.

## Releases

### v1.0.0

- Initial public API surface: auth, books/upload, films, sessions (intent/seek),
  director tools, directing-style preferences, eval/optim, and the SSE event
  channel.
- TypeScript SDK (`@kinora/sdk`): isomorphic, fetch-based, typed errors, retries,
  pagination wrapper, typed SSE streaming.
- Python SDK (`kinora`): sync + async clients on `httpx`, typed errors, retries,
  typed SSE streaming, `mypy --strict` clean.
- OpenAPI 3.1 document, this versioned docs portal, and the contract-drift test.

## Roadmap

- Code-generate the SDK model types directly from `openapi.json` (today they are
  hand-mirrored from the backend schemas and kept honest by the drift test).
- First-class WebSocket clients in both SDKs (today SSE is first-class; WS is
  documented and reachable via the REST control endpoints).
- A `kinora` CLI wrapping the Python SDK.
- Richer JSON-Schema component extraction in the OpenAPI emitter.
