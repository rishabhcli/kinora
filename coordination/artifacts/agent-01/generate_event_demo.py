"""Generate Agent-01 artifacts: a real vertical event film + its sync map.

Reproduces the "chase across the bridge" event end-to-end on the off-gate
Ken-Burns lane (zero video-seconds): plans a 3-shot event script, renders the
shots concurrently at 720×1280, stitches ONE crossfaded vertical mp4, and writes
the film + the merged sync map + the event script + the continuity report next to
this file.

Run (no infra, no model, no spend):
    cd backend && .venv/bin/python ../coordination/artifacts/agent-01/generate_event_demo.py
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import struct
import wave
from pathlib import Path

from PIL import Image

from app.agents.contracts import Beat, SourceSpan
from app.memory.interfaces import (
    CanonEntitySlice,
    CanonSlice,
    RefImage,
    StateSlice,
)
from app.render import degrade
from app.render.event_director import EventDirector, plan_event_script

OUT = Path(__file__).resolve().parent
BOOK_ID = "demo_book"


def vertical_png(seed: int) -> bytes:
    """A distinct vertical 720×1280 gradient still per shot (so seams are visible)."""
    w, h = degrade.FILM_SIZE
    img = Image.new("RGB", (w, h))
    px = img.load()
    assert px is not None
    for y in range(h):
        for x in range(w):
            px[x, y] = ((x * 255) // w, (y * 255) // h, (seed * 60) % 256)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def tone(duration_s: float, freq: float) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        frames = bytearray()
        for n in range(int(24000 * duration_s)):
            frames += struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * n / 24000)))
        w.writeframes(bytes(frames))
    return buf.getvalue()


def bridge_beats() -> list[Beat]:
    return [
        Beat(
            beat_id="b0",
            scene_id="scene_005",
            beat_index=0,
            summary="A wide stone bridge at dusk, fog rolling low over the water.",
            mood="calm, still",
            source_span=SourceSpan(page=12, word_range=(100, 140)),
        ),
        Beat(
            beat_id="b1",
            scene_id="scene_005",
            beat_index=1,
            summary=(
                "She sprints across the planks, boots pounding, breath ragged, the "
                "pursuers gaining behind her in a frantic, headlong chase."
            ),
            mood="tense chase",
            source_span=SourceSpan(page=12, word_range=(141, 180)),
        ),
        Beat(
            beat_id="b2",
            scene_id="scene_005",
            beat_index=2,
            summary="She reaches the far side and turns to face them.",
            mood="resolute",
            source_span=SourceSpan(page=13, word_range=(181, 205)),
        ),
    ]


def demo_canon() -> CanonSlice:
    """A locked-reference character + a setting + style, so directives are populated."""
    return CanonSlice(
        book_id=BOOK_ID,
        beat_id="b0",
        beat_index=0,
        scene_id="scene_005",
        characters=[
            CanonEntitySlice(
                entity_key="runner",
                type="character",
                name="The Runner",
                version=1,
                appearance={"wardrobe": "a rain-dark travelling coat"},
                reference_images=[RefImage(key="refs/demo/runner/front.png", locked=True)],
                valid_from_beat=1,
            )
        ],
        location=CanonEntitySlice(
            entity_key="bridge",
            type="location",
            name="The Old Stone Bridge",
            version=1,
            description="a fog-wrapped stone bridge over black water at dusk",
            valid_from_beat=1,
        ),
        style=CanonEntitySlice(
            entity_key="style",
            type="style",
            name="Storybook noir",
            version=1,
            style_tokens={"lighting": "low-key", "palette": "cool", "time_of_day": "dusk"},
            valid_from_beat=1,
        ),
        active_states=[
            StateSlice(
                state_id="s1",
                subject_entity_key="runner",
                predicate="wears",
                object_value="a rain-dark travelling coat",
                valid_from_beat=1,
            )
        ],
    )


class _MemStore:
    """Tiny in-memory BlobStore so the demo persists without MinIO."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.store[key] = data

    def get_bytes(self, key: str) -> bytes:
        return self.store[key]

    def exists(self, key: str) -> bool:
        return key in self.store

    def presigned_get_url(self, key: str, ttl: int = 3600) -> str:
        return f"memory://{key}"


async def main() -> None:
    script = plan_event_script(
        event_id="bridge_chase",
        book_id=BOOK_ID,
        scene_id="scene_005",
        beats=bridge_beats(),
        canon=demo_canon(),
    )
    stills = {s.shot_id: vertical_png(i + 1) for i, s in enumerate(script.shots)}
    audio = {s.shot_id: tone(s.duration_s, 200 + 40 * i) for i, s in enumerate(script.shots)}

    store = _MemStore()
    result = await EventDirector(store=store).render_event(script, stills=stills, audio=audio)

    (OUT / "bridge_event.mp4").write_bytes(result.clip_bytes)
    (OUT / "bridge_event.script.json").write_text(script.model_dump_json(indent=2))
    (OUT / "bridge_event.sync_map.json").write_text(result.sync_map.model_dump_json(indent=2))
    continuity = result.continuity
    (OUT / "bridge_event.continuity.json").write_text(
        json.dumps(
            {
                "ok": continuity.ok,
                "action": continuity.action.value,
                "score": continuity.score,
                "geometry_uniform": continuity.geometry_uniform,
                "duration_ok": continuity.duration_ok,
                "seams": [
                    {
                        "from": s.from_shot_id,
                        "to": s.to_shot_id,
                        "score": s.score,
                        "ok": s.ok,
                    }
                    for s in continuity.seams
                ],
            },
            indent=2,
        )
        if continuity is not None
        else "{}"
    )

    info = degrade.probe(result.clip_bytes)
    print(f"event film : {info.width}x{info.height}, {info.duration_s:.2f}s, {len(result.clip_bytes)} bytes")
    print(f"shots      : {result.shot_count}  modes={[s.render_mode.value for s in script.shots]}")
    print(f"sync map   : {len(result.sync_map.segments)} segments, duration={result.sync_map.duration_s}s")
    print(f"continuity : ok={continuity.ok if continuity else 'n/a'}")
    print(f"last frames: {sorted(result.last_frame_keys.values())}")


if __name__ == "__main__":
    asyncio.run(main())
