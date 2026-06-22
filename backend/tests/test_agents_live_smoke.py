"""LIVE agent smoke tests — real DashScope calls (TEXT ONLY). Skipped unless
``KINORA_LIVE_TESTS`` is set, so CI never runs them. Run locally with the key:

    export DASHSCOPE_API_KEY=$(grep '^DASHSCOPE_API_KEY=' .env | cut -d= -f2-)
    KINORA_LIVE_TESTS=1 .venv/bin/python -m pytest tests/test_agents_live_smoke.py -s -rA

These exercise the two cheapest, text-only agents on a tiny input: the Adapter on
a short public-domain Grimm paragraph, and the Cinematographer on a synthetic
canon slice. They NEVER call the Generator or any Wan video render.
"""

from __future__ import annotations

import os

import pytest

from app.agents.adapter import Adapter
from app.agents.cinematographer import Cinematographer
from app.agents.contracts import Beat, RenderMode, SourceSpan
from app.memory.interfaces import CanonEntitySlice, CanonSlice, RefImage
from app.providers import Providers, ResilienceConfig, create_providers

pytestmark = pytest.mark.skipif(
    not os.getenv("KINORA_LIVE_TESTS"),
    reason="live DashScope smoke tests; set KINORA_LIVE_TESTS=1 to run",
)

# A generous per-call timeout (page analysis can be a long JSON generation), with
# a couple of retries to ride out a transient endpoint disconnect.
_LIVE_RESILIENCE = ResilienceConfig(default_timeout_s=180.0, max_attempts=3)
# NOTE: the multi-beat json_object generation drives the qwen3 *thinking* models
# past the intl gateway's ~60s non-streaming first-byte budget, so the Adapter
# live call currently fails with a server disconnect on this endpoint. The flat
# single-object Cinematographer call returns in ~30s. The fix is streaming in the
# providers layer (out of this phase's scope); a single attempt keeps the stall
# from stacking 3×60s here.
_ADAPTER_RESILIENCE = ResilienceConfig(default_timeout_s=180.0, max_attempts=1)


def _live_providers() -> Providers:
    return create_providers(resilience=_LIVE_RESILIENCE)

# "The Frog King" (Brothers Grimm) — public domain. Three sentences.
_GRIMM = (
    "Close by the king's castle lay a great dark forest, and under an old lime-tree "
    "in the forest was a well. When the day was very warm, the king's youngest "
    "daughter went out into the forest and sat down beside the cool well. She took "
    "her golden ball, threw it up on high, and caught it, her favorite plaything."
)


async def test_live_adapter_returns_valid_beats() -> None:
    providers = create_providers(resilience=_ADAPTER_RESILIENCE)
    try:
        adapter = Adapter(providers)  # the configured qwen3.5-plus
        beats = await adapter.analyze_page(
            _GRIMM, page=1, scene_id="scene_001", max_tokens=1500
        )
        print(f"\n[ADAPTER] {len(beats)} beat(s) from {adapter.model}:")
        for beat in beats:
            print(
                f"  - {beat.beat_id}: {beat.summary!r}\n"
                f"      entities={beat.entities} unresolved={beat.unresolved_entities} "
                f"mood={beat.mood!r} span={beat.source_span.model_dump()}"
            )
        assert beats, "adapter returned no beats"
        assert all(b.summary for b in beats)
        assert all(b.beat_id.startswith("beat_") for b in beats)
        # Source spans should be present and well-formed.
        assert all(isinstance(b.source_span, SourceSpan) for b in beats)
    finally:
        await providers.aclose()


async def test_live_cinematographer_returns_valid_shot_spec() -> None:
    providers = _live_providers()
    try:
        cine = Cinematographer(providers)
        canon = CanonSlice(
            book_id="book_grimm_frog",
            beat_id="beat_0001",
            beat_index=1,
            scene_id="scene_001",
            characters=[
                CanonEntitySlice(
                    entity_key="char_princess",
                    type="character",
                    name="the youngest princess",
                    version=3,
                    description="a radiant young princess, golden hair, pale-blue gown",
                    reference_images=[RefImage(key="refs/princess/front.png", locked=True)],
                    valid_from_beat=1,
                )
            ],
            location=CanonEntitySlice(
                entity_key="loc_well",
                type="location",
                name="the forest well",
                version=1,
                description="an old stone well under a lime-tree in a dark forest",
                valid_from_beat=1,
            ),
            style=CanonEntitySlice(
                entity_key="style_book",
                type="style",
                name="painterly storybook",
                version=1,
                style_tokens={"palette": "warm gold and forest green", "lens": "35mm"},
                valid_from_beat=1,
            ),
        )
        beat = Beat(
            beat_id="beat_0001",
            scene_id="scene_001",
            beat_index=1,
            summary="The princess sits by the cool well and tosses her golden ball.",
            described_visuals="a princess by a stone well in a dark forest, golden ball in hand",
            mood="serene",
            source_span=SourceSpan(page=1, para=2, word_range=(60, 95)),
        )
        spec = await cine.design_shot(beat, canon)
        print(
            f"\n[CINEMATOGRAPHER] {cine.model} -> shot {spec.shot_id}\n"
            f"  render_mode={spec.render_mode.value}\n"
            f"  prompt={spec.prompt!r}\n"
            f"  negative_prompt={spec.negative_prompt!r}\n"
            f"  reference_image_ids={spec.reference_image_ids}\n"
            f"  camera={spec.camera.model_dump()} seed={spec.seed}"
        )
        assert spec.prompt, "cinematographer produced no prompt"
        assert isinstance(spec.render_mode, RenderMode)
        # A locked character is present and there is no prior endpoint -> ref-to-video.
        assert spec.render_mode is RenderMode.REFERENCE_TO_VIDEO
        # Any selected ref must be a locked candidate from the slice (never invented).
        assert set(spec.reference_image_ids) <= {"char_princess@v3"}
    finally:
        await providers.aclose()
