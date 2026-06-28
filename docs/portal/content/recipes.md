# Examples & recipes

Self-contained snippets for common tasks. Every example is also a runnable script
under [`clients/examples/`](../../clients/examples) — they default to a mock so
they run with **no live backend and never spend video credits**; point them at a
real backend with `KINORA_BASE_URL`.

## Recipe: ingest a book and wait for it

```python
from kinora import KinoraClient

with KinoraClient("http://localhost:8000") as client:
    client.auth.login_or_register("demo@kinora.local", "demo-password-123")
    with open("book.pdf", "rb") as f:
        book = client.books.upload(f.read(), filename="book.pdf", title="My Book")
    ready = client.books.wait_until_ready(book.id, interval_s=2.0)
    print(ready.num_pages, "pages, status", ready.status)
```

## Recipe: drive a reading session from scroll position

```ts
import { KinoraClient } from "@kinora/sdk";

const client = new KinoraClient({ baseUrl: "http://localhost:8000", token });
const session = await client.sessions.create({ book_id });

// Debounce scroll, then post intent. velocity is words/sec.
let timer: ReturnType<typeof setTimeout> | undefined;
function onScroll(focusWord: number, velocity: number) {
  clearTimeout(timer);
  timer = setTimeout(() => {
    void client.sessions.intent(session.session_id, { focus_word: focusWord, velocity });
  }, 150);
}
```

## Recipe: play clips as they become ready

```python
def watch(client, session_id):
    for event in client.sessions.iter_events(session_id):
        if event.name == "clip_ready":
            yield event["shot_id"], event["oss_url"]
        elif event.name == "budget_low":
            print("budget low:", event["budget_remaining_s"], "s left")
```

## Recipe: visualise the buffer without spending video

```python
trace = client.eval.buffer_trace(session_id, velocity=5.0, duration_s=180)
for p in trace:
    bar = "#" * int(p.committed_seconds_ahead)
    print(f"{p.t:6.1f}s |{bar} {p.committed_seconds_ahead:.0f}s (low {p.low:.0f} high {p.high:.0f})")
```

## Recipe: surgical canon edit, then watch the regens

```ts
const edit = await client.director.canonEdit(bookId, {
  entity_key: "eleanor",
  changes: { description: "wears a deep crimson cloak" },
});
const pending = new Set(edit.affected_shot_ids);
for await (const ev of client.sessions.events(sessionId)) {
  if (ev.event === "regen_done" && pending.delete(ev.shot_id) && pending.size === 0) break;
}
console.log("all dependent shots re-rendered");
```

## Recipe: resolve a continuity conflict end-to-end

```python
for event in client.sessions.iter_events(session_id):
    if event.name == "conflict_choice":
        cid = event["conflict_id"]
        # Honor the established canon and regenerate the disputed shot.
        choice = client.director.conflict_choice(session_id, conflict_id=cid, option="honor_canon")
        print("resolution:", choice.status, "-", choice.reasoning)
    elif event.name == "regen_done":
        print("disputed shot re-rendered:", event["oss_url"])
        break
```

## Recipe: enumerate the API surface

```python
from kinora import ENDPOINTS
from kinora.spec import endpoints_by_tag

for tag, eps in endpoints_by_tag().items():
    print(f"# {tag}")
    for e in eps:
        auth = " (auth)" if e["auth"] else ""
        print(f"  {e['method']:6} /api{e['path']}{auth}")
```

## Recipe: read a page's karaoke word boxes

```python
page = client.books.page(book_id, 1)
for wb in page.word_boxes[:10]:
    x0, y0, x1, y1 = wb["bbox"]
    print(wb["word_index"], repr(wb["text"]), f"@({x0:.2f},{y0:.2f})")
```
