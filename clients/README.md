# Kinora developer SDKs + contract

This directory is the **developer-experience layer** over Kinora's REST API: a
single source-of-truth API description, two typed SDKs generated from it, a
machine-readable OpenAPI document, a contract-drift test, and runnable examples.
The versioned documentation portal lives in [`../docs/portal`](../docs/portal).

```
clients/
  spec/               the ONE source of truth + generators
    catalog.mjs         every endpoint, event, error, and model (edit here)
    kinora-api.ts       typed view of the catalog (re-exports it)
    generate.mjs        -> openapi.json
    sync-ts.mjs         -> clients/typescript/src/spec.ts
    sync-py.mjs         -> clients/python/src/kinora/spec.py
    openapi.json        generated OpenAPI 3.1 document
  typescript/         @kinora/sdk — isomorphic TS SDK (Node 20+ / browser)
  python/             kinora — sync + async Python SDK (httpx)
  contract-drift/     check_drift.py — flags when the SDKs lag the API
  examples/           runnable end-to-end scripts (mock by default)
```

## Single source of truth

`spec/catalog.mjs` is the **only** place the API surface is declared. Everything
else is generated from it, so the SDKs, the OpenAPI document, and the docs
reference can never silently disagree:

```bash
node clients/spec/generate.mjs    # -> clients/spec/openapi.json
node clients/spec/sync-ts.mjs     # -> clients/typescript/src/spec.ts
node clients/spec/sync-py.mjs     # -> clients/python/src/kinora/spec.py
```

Each generator takes `--check` (exit 1 if stale) — the staleness gate run by the
contract-drift test.

## The SDKs

| | TypeScript (`@kinora/sdk`) | Python (`kinora`) |
|---|---|---|
| Runtime | Node 20+, browsers | Python 3.11+ |
| HTTP | `fetch` | `httpx` (sync + async) |
| Streaming | typed SSE async iterator + callback | typed SSE sync + async iterator |
| Errors | typed class hierarchy | typed exception hierarchy |
| Retries | exp backoff + jitter, `Retry-After` | exp backoff + jitter, `Retry-After` |
| Pagination | `Page<T>` wrapper | plain lists (forward-compatible) |

See [`docs/portal`](../docs/portal) for guides + the API reference, or each SDK's
README ([TS](typescript/), [Python](python/README.md)).

## Verify

```bash
# TypeScript SDK
cd clients/typescript && npm install
npx tsc --noEmit && npx vitest run

# Python SDK
cd clients/python && python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check src && .venv/bin/mypy src/kinora && .venv/bin/pytest

# Contract drift (no backend install needed — static AST parse)
python clients/contract-drift/check_drift.py

# Docs portal
node docs/portal/build/build.mjs --check
```

Every SDK test mocks HTTP — **zero live calls**, and nothing requires
`KINORA_LIVE_VIDEO`.
