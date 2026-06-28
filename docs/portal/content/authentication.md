# Authentication

Kinora uses **JWT bearer tokens**. You register an account, log in to exchange
credentials for an access token, then attach that token (`Authorization: Bearer
<token>`) to every authenticated request. The SDKs do this for you.

## The flow

1. `POST /api/auth/register` — create an account (email + a password of at least
   8 characters). Returns the public user record.
2. `POST /api/auth/login` — exchange credentials for a token. Returns
   `{ access_token, token_type, expires_in }`.
3. `GET /api/auth/me` — the authenticated user (a quick "am I logged in" check).

Every other endpoint requires the bearer token. The auth surface is
rate-limited (credential-stuffing defence) — back off on a `429`.

## TypeScript

```ts
import { KinoraClient, AuthError } from "@kinora/sdk";

const client = new KinoraClient({ baseUrl: "http://localhost:8000" });

// login() stores the token on the client for subsequent calls.
await client.auth.login({ email: "you@example.com", password: "hunter2hunter2" });
console.log(client.isAuthenticated()); // true

const me = await client.auth.me();
console.log(me.email);

// Or do both in one call:
await client.auth.loginOrRegister({ email: "new@example.com", password: "hunter2hunter2" });
```

### Where the token lives

By default the token is held in memory. In a browser, opt into `localStorage`
parity with the renderer (the `kinora.token` key):

```ts
import { KinoraClient, browserTokenStore } from "@kinora/sdk";

const client = new KinoraClient({
  baseUrl: "http://localhost:8000",
  tokenStore: browserTokenStore(),
});
```

You can also set/clear it directly: `client.token = "..."` / `client.token = null`,
or `client.auth.logout()`.

## Python

```python
from kinora import KinoraClient, AuthError

with KinoraClient("http://localhost:8000") as client:
    client.auth.login("you@example.com", "hunter2hunter2")  # stores the token
    print(client.is_authenticated())  # True
    print(client.auth.me().email)

    # Construct already-authenticated:
    authed = KinoraClient("http://localhost:8000", token="eyJ...")
```

## Handling auth failures

A wrong password or a missing/expired token raises `AuthError` (HTTP 401):

```python
try:
    client.auth.login("you@example.com", "wrong")
except AuthError as e:
    print(e.status, e.type)  # 401 invalid_credentials
```

```ts
try {
  await client.auth.login({ email, password });
} catch (e) {
  if (e instanceof AuthError) console.error("bad credentials");
}
```

## Streaming auth

Server-Sent-Events and WebSockets cannot set request headers in the browser, so
those endpoints also accept the token as a `?token=` query parameter. The SDKs
send the bearer **header** by default (works server-side); pass
`tokenInQuery: true` (TS) / `token_in_query=True` (Python) to also append the
query parameter when fronted by a proxy that strips auth headers from streams.
See [Streaming events](guide-events.html).
