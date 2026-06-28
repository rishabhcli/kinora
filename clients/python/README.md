# kinora — Python SDK

Typed Python client for the [Kinora](../../README.md) API: auth, books/upload,
sessions + intent/seek, SSE event streaming, and the director tools — with
retries, typed errors, and both **sync** and **async** clients.

```bash
pip install kinora
```

## Quickstart (sync)

```python
from kinora import KinoraClient

with KinoraClient("http://localhost:8000") as client:
    client.auth.login("demo@kinora.local", "demo-password-123")
    for book in client.books.list():
        print(book.title, book.status)

    session = client.sessions.create(book_id=book.id)
    client.sessions.intent(session.session_id, focus_word=120, velocity=4.2)

    for event in client.sessions.iter_events(session.session_id):
        if event.name == "clip_ready":
            print("clip:", event["oss_url"])
            break
```

## Quickstart (async)

```python
import asyncio
from kinora import AsyncKinoraClient

async def main() -> None:
    async with AsyncKinoraClient("http://localhost:8000") as client:
        await client.auth.login("demo@kinora.local", "demo-password-123")
        async for event in client.sessions.iter_events(session_id):
            if event.name == "buffer_state":
                print("ahead:", event["committed_seconds_ahead"])

asyncio.run(main())
```

## Errors

Every non-2xx response raises a subclass of `KinoraError`:

```python
from kinora import NotFoundError, RateLimitError

try:
    client.books.get("nope")
except NotFoundError as e:
    print(e.status, e.type)        # 404 book_not_found
except RateLimitError as e:
    print(e.retry_after_s)         # honored automatically by the retry layer
```

## Configuration

```python
from kinora import KinoraClient, RetryPolicy

client = KinoraClient(
    "https://api.example.com",
    token="...",                                  # or call auth.login()
    timeout_s=30.0,
    retry=RetryPolicy(max_attempts=5, base_delay_s=0.5),
)
```

Retries apply to idempotent/safe requests (GET/HEAD/DELETE + `intent`/`seek`) on
429/502/503/504 and network errors, with exponential backoff + jitter, honoring
`Retry-After`.

See the [docs portal](../../docs/portal) for guides, the API reference, and the
six-agent architecture overview.
