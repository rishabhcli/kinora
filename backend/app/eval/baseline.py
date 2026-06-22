"""The single-agent, no-memory, frame-chaining baseline — the §13 control arm.

This is the honest control the crew is measured against (§13): **one Qwen3.7-Max
call** designs each shot end-to-end with

* **no memory / no canon retrieval** — the model never sees a character bible,
  locked references, style tokens, or active continuity state; and
* **no critic loop** — there is no QA-driven regeneration; whatever the first
  pass produces is what ships.

Consistency therefore rests entirely on **pure frame-chaining**: each shot's
keyframe is conditioned *only* on the previous shot's frame (image-to-image),
which is exactly the drift-prone setup the whole architecture argues against. As
with the crew arm, ``KINORA_LIVE_VIDEO`` stays off so the artifact is a keyframe
still (image-gen) — **zero video-seconds** — and CCS/style are measured on it
against the same shared locked references the crew is scored against.

The arm satisfies the :class:`~app.eval.harness.Arm` protocol, so the §13 harness
runs it over the identical demo sequence as the crew. Its providers are injected
(``chat`` + ``image`` + ``embedder``), so tests drive it with light doubles.
"""

from __future__ import annotations

from typing import Any, Protocol

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.eval.harness import DemoSequence, DemoShot, SequenceRun, ShotOutcome
from app.eval.metrics import Vector
from app.memory.interfaces import Embedder

logger = get_logger("app.eval.baseline")

#: The control arm's system prompt: explicitly memoryless, single-agent (§13).
BASELINE_SYSTEM = (
    "You are a single, stateless video-generation assistant. You have NO memory "
    "of earlier shots, NO character bible or reference images, and NO continuity "
    "database. Working ONLY from this shot's text (and, if given, a short note "
    "about the previous frame), write one concise visual prompt for the next ~5 "
    "second clip. Reply with strict JSON and nothing else: {\"prompt\": \"<prompt>\"}."
)


class ChatLike(Protocol):
    """The slice of the chat provider the baseline uses (one JSON call)."""

    async def chat_json(
        self,
        messages: list[dict[str, Any]],
        model: str,
        *,
        temperature: float | None = ...,
        max_tokens: int | None = ...,
        stream: bool | None = ...,
    ) -> Any: ...


class ImageLike(Protocol):
    """The slice of the image provider the baseline uses (frame-chained gen)."""

    async def generate(
        self,
        prompt: str,
        *,
        n: int = ...,
        reference_images: list[bytes | str] | None = ...,
        seed: int | None = ...,
    ) -> list[bytes]: ...


class BaselineArm:
    """The §13 control arm: one LLM call + frame-chained keyframe, no memory."""

    name = "baseline"

    def __init__(
        self,
        *,
        chat: ChatLike,
        image: ImageLike,
        embedder: Embedder,
        settings: Settings | None = None,
        model: str | None = None,
    ) -> None:
        self._chat = chat
        self._image = image
        self._embedder = embedder
        self._settings = settings or get_settings()
        # The expensive single agent doing the whole job, per §13 ("one Qwen3.7-Max").
        self._model = model or self._settings.chat_model_max

    async def run_sequence(self, sequence: DemoSequence, run_index: int) -> SequenceRun:
        """Generate every shot by frame-chaining, with no memory and no critic."""
        outcomes: list[ShotOutcome] = []
        prev_frame_bytes: bytes | None = None
        prev_prompt: str | None = None
        for shot in sequence.shots:
            prompt = await self._design_prompt(shot, prev_prompt)
            keyframe = await self._render_keyframe(prompt, prev_frame_bytes, seed=shot.seed)
            crop_emb: Vector = (await self._embedder.embed_images([keyframe]))[0]
            # One still per shot: it is both the per-character crop and the style
            # sample (the baseline has no separate locked references to draw on).
            crops = {char_key: list(crop_emb) for char_key in shot.character_keys}
            outcomes.append(
                ShotOutcome(
                    shot_id=shot.shot_id,
                    scene_id=shot.scene_id,
                    est_duration_s=shot.est_duration_s,
                    style_embedding=list(crop_emb),
                    character_crops=crops,
                )
            )
            prev_frame_bytes = keyframe  # pure frame-chaining: the only conditioning
            prev_prompt = prompt
        return SequenceRun(arm=self.name, outcomes=outcomes)

    async def _design_prompt(self, shot: DemoShot, prev_prompt: str | None) -> str:
        """One memoryless LLM call to design the shot (falls back to the fixed prompt)."""
        user = shot.prompt
        if prev_prompt is not None:
            user = f"{shot.prompt}\n\n[previous frame depicted: {prev_prompt}]"
        messages = [
            {"role": "system", "content": BASELINE_SYSTEM},
            {"role": "user", "content": user},
        ]
        try:
            raw = await self._chat.chat_json(
                messages, self._model, temperature=0.0, max_tokens=300, stream=False
            )
        except Exception as exc:  # noqa: BLE001 - a control arm must still produce a shot
            logger.warning("baseline.design_failed", shot_id=shot.shot_id, error=str(exc))
            return shot.prompt
        if isinstance(raw, dict):
            prompt = raw.get("prompt")
            if isinstance(prompt, str) and prompt.strip():
                return prompt.strip()
        return shot.prompt

    async def _render_keyframe(
        self, prompt: str, prev_frame: bytes | None, *, seed: int
    ) -> bytes:
        """Render one keyframe still, conditioned ONLY on the previous frame (§13).

        No canon/locked references are ever passed — the previous shot's frame is
        the sole visual conditioning (pure frame-chaining). Zero video-seconds.
        """
        references: list[bytes | str] | None = (
            [prev_frame] if prev_frame is not None else None
        )
        images = await self._image.generate(
            prompt, n=1, reference_images=references, seed=seed
        )
        if not images:
            raise RuntimeError("baseline image generation returned no keyframe")
        return images[0]


__all__ = ["BASELINE_SYSTEM", "BaselineArm", "ChatLike", "ImageLike"]
