# Agent 01 artifacts — a real event film, built offline

The "chase across the bridge" event, planned and rendered end-to-end on the
off-gate Ken-Burns lane (zero video-seconds, no model, no network).

| File | What |
|---|---|
| `bridge_event.mp4` | The stitched event film — **720×1280 vertical**, ~14.9s, 3 shots crossfaded into one continuous clip. |
| `bridge_event.script.json` | The `EventScript`: 3 shots, per-beat durations (6.25 / 4.8 / 4.56s — not constant), §9.3 modes `reference_to_video → video_continuation → first_last_frame`, continuity directives (wardrobe/setting/lighting/time-of-day, screen direction, last-frame hand-off). |
| `bridge_event.sync_map.json` | The merged event sync map — cumulative, crossfade-aware timecodes; `duration_s` == last `video_end_s`. |
| `bridge_event.continuity.json` | The deterministic continuity QA verdict (`ok: true`, `action: accept`, per-seam scores). |
| `generate_event_demo.py` | Reproducer. |

## Regenerate

```bash
cd backend && .venv/bin/python ../coordination/artifacts/agent-01/generate_event_demo.py
```

Expected console summary:

```
event film : 720x1280, ~14.9s
shots      : 3  modes=['reference_to_video', 'video_continuation', 'first_last_frame']
sync map   : 3 segments
continuity : ok=True
```

## Verify the film geometry

```bash
ffprobe -v error -select_streams v:0 -show_entries stream=width,height \
  -of csv=p=0 coordination/artifacts/agent-01/bridge_event.mp4
# → 720,1280
```
