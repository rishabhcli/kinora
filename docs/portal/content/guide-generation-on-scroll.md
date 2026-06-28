# Generation-on-scroll

Kinora's core consumption model. A reader *dwells*: a page of ~250 words takes
45–90 seconds to read but maps to only ~8–15 seconds of video. That asymmetry is
the whole trick — the backend is racing **reading speed**, not real-time
playback, and reading is slow.

## Three zones

The forward path ahead of the reader is split into three zones:

| Zone | ETA window | What exists | Video budget |
|---|---|---|---|
| Committed | 0 – ~45s | Full video, QA-passed, narrated, cached, playable | spends video-seconds |
| Speculative | ~45 – ~240s | One keyframe still per beat (image-gen, not video) | ~zero |
| Cold | > 240s | Plan + canon only (text analysed at import) | free |

A **dual-watermark buffer with hysteresis** (low ~25s, high ~75s of committed
video ahead) makes generation bursty and event-driven: it fills to the high
mark, then idles until the buffer drains.

## Driving it: sessions + intent

You open a **session** against a book, then post **intent updates** — where the
reader's focus word is and how fast they are reading (words/sec). The scheduler
runs one control tick per update: promoting committed shots and maintaining
keyframes across the speculative horizon.

```python
from kinora import KinoraClient

with KinoraClient("http://localhost:8000") as client:
    client.auth.login("demo@kinora.local", "demo-password-123")
    session = client.sessions.create(book_id, focus_word=0, mode="viewer")

    # As the reader scrolls, debounce + post their position and velocity.
    result = client.sessions.intent(session.session_id, focus_word=240, velocity=4.5)
    print("promoted shots:", result.promoted)
    print("committed seconds ahead:", result.committed_seconds_ahead)
    print("bursting:", result.bursting, "idle:", result.idle)
```

```ts
const result = await client.sessions.intent(session.session_id, {
  focus_word: 240,
  velocity: 4.5,
});
console.log(result.promoted, result.committed_seconds_ahead);
```

`intent` is debounced server-side and **idempotent** w.r.t. a control tick, so
the SDKs treat it as retry-safe.

## Seeking

A jump (fast scroll / page skip) is a **seek**: it cancels distant speculative
work, bridges with a keyframe, and re-seeds the buffer at the new position.

```python
seek = client.sessions.seek(session.session_id, word=5000)
print("cancelled jobs:", seek.cancelled, "bridge beat:", seek.bridge_beat)
```

```ts
const seek = await client.sessions.seek(session.session_id, { word: 5000 });
```

## Reading the live state

`GET /sessions/{id}` returns the scheduler's current control state — focus word,
velocity, committed seconds ahead, whether it is bursting, and the remaining
budget:

```python
state = client.sessions.get(session.session_id)
print(state.committed_seconds_ahead, state.budget_remaining_s)
```

## Watching it without spending video

The eval endpoint **recomputes the watermark sawtooth** for a session by driving
the real scheduler over the book's source-span index — zero video-seconds. Great
for visualising the buffer behaviour:

```python
trace = client.eval.buffer_trace(session.session_id, velocity=5.0, duration_s=120)
for point in trace[:5]:
    print(point.t, point.committed_seconds_ahead, point.low, point.high)
```

As shots become playable, the backend emits `clip_ready` / `buffer_state`
events — see [Streaming events](guide-events.html).
