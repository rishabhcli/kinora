# Uploading books

A book starts as a **PDF or EPUB** upload. The backend validates it, normalises
an EPUB to PDF, stores it, and triggers **Phase-A ingest** out of band: it
extracts pages + per-word boxes, runs page analysis, builds the versioned canon,
plans the shot list, and identity-locks keyframes + voices. The book moves from
`importing` to `ready`.

## Upload

The upload is a `multipart/form-data` POST with the file and optional
`title` / `author` / `art_direction`.

```python
with open("my-novel.pdf", "rb") as f:
    data = f.read()

book = client.books.upload(
    data,
    filename="my-novel.pdf",
    title="My Novel",
    author="A. Writer",
    art_direction="noir, high contrast, rain-soaked streets",
)
print(book.id, book.status)  # importing
```

```ts
// Browser: a File/Blob from an <input type="file">.
const book = await client.books.upload(file, {
  title: "My Novel",
  art_direction: "noir, high contrast",
});

// Node: a Uint8Array/ArrayBuffer + a filename.
import { readFile } from "node:fs/promises";
const bytes = await readFile("my-novel.pdf");
const book2 = await client.books.upload(bytes, { filename: "my-novel.pdf" });
```

### Limits

- Accepted: PDF and EPUB (detected by content magic, not the content-type).
- Size and page caps apply; an oversized upload returns `413` (`UploadError`),
  an unsupported file `415`.
- A per-user book quota returns `429` (`book_quota_exceeded`).

## Tracking ingest progress

### Poll

`get(book_id)` carries `status`, `progress` (0–1), and a `stage` label. The SDKs
provide a `wait_until_ready` / `waitUntilReady` helper:

```python
ready = client.books.wait_until_ready(book.id, interval_s=2.0, timeout_s=600)
print(ready.num_pages, "pages ready")
```

```ts
const ready = await client.books.waitUntilReady(book.id, {
  intervalMs: 2000,
  onProgress: (b) => console.log(b.stage, b.progress),
});
```

It raises on a `failed` ingest or a timeout.

### Stream

For a live shelf, subscribe to the per-user library stream
(`GET /api/books/events`) for `ingest_progress` events instead of polling. The
same SSE decoder powers it.

## Reading what ingest produced

Once `ready`:

```python
# A page: presigned image URL, text, and per-word boxes (for karaoke sync).
page = client.books.page(book.id, 1)
print(page.image_url, len(page.word_boxes), "word boxes")

# The canon graph: entities, continuity facts, and the markdown vault.
canon = client.books.canon(book.id)
print([e.name for e in canon.entities])

# The shot timeline.
shots = client.books.shots(book.id)
print(len(shots), "shots;", sum(1 for s in shots if s.status == "accepted"), "accepted")

# Stitched event/scene films + sync maps + open-book restore state.
films = client.films.events(book.id)
for ev in films.events:
    print(ev.event_id, ev.stitched, ev.duration_s)
```

```ts
const page = await client.books.page(book.id, 1);
const canon = await client.books.canon(book.id);
const shots = await client.books.shots(book.id);
const films = await client.films.events(book.id);
```

## Covers

`GET /api/books/{id}/cover` 302-redirects to the presigned cover image (when the
book has one). The shelf list (`GET /api/books`) also carries a presigned
`cover_url` per book.
