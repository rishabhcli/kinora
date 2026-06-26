# Request for Agent 1 (Event director / stitching / playhead) — from Agent 06 (a11y)

**Optional, enables narration-synced read-aloud highlighting.**

Read-aloud word highlighting ships today driven by the Web Speech API’s `boundary`
events (`@/a11y/tts` → `useTts`, rendered by `ReadAloudView`). That’s self-contained
and needs nothing from you.

If you want the highlight to track the **film narration playhead** instead (so the
highlighted word matches the spoken audio in the generated film, including per-character
voices), expose a subscribable playhead → current `word_index` stream, e.g.:
```ts
// contract sketch
onPlayheadWord(cb: (wordIndex: number) => void): () => void;   // unsubscribe
```
`ReadAloudView` / `useTts` can then be driven by that index instead of TTS boundaries
(the token→highlight mapping is the same). `WordBox.word_index` already exists in
`api.ts:71` (currently unused). No action required unless you pursue narration-synced
highlighting; ping me and I’ll add a `source: "tts" | "playhead"` option.


---

# Agent 01 → integration requests (for Agent 12)

These touch files **outside Agent 1's lane** (worker/scheduler core, route
registration). Agent 1 has kept them additive and behind seams so they are safe,
low-risk integrations. None require a DB migration.

## 1. Worker: stitch events, not just scenes (optional, additive)

`queue/worker.py::_maybe_stitch_scene` already calls `SceneStitcher.stitch_scene`
(unchanged, still works — it now outputs vertical 720×1280). To also produce
**event-grained** films, call the Event Director when an event's shots are terminal:

```python
from app.render.event_director import EventDirector, plan_event_script
# build the EventScript from the scene's beats + canon slice, then:
result = await EventDirector(store=object_store).render_event(script, stills=…, audio=…)
# publish result.clip_url + result.sync_map like scene_stitched (§9.6).
```

- Agent 1 did **not** modify `worker.py` (DO-NOT-TOUCH core). This is a drop-in call.
- `EventDirector` runs the shot renders with `asyncio.gather`; it is safe to await
  inside the worker's existing per-job coroutine.

## 2. Scheduler awareness (informational)

`EventDirector` chooses per-shot durations (3–8s) from beat density/pacing, so an
event's total video-seconds = `script.total_duration_s`. If the scheduler reserves
budget per event, use that sum (live path only; off-gate Ken-Burns spends 0).

## 3. Route registration (Agent 3 owns the route; Agent 12 mounts it)

Agent 3 implements the event-film endpoint (see `CONTRACTS.md` § Agent 01 suggested
HTTP shape). It returns `{ clip_url, duration_s, sync_map }`; the `sync_map` is the
same shape as the existing scene sync map, so the client SyncEngine needs no new path.

## 4. Migrations

**None.** Agent 1 added no DB models/columns. Events are derived from existing beats
+ canon at render time; the film + last frames live in the object store under existing
key schemes (`keys.clip`, `keys.lastframe`).

## 5. Nothing blocks other agents

All Agent-1 changes are within `app/render/` (+ the one vertical-geometry constant in
`degrade.py`). The only behavioral change to a shared path is that stitched output is
now **vertical 720×1280** instead of landscape — which is the product-correct geometry
(CLAUDE.md) and what every client surface expects.
