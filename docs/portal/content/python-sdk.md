# Python SDK (`kinora`)

A typed client for Python 3.11+, built on `httpx`, with both a **synchronous**
`KinoraClient` and an **asynchronous** `AsyncKinoraClient` sharing one transport
core. Full type hints; ships `py.typed`-clean under `mypy --strict`.

## Install

```bash
pip install kinora
```

## Sync client

```python
from kinora import KinoraClient

with KinoraClient("http://localhost:8000", timeout_s=15.0) as client:
    client.auth.login("demo@kinora.local", "demo-password-123")
    for book in client.books.list():
        print(book.title, book.status)
```

The client is a context manager (it owns an `httpx.Client`); pass your own with
`http_client=...` if you want to manage the connection pool.

## Async client

```python
import asyncio
from kinora import AsyncKinoraClient

async def main() -> None:
    async with AsyncKinoraClient("http://localhost:8000") as client:
        await client.auth.login("demo@kinora.local", "demo-password-123")
        books = await client.books.list()
        async for ev in client.sessions.iter_events(session_id):
            if ev.name == "clip_ready":
                print(ev["oss_url"])
                break

asyncio.run(main())
```

The async API mirrors the sync one exactly — same resource namespaces, same
method names, with `await` and `async for`.

## Resource namespaces

| Namespace | Methods |
|---|---|
| `client.auth` | `register`, `login`, `login_or_register`, `me`, `logout` |
| `client.books` | `upload`, `list`, `get`, `page`, `canon`, `shots`, `wait_until_ready` |
| `client.films` | `events`, `scene_film` |
| `client.sessions` | `create`, `get`, `intent`, `seek`, `iter_events` |
| `client.director` | `comment`, `canon_edit`, `conflict_choice`, `conflicts`, `demo_conflict` |
| `client.prefs` | `me`, `book`, `reset_me`, `reset_book` |
| `client.eval` | `buffer_trace`, `report` |
| `client.optim` | `cost`, `perf` |

## Typed models

Responses are frozen dataclasses (`BookResponse`, `SessionResponse`, ...) built
with a forward-compatible `from_dict` — unknown fields are preserved in `.extra`
rather than rejected, so a newer backend never breaks parsing:

```python
from kinora.models import BookResponse

book = client.books.get(book_id)
print(book.title, book.status, book.progress)
print(book.extra)          # any fields the SDK does not yet name
print(book.get("title"))   # read named or extra fields by key
```

## Errors & retries

Every non-2xx raises a typed exception under `KinoraError`; idempotent requests
retry with backoff. See [Errors & retries](errors-and-retries.html).

```python
from kinora import NotFoundError, RetryPolicy, KinoraClient

client = KinoraClient("http://localhost:8000", retry=RetryPolicy(max_attempts=5))
try:
    client.books.get("missing")
except NotFoundError as e:
    print(e.status, e.type)  # 404 book_not_found
```

## Streaming events

```python
for event in client.sessions.iter_events(session_id):
    print(event.name, event.data)
```

The decoder (`kinora.SseDecoder`, `kinora.decode_text_stream`) is also exposed
standalone if you want to parse an SSE byte stream yourself.

## Introspecting the surface

```python
from kinora import ENDPOINTS, EVENTS, ERROR_TYPES
from kinora.spec import endpoints_by_tag, full_path

print(len(ENDPOINTS), "endpoints")
print(full_path(next(e for e in ENDPOINTS if e["id"] == "login")))  # /api/auth/login
```

## Develop & test

```bash
cd clients/python
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check src
.venv/bin/mypy src/kinora
.venv/bin/pytest         # all HTTP mocked via respx — zero live calls
```
