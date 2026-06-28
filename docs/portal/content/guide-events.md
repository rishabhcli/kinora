# Streaming events

The backend pushes live generation events over **Server-Sent Events** (and a
bidirectional WebSocket). The SDKs decode the SSE stream into typed events so you
can branch on `event.event` (TS) / `event.name` (Python).

## The event channel

`GET /api/sessions/{session_id}/events` is an SSE stream subscribed to the
session's (and its book's) channels. Each frame is `event: <name>` plus a JSON
`data:` payload. The stream also carries `:`-prefixed keepalive comments, which
the SDKs skip.

## Event catalog

| Event | Carries | Meaning |
|---|---|---|
| `buffer_state` | `committed_seconds_ahead`, `bursting`, `idle`, `budget_remaining_s` | one control tick's buffer state |
| `clip_ready` | `shot_id`, `oss_url`, `video_seconds` | a shot's clip is playable |
| `keyframe_ready` | `shot_id`, `beat_id`, `oss_url` | a speculative keyframe still |
| `scene_stitched` | `scene_id`, `oss_url`, `sync_map` | a scene stitched into one film |
| `event_stitched` | `event_id`, `oss_url`, `sync_map` | event-level film rollup |
| `agent_activity` | `agent`, `aspect`, `message`, `shot_id` | a crew agent did something visible |
| `regen_done` | `shot_id`, `oss_url`, `qa` | a targeted regeneration finished |
| `budget_low` | `budget_remaining_s`, `scope` | the video budget is running low |
| `conflict_choice` | `conflict_id`, `options`, `claim`, `canon_fact` | a continuity conflict to resolve |
| `ingest_progress` | `book_id`, `stage`, `pct` | Phase-A ingest progress |

The full field list per event is in the [API reference](api-reference.html#events).

## TypeScript — async iterator

Use the `isEvent` type guard to narrow each event to its typed payload:

```ts
import { isEvent } from "@kinora/sdk";

const controller = new AbortController();

for await (const ev of client.sessions.events(sessionId, { signal: controller.signal })) {
  if (isEvent(ev, "buffer_state")) {
    console.log("ahead", ev.committed_seconds_ahead, "bursting", ev.bursting);
  } else if (isEvent(ev, "clip_ready")) {
    playClip(ev.oss_url, ev.shot_id);
  } else if (isEvent(ev, "agent_activity")) {
    console.log(`[${ev.agent}] ${ev.message}`);
  } else if (isEvent(ev, "conflict_choice")) {
    surfaceConflict(ev.conflict_id, ev.options);
  } else {
    // An event name the SDK does not model yet — still delivered.
    console.debug("unmodelled event", ev.event);
  }
}
// Later: controller.abort() to stop the stream.
```

`isEvent(ev, "clip_ready")` narrows `ev` to `ClipReadyEvent`, so `ev.oss_url` is
typed `string`. Because the stream also delivers events the SDK doesn't model
yet (as a plain `{ event, ...fields }`), a newer backend never breaks your loop —
they fall through to the final `else`.

## TypeScript — callback API

```ts
const unsubscribe = client.sessions.subscribe(
  sessionId,
  (ev) => {
    if (ev.event === "clip_ready") playClip(ev.oss_url, ev.shot_id);
  },
  {
    onError: (err) => console.error("stream error", err),
    onClose: () => console.log("stream ended"),
  },
);
// unsubscribe() aborts the stream.
```

## Python — sync iterator

```python
for event in client.sessions.iter_events(session_id):
    if event.name == "buffer_state":
        print("ahead", event["committed_seconds_ahead"])
    elif event.name == "clip_ready":
        play_clip(event["oss_url"])
    elif event.name == "agent_activity":
        print(f"[{event['agent']}] {event['message']}")
```

## Python — async iterator

```python
async for event in client.sessions.iter_events(session_id):
    if event.name == "regen_done":
        print("regenerated", event["shot_id"], "->", event["oss_url"])
```

## Library progress stream

`GET /api/books/events` is a per-user SSE stream of `ingest_progress` while books
import — drive a shelf progress strip from it. (Use the raw transport or
`stream_lines` until a dedicated helper lands; the same decoder applies.)

## Auth on streams

SSE/WebSocket cannot set headers in the browser. The SDKs send the bearer header
by default (works server-side). If a proxy strips it from streaming responses,
also append `?token=`:

```ts
client.sessions.events(sessionId, { tokenInQuery: true });
```

```python
client.sessions.iter_events(session_id, token_in_query=True)
```

## WebSocket

`WS /api/ws/sessions/{session_id}` fans out the same events **and** accepts
client→backend messages (`intent_update`, `seek`, `comment`). The SDKs make SSE
first-class; for bidirectional control today, drive intent/seek/comment over the
REST endpoints (which is what the WS messages call internally) and consume events
over SSE.
