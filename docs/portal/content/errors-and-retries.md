# Errors & retries

## The error envelope

Every failed request returns a stable JSON envelope:

```json
{ "error": { "type": "book_not_found", "message": "no such book for this user", "detail": null } }
```

The SDKs decode that and raise a **typed exception** keyed on the HTTP status and
the `type` string, so you branch on a class rather than parse strings.

| Status | Type (examples) | TypeScript | Python |
|---|---|---|---|
| 401 | `invalid_credentials`, `unauthorized` | `AuthError` | `AuthError` |
| 402 | `budget_exceeded` | `BudgetExceededError` | `BudgetExceededError` |
| 403 | `forbidden` | `ForbiddenError` | `ForbiddenError` |
| 404 | `book_not_found`, `session_not_found` | `NotFoundError` | `NotFoundError` |
| 409 | `email_taken` | `ConflictError` | `ConflictError` |
| 409 | `live_video_disabled` | `LiveVideoDisabledError` | `LiveVideoDisabledError` |
| 413 / 415 | `file_too_large`, `unsupported_media_type` | `UploadError` | `UploadError` |
| 422 | `validation_error` | `ValidationError` | `ValidationError` |
| 429 | `book_quota_exceeded` | `RateLimitError` | `RateLimitError` |
| 502 | `provider_error` | `ProviderError` | `ProviderError` |
| 5xx | `internal_error` | `ServerError` | `ServerError` |
| — | timeout / network | `TimeoutError` / `NetworkError` | `TimeoutError` / `NetworkError` |

All inherit from `KinoraError`, which carries `status`, `type`, `detail`, the raw
`body`, and the `request` label (`"GET /books/{id}"`).

```ts
import { NotFoundError, ValidationError, KinoraError } from "@kinora/sdk";

try {
  await client.books.get("missing");
} catch (e) {
  if (e instanceof NotFoundError) console.warn("no such book");
  else if (e instanceof ValidationError) console.warn(e.detail?.errors);
  else if (e instanceof KinoraError) console.error(e.status, e.type);
  else throw e;
}
```

```python
from kinora import NotFoundError, ValidationError, KinoraError

try:
    client.books.get("missing")
except NotFoundError:
    print("no such book")
except ValidationError as e:
    print(e.detail.get("errors"))
except KinoraError as e:
    print(e.status, e.type)
```

## Retries

Both SDKs retry **idempotent / safe** requests automatically:

- `GET`, `HEAD`, `DELETE`, and the safe writes `intent` / `seek`,
- on `429`, `502`, `503`, `504`, and network/connection errors,
- with **exponential backoff + full jitter**, capped attempts (default 3),
- honoring a `Retry-After` header when present.

Non-idempotent writes (e.g. `POST /books`, `POST /sessions`) are **not** retried
by default, to avoid duplicate side-effects.

### Tuning the policy

```ts
import { KinoraClient } from "@kinora/sdk";

const client = new KinoraClient({
  baseUrl: "http://localhost:8000",
  timeoutMs: 30_000,
  retry: { maxAttempts: 5, baseDelayMs: 500, maxDelayMs: 20_000 },
});
```

```python
from kinora import KinoraClient, RetryPolicy

client = KinoraClient(
    "http://localhost:8000",
    timeout_s=30.0,
    retry=RetryPolicy(max_attempts=5, base_delay_s=0.5, max_delay_s=20.0),
)
```

### Rate limits

On a `429`, a `RateLimitError` carries the server's suggested delay
(`retryAfterMs` in TS, `retry_after_s` in Python). The retry layer already waits
that long before retrying; if you exhaust attempts the error surfaces so you can
back off further.

## The budget cap

Live video generation is bounded by a hard budget ceiling. When it is reached,
write paths that would spend video-seconds return `402 budget_exceeded` —
`BudgetExceededError`, whose `detail` carries `{ scope, requested, used, cap }`.
You also get a `budget_low` SSE event before the ceiling is hit. See
[Streaming events](guide-events.html).
