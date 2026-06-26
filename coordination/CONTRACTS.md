# Cross-agent contracts

> Append-only. Each agent owns its own section; Agent 12 integrates. Do not edit
> another agent's section — add a request in `coordination/requests/agent-XX.md`.

---

## Agent 01 — Event Director, Stitching & Video Generation

**Owns:** `backend/app/render/{event_director,stitch,continuity_qa,shot_grammar,degrade,pipeline,states,conflict}.py`.
**Publishes to Agent 3** (who exposes it over HTTP): the **event film** (one vertical
720×1280 mp4) + its **event sync map**. Agent 1 produces the data model + the bytes;
Agent 3 owns the routes/serialization (`routes/films.py`, `lib/api/films.ts`).

### Geometry (hard invariant)

Kinora films are **vertical 720×1280** (short-drama format) end-to-end — `degrade.FILM_SIZE`.
`stitch.concat_clips` enforces it (any source is scaled+padded into vertical, never
leaked landscape), and the per-shot degrade lane renders at it too. **No resolution
jump mid-event.** Do not upscale to 4K; do not stitch to 1080p landscape.

### Data model (source of truth: `app/render/event_director.py`)

An **event** is a beat-cluster (3–6 shots) rendered as ONE continuous film.

```jsonc
// EventScript — the plan (pydantic: EventScript)
{
  "event_id": "bridge_chase",
  "book_id": "demo_book",
  "scene_id": "scene_005",
  "shots": [                       // EventShot[], 3–6
    {
      "shot_id": "bridge_chase_shot_00",
      "beat_id": "b0",
      "ordinal": 0,
      "render_mode": "reference_to_video",   // §9.3; chains reference→continuation→first_last_frame
      "summary": "A wide stone bridge at dusk…",
      "camera": { "move": "push_in", "speed": "slow", "shot_size": "wide" },
      "duration_s": 6.25,          // decided per-beat (3–8s), NOT a global constant
      "source_span": { "page": 12, "para": null, "word_range": [100, 140] },
      "directive": {               // ContinuityDirective
        "wardrobe": "a rain-dark travelling coat",
        "setting": "a fog-wrapped stone bridge…",
        "lighting": "low-key",
        "time_of_day": "dusk",
        "camera_logic": "establishing",
        "screen_direction": "neutral",   // 180°-rule axis
        "motion_reversal": false,        // true ⇒ a motivated line cross (not a 180° error)
        "hand_off": "end on: fog rolling low over the water",
        "continues_from_shot_id": null,  // shot N>0 ⇒ previous shot_id; shot 0 ⇒ prior event endpoint
        "last_frame_key": null           // lastframes/{book}/{shot}.png — the continuation anchor
      }
    }
  ]
}
```

```jsonc
// SceneSyncMap — the merged event sync map (pydantic: stitch.SceneSyncMap; §9.4/§9.6)
// scene_id carries the event_id when produced by the Event Director.
{
  "scene_id": "bridge_chase",
  "duration_s": 14.81,             // == last segment.video_end_s (exact)
  "segments": [                    // one per shot, cumulative timecodes
    {
      "shot_id": "bridge_chase_shot_00",
      "video_start_s": 0.0,
      "video_end_s": 6.25,
      "page": 12,
      "page_turn_at_s": 6.0,       // flip slightly before the shot ends
      "words": [                   // SyncWord[] — karaoke highlight (empty when no TTS)
        { "word_index": 100, "text": "She", "t_start": 0.1, "t_end": 0.32, "bbox": [0.12,0.34,0.04,0.02] }
      ]
    }
  ]
}
```

**Crossfade note:** the event stitch dissolves each seam (`EventDirector` default 0.4s,
clamped to ≤45% of the shortest clip). The sync map is merged on the **same** overlap
(`merge_sync_segments(..., overlap_s=...)`), so shot N+1's `video_start_s` is pulled
`overlap_s` earlier and the map's timeline matches the played film to within a frame.

### Producer API (Python, for Agent 3 / Agent 12 to call)

```python
from app.render.event_director import EventDirector, plan_event_script

script = plan_event_script(event_id=…, book_id=…, scene_id=…, beats=[…], canon=slice)
result = await EventDirector(store=object_store).render_event(
    script, stills={shot_id: png}, audio={shot_id: wav}, page_boxes=…, word_timestamps=…
)
# result: EventRenderResult(clip_bytes, clip_key, clip_url, sync_map, duration_s,
#                           shot_count, last_frame_keys, continuity)
```

- Persisted object keys: film → `clips/{book}/{event_id}.mp4` (`keys.clip`); per-shot
  anchors → `lastframes/{book}/{shot}.png` (`keys.lastframe`).
- `result.continuity` is an `EventContinuityReport` (`continuity_qa`): `ok`, `score`,
  `action ∈ {accept, insert_supplemental, regen_continuation, degrade}`, per-seam scores.

### Suggested HTTP shape (Agent 3 implements; not built by Agent 1)

- `GET /books/{book_id}/events/{event_id}/film` → `{ clip_url, duration_s, sync_map }`
  (presign `clip_key`; return `sync_map` verbatim — it is already client-ready).
- The client transitions from per-shot playback to the stitched event film exactly as it
  does for `scene_stitched` today (§9.6); the event sync map is the same shape as the
  scene sync map, so the SyncEngine needs no new code path.

### Live artifact

`coordination/artifacts/agent-01/` — a real 720×1280 event film + its sync map +
script + continuity report, regenerable offline via `generate_event_demo.py`.
