"""The keyframe fallback — bake locked identity into a frame, then drive i2v.

When a backend declares :data:`~app.video.identity.capabilities.ConditioningKind.NONE`
(a pure text-to-video model accepts no reference), identity can still be preserved:
*synthesise a keyframe that bakes the locked appearance* (image-gen conditioned on
the locked refs, exactly the §6 "lock identity" image-gen step), then drive the
shot via image-to-video (or first-last-frame) from that frame. The clip inherits
the keyframe's pinned identity even though the video model itself knows nothing
about the character.

This module owns:

* :class:`KeyframeBaker` — the *injectable* image-gen seam (a narrow slice of
  ``app.providers.image.ImageProvider.generate``). No provider import, so the
  fallback is unit-testable with a deterministic fake;
* :class:`KeyframeFallback` — orchestrates "select best locked ref → bake → return
  a :class:`BakedKeyframe`" and reports the *re-routed* render mode the conditioner
  should emit. If no image-gen seam is wired, it degrades to *reusing the best
  locked ref directly as the first frame* (no spend) so the path never hard-fails.

Spend safety: baking calls image-gen, which is independent of ``KINORA_LIVE_VIDEO``
(ingest already pays for identity-lock keyframes). Nothing here ever touches the
video spend gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.core.logging import get_logger

from .bundle import IdentityBundle, LockedReference, Pose
from .selection import SelectionPolicy, select_best, select_top

logger = get_logger("app.video.identity.keyframe")


# --------------------------------------------------------------------------- #
# The image-gen seam
# --------------------------------------------------------------------------- #


@runtime_checkable
class KeyframeBaker(Protocol):
    """The narrow image-gen capability the fallback needs (injectable seam).

    Satisfied by ``app.providers.image.ImageProvider`` (its ``generate`` is a
    superset) and by a deterministic test double. Returns raw image bytes.
    """

    async def generate(
        self,
        prompt: str,
        *,
        reference_images: list[bytes] | None = None,
        negative_prompt: str | None = None,
        seed: int | None = None,
    ) -> list[bytes]: ...


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #


class KeyframeSource(StrEnum):
    """How the fallback obtained the driving keyframe."""

    #: Freshly synthesised by image-gen conditioned on the locked refs.
    BAKED = "baked"
    #: Reused an existing locked ref directly (no baker wired, or no ref bytes to
    #: condition on but a usable locked ref existed).
    REUSED_REFERENCE = "reused_reference"


@dataclass(frozen=True, slots=True)
class BakedKeyframe:
    """A keyframe that bakes one entity's locked identity, ready to drive i2v.

    Attributes:
        entity_key: The entity whose identity this frame pins.
        image_bytes: The frame's raw pixels (always present — drives i2v).
        source: How the frame was obtained (:class:`KeyframeSource`).
        pose: The pose the frame depicts (carried for the self-check / telemetry).
        seed: The image-gen seed used (None when a ref was reused).
        prompt: The bake prompt (empty when a ref was reused).
        source_ref_ids: The locked ref ids this frame was conditioned on / reused.
    """

    entity_key: str
    image_bytes: bytes
    source: KeyframeSource
    pose: Pose = Pose.FRONT
    seed: int | None = None
    prompt: str = ""
    source_ref_ids: tuple[str, ...] = ()

    def as_locked_reference(self) -> LockedReference:
        """Wrap the baked frame as a (non-locked) :class:`LockedReference`.

        ``locked=False`` because a baked/derived frame is *not* canon — it is a
        per-shot driving frame, distinct from the entity's locked truth.
        """
        return LockedReference(
            ref_id=f"{self.entity_key}:keyframe:{self.pose.value}",
            pose=self.pose,
            image_bytes=self.image_bytes,
            locked=False,
            mime="image/png",
        )


# --------------------------------------------------------------------------- #
# Bake-prompt assembly
# --------------------------------------------------------------------------- #


def build_bake_prompt(bundle: IdentityBundle, shot_prompt: str, *, pose: Pose) -> str:
    """Compose the image-gen prompt that bakes the locked identity (pure).

    The locked appearance phrase leads (it is the identity anchor), then the shot's
    own description, then an explicit pose cue so the synthesised frame matches the
    shot's framing — maximising downstream i2v consistency.
    """
    parts: list[str] = []
    if bundle.appearance_prompt:
        parts.append(bundle.appearance_prompt.strip())
    elif bundle.name:
        parts.append(bundle.name.strip())
    shot = shot_prompt.strip()
    if shot:
        parts.append(shot)
    if pose is not Pose.UNKNOWN:
        parts.append(f"{pose.value} view")
    return ", ".join(p for p in parts if p)


# --------------------------------------------------------------------------- #
# The fallback
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FallbackConfig:
    """Tunables for :class:`KeyframeFallback` (deterministic; no env reads)."""

    #: How many locked refs to feed the baker as conditioning (capped by what the
    #: entity actually has). More refs → stronger pinning but more tokens.
    max_conditioning_refs: int = 3
    #: Selection policy for choosing/ranking conditioning refs.
    selection: SelectionPolicy | None = None


class KeyframeFallback:
    """Bake (or reuse) a keyframe that pins one entity's locked identity.

    Stateless except for the injected baker + config; safe to share. Never raises
    for "no baker" — it degrades to reusing a locked ref so a t2v-only backend
    still gets an identity-bearing first frame.
    """

    def __init__(
        self,
        *,
        baker: KeyframeBaker | None = None,
        config: FallbackConfig | None = None,
    ) -> None:
        self._baker = baker
        self._config = config or FallbackConfig()

    @property
    def can_bake(self) -> bool:
        """True when an image-gen seam is wired (else only ref-reuse is possible)."""
        return self._baker is not None

    async def keyframe_for(
        self,
        bundle: IdentityBundle,
        *,
        shot_prompt: str = "",
        desired_pose: Pose = Pose.FRONT,
        seed: int | None = None,
    ) -> BakedKeyframe | None:
        """Produce a driving keyframe pinning ``bundle``'s identity (``None`` if impossible).

        Strategy:

        1. If a baker is wired, synthesise a frame conditioned on the top locked
           refs (bytes when available, else the baker may fetch refs by URL — but
           our seam takes bytes, so we pass only refs that carry bytes).
        2. Otherwise (or if the bake yields nothing) reuse the best locked ref that
           *carries bytes* directly as the first frame.
        3. If neither is possible (no refs with bytes, no baker), return ``None`` —
           the caller must fall back to URL refs or a text-only render.
        """
        pol = self._config.selection
        if self.can_bake:
            baked = await self._bake(
                bundle, shot_prompt=shot_prompt, desired_pose=desired_pose, seed=seed
            )
            if baked is not None:
                return baked
        # Reuse path: pick the best locked ref that carries pixel bytes.
        ref = select_best(
            bundle, desired_pose=desired_pose, policy=pol, require_bytes=True
        )
        if ref is None or ref.image_bytes is None:
            logger.info(
                "identity.keyframe.no_byte_ref",
                entity=bundle.entity_key,
                can_bake=self.can_bake,
            )
            return None
        logger.info(
            "identity.keyframe.reused_reference",
            entity=bundle.entity_key,
            ref_id=ref.ref_id,
        )
        return BakedKeyframe(
            entity_key=bundle.entity_key,
            image_bytes=ref.image_bytes,
            source=KeyframeSource.REUSED_REFERENCE,
            pose=ref.pose if ref.pose is not Pose.UNKNOWN else desired_pose,
            source_ref_ids=(ref.ref_id,),
        )

    async def _bake(
        self,
        bundle: IdentityBundle,
        *,
        shot_prompt: str,
        desired_pose: Pose,
        seed: int | None,
    ) -> BakedKeyframe | None:
        assert self._baker is not None
        pol = self._config.selection
        conditioning = select_top(
            bundle,
            k=self._config.max_conditioning_refs,
            desired_pose=desired_pose,
            policy=pol,
            require_bytes=True,
        )
        ref_bytes = [r.image_bytes for r in conditioning if r.image_bytes is not None]
        prompt = build_bake_prompt(bundle, shot_prompt, pose=desired_pose)
        try:
            images = await self._baker.generate(
                prompt,
                reference_images=ref_bytes or None,
                negative_prompt=", ".join(bundle.negative_tokens) or None,
                seed=seed,
            )
        except Exception:  # noqa: BLE001 — bake failure degrades to ref-reuse, never sinks the shot
            logger.warning("identity.keyframe.bake_failed", entity=bundle.entity_key)
            return None
        if not images or not images[0]:
            return None
        logger.info(
            "identity.keyframe.baked",
            entity=bundle.entity_key,
            conditioning_refs=len(ref_bytes),
            pose=desired_pose.value,
        )
        return BakedKeyframe(
            entity_key=bundle.entity_key,
            image_bytes=images[0],
            source=KeyframeSource.BAKED,
            pose=desired_pose,
            seed=seed,
            prompt=prompt,
            source_ref_ids=tuple(r.ref_id for r in conditioning),
        )


__all__ = [
    "BakedKeyframe",
    "FallbackConfig",
    "KeyframeBaker",
    "KeyframeFallback",
    "KeyframeSource",
    "build_bake_prompt",
]
