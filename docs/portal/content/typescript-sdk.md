# TypeScript SDK (`@kinora/sdk`)

A typed, **isomorphic** client — it runs in Node 20+ and browsers, depending only
on the standard `fetch`, `AbortController`, and `TextDecoder`. SSE streaming is
parsed from the `fetch` response body (not `EventSource`), so it works
server-side and can carry the bearer in a header.

## Install

```bash
npm install @kinora/sdk
```

## The client

```ts
import { KinoraClient } from "@kinora/sdk";

const client = new KinoraClient({
  baseUrl: "http://localhost:8000",   // default
  token: undefined,                     // or a starting bearer token
  timeoutMs: 15_000,
  retry: { maxAttempts: 3 },            // see Errors & retries
  headers: { "X-App": "my-reader" },    // extra default headers
});
```

In a browser, persist the token like the renderer does:

```ts
import { KinoraClient, browserTokenStore } from "@kinora/sdk";
const client = new KinoraClient({ tokenStore: browserTokenStore() });
```

## Resource namespaces

The API is grouped into namespaces that mirror the backend routes:

| Namespace | Methods |
|---|---|
| `client.auth` | `register`, `login`, `loginOrRegister`, `me`, `logout` |
| `client.books` | `upload`, `list`, `get`, `page`, `canon`, `shots`, `waitUntilReady` |
| `client.films` | `events`, `sceneFilm` |
| `client.sessions` | `create`, `get`, `intent`, `seek`, `events`, `subscribe` |
| `client.director` | `comment`, `canonEdit`, `conflictChoice`, `conflicts`, `demoConflict` |
| `client.prefs` | `me`, `book`, `resetMe`, `resetBook` |
| `client.eval` | `bufferTrace`, `report` |
| `client.optim` | `cost`, `perf` |

## Pagination

List endpoints return a `Page<T>` — a thin, future-proof wrapper over the bare
array the backend returns today:

```ts
const books = await client.books.list();
console.log(books.length, books.first());
for (const b of books) console.log(b.title);       // sync iterate
for await (const b of books) console.log(b.title);  // async iterate
const ready = books.filter((b) => b.status === "ready").collect();
for (const chunk of books.chunks()) handle(chunk);  // fixed-size chunks
```

When the backend grows real server-side pagination, `Page` is the seam that
evolves — your iteration code stays the same.

## Streaming events

```ts
for await (const ev of client.sessions.events(sessionId, { signal })) {
  if (ev.event === "clip_ready") play(ev.oss_url);
}
// or the callback form:
const unsub = client.sessions.subscribe(sessionId, (ev) => { /* ... */ });
```

The event type is a discriminated union on `ev.event`; see
[Streaming events](guide-events.html).

## Typed models & errors

Every response is a typed interface (e.g. `BookResponse`, `SessionResponse`,
`CommentResponse`) with an index signature, so a newer backend field never breaks
the build. Errors are a class hierarchy under `KinoraError` — see
[Errors & retries](errors-and-retries.html).

```ts
import type { BookResponse, ShotResponse } from "@kinora/sdk";
import { NotFoundError } from "@kinora/sdk";
```

## Introspecting the surface

The SDK re-exports the source-of-truth catalog, so you can enumerate the
documented surface at runtime:

```ts
import { ENDPOINTS, EVENTS, ERROR_TYPES, API_VERSION, endpointsByTag } from "@kinora/sdk";
console.log(API_VERSION, ENDPOINTS.length, "endpoints");
```

## Build & test

```bash
cd clients/typescript
npm install
npx tsc --noEmit     # typecheck
npx vitest run        # unit tests (all HTTP mocked)
npx tsc -p tsconfig.build.json  # emit dist/
```
