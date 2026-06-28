# Quickstart

This walks the whole loop: authenticate, list books, open a reading session,
stream intent, and receive a `clip_ready` event — in both SDKs.

## 1. Bring up a backend

Locally (see the repo README for the full stack):

```bash
cp .env.example backend/.env      # set DASHSCOPE_API_KEY=...
make stack-up                      # postgres + redis + minio + api + workers
make seed-demo                     # loads the bundled public-domain demo book
```

The API serves at `http://localhost:8000` (OpenAPI docs at `/docs`). The demo
login is `demo@kinora.local` / `demo-password-123`.

## 2. Install an SDK

```bash
# TypeScript / JavaScript (Node 20+ or a bundler)
npm install @kinora/sdk

# Python 3.11+
pip install kinora
```

## 3. The whole loop — TypeScript

```ts
import { KinoraClient } from "@kinora/sdk";

const client = new KinoraClient({ baseUrl: "http://localhost:8000" });

// Auth (registers the account if it does not exist yet).
await client.auth.loginOrRegister({
  email: "demo@kinora.local",
  password: "demo-password-123",
});

// Pick a ready book.
const books = await client.books.list();
const book = books.collect().find((b) => b.status === "ready") ?? books.first()!;

// Open a reading session and tell the scheduler where the reader is.
const session = await client.sessions.create({ book_id: book.id, focus_word: 0 });
await client.sessions.intent(session.session_id, { focus_word: 120, velocity: 4.2 });

// Stream the live generation events.
const controller = new AbortController();
for await (const ev of client.sessions.events(session.session_id, { signal: controller.signal })) {
  if (ev.event === "buffer_state") console.log("ahead:", ev.committed_seconds_ahead, "s");
  if (ev.event === "clip_ready") {
    console.log("clip:", ev.oss_url);
    controller.abort(); // stop after the first clip
    break;
  }
}
```

## 4. The whole loop — Python

```python
from kinora import KinoraClient

with KinoraClient("http://localhost:8000") as client:
    client.auth.login_or_register("demo@kinora.local", "demo-password-123")

    book = next((b for b in client.books.list() if b.status == "ready"), None)
    if book is None:
        raise SystemExit("no ready book — run `make seed-demo`")

    session = client.sessions.create(book.id, focus_word=0)
    client.sessions.intent(session.session_id, focus_word=120, velocity=4.2)

    for event in client.sessions.iter_events(session.session_id):
        if event.name == "buffer_state":
            print("ahead:", event["committed_seconds_ahead"], "s")
        if event.name == "clip_ready":
            print("clip:", event["oss_url"])
            break
```

## 5. Async Python

```python
import asyncio
from kinora import AsyncKinoraClient

async def main() -> None:
    async with AsyncKinoraClient("http://localhost:8000") as client:
        await client.auth.login("demo@kinora.local", "demo-password-123")
        async for event in client.sessions.iter_events(session_id):
            if event.name == "clip_ready":
                print(event["oss_url"])
                break

asyncio.run(main())
```

Next: [Authentication](authentication.html), [Streaming events](guide-events.html),
or the full [API reference](api-reference.html).
