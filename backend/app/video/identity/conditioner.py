"""The :class:`IdentityConditioner` — normalize locked identity across providers.

This is the subsystem's keystone. Given:

* the locked :class:`~app.video.identity.bundle.IdentityBundle` (per-entity truth),
* a backend's :class:`~app.video.identity.capabilities.CapabilityProfile`
  (what it can ingest), and
* the shot's framing intent (desired pose) + chosen render mode,

it **chooses the best conditioning strategy** the backend supports and emits the
**provider-appropriate fields** — so a character looks the same whether the shot
renders on Wan reference-to-video (a locked ref set), MiniMax (a single inline
first frame), or a pure text-to-video model (where it bakes a keyframe and re-routes
to image-to-video). It returns a :class:`ConditioningPlan` whose
:meth:`ConditioningPlan.apply_to` mutates a :class:`~app.providers.types.WanSpec`
in place, keeping all coupling to the provider layer in one small method.

Strategy ladder (kinora.md §9.3 render-mode tree + §6 identity lock), highest
fidelity first, gated by capability:

1. **REFERENCE_SET** — hand over the top-``k`` locked refs (Wan r2v). Strongest.
2. **CHARACTER_ID** — pass the registered subject/IP-Adapter token.
3. **INLINE_IMAGE** — the best locked ref's bytes inline (base64 / data-uri).
4. **FIRST_FRAME / FIRST_LAST_FRAME** — the best locked ref as the start frame.
5. **Keyframe fallback** — when the backend takes nothing usable (t2v-only), bake
   a keyframe that pins identity and *re-route the mode* to image-to-video.

The conditioner always injects the locked appearance phrase into the prompt and
anti-drift negatives (when the backend honours them), so even the weakest path
gets textual identity reinforcement.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.providers.types import WanMode, WanSpec

from .bundle import IdentityBundle, LockedReference, Pose
from .capabilities import (
    CapabilityProfile,
    ConditioningKind,
    ImageTransport,
)
from .keyframe import BakedKeyframe, KeyframeFallback
from .selection import SelectionPolicy, select_best, select_top

logger = get_logger("app.video.identity.conditioner")


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ConditioningPlan:
    """The resolved, provider-appropriate identity fields for one shot.

    Produced by :meth:`IdentityConditioner.plan`; applied to a
    :class:`~app.providers.types.WanSpec` by :meth:`apply_to`. Inspectable so tests
    assert the *decision* (which kind, which refs) independent of the WanSpec
    mutation.

    Attributes:
        entity_key: The entity this plan conditions on.
        kind: The conditioning kind that was chosen.
        mode: The (possibly re-routed) Wan render mode to use.
        reference_values: Ref pixel/URL values for ``reference_image_urls`` (r2v).
        image_value: Single-frame value for ``image_url`` / ``first_frame_url``.
        character_id: A subject/IP-Adapter token, when chosen.
        prompt_suffix: The locked appearance phrase to append to the prompt.
        negative_suffix: Anti-drift negatives to merge into the negative prompt.
        voice_ref_url: Cloned-voice ref (only for backends that accept it).
        seed: A deterministic seed to pin (None ⇒ leave the shot's seed).
        baked_keyframe: The fallback keyframe, when the keyframe path was taken.
        selected_ref_ids: The locked ref ids actually used (telemetry / dedup).
        used_fallback: True when identity was pinned via a baked/reused keyframe.
    """

    entity_key: str
    kind: ConditioningKind
    mode: WanMode
    reference_values: tuple[str, ...] = ()
    image_value: str | None = None
    character_id: str | None = None
    prompt_suffix: str = ""
    negative_suffix: str = ""
    voice_ref_url: str | None = None
    seed: int | None = None
    baked_keyframe: BakedKeyframe | None = field(default=None, repr=False)
    selected_ref_ids: tuple[str, ...] = ()
    used_fallback: bool = False

    def apply_to(self, spec: WanSpec) -> WanSpec:
        """Apply this plan's identity fields onto ``spec`` (in place; returns it).

        The only place this subsystem touches the provider layer. Sets the mode
        (it may have been re-routed by the fallback), the kind-appropriate image
        fields, and reinforces the prompt/negatives. Never clears fields the plan
        didn't set, so a caller can layer endpoint-frame info first.
        """
        spec.mode = self.mode
        if self.kind is ConditioningKind.REFERENCE_SET:
            spec.reference_image_urls = list(self.reference_values)
        elif self.kind in (
            ConditioningKind.INLINE_IMAGE,
            ConditioningKind.FIRST_FRAME,
        ):
            if self.mode is WanMode.FIRST_LAST_FRAME:
                spec.first_frame_url = self.image_value
            else:
                spec.image_url = self.image_value
        elif self.kind is ConditioningKind.FIRST_LAST_FRAME:
            spec.first_frame_url = self.image_value
        # CHARACTER_ID / NONE set no pixel fields. WanSpec has no subject-token
        # field, so a CHARACTER_ID backend reads ``plan.character_id`` directly
        # (the plan stays the structured carrier); the prompt below still gets the
        # appearance phrase as a soft reinforcement.
        if self.prompt_suffix:
            spec.prompt = _join_prompt(spec.prompt, self.prompt_suffix)
        if self.negative_suffix:
            spec.negative_prompt = _merge_negative(spec.negative_prompt, self.negative_suffix)
        if self.voice_ref_url is not None:
            spec.reference_voice_url = self.voice_ref_url
        if self.seed is not None and spec.seed is None:
            spec.seed = self.seed
        return spec


def _join_prompt(prompt: str, suffix: str) -> str:
    prompt = (prompt or "").strip()
    suffix = suffix.strip()
    if not suffix:
        return prompt
    if not prompt:
        return suffix
    return f"{prompt}, {suffix}"


def _merge_negative(existing: str | None, suffix: str) -> str:
    parts = [p.strip() for p in (existing or "").split(",") if p.strip()]
    for tok in (t.strip() for t in suffix.split(",")):
        if tok and tok not in parts:
            parts.append(tok)
    return ", ".join(parts)


# --------------------------------------------------------------------------- #
# Transport materialisation
# --------------------------------------------------------------------------- #


def _materialise(ref: LockedReference, transport: ImageTransport) -> str | None:
    """Render a locked ref into the value a backend's transport expects.

    URL transport ⇒ the ref's URL (or its data-uri when it only has bytes).
    DATA_URI / BASE64 ⇒ require bytes; ``None`` when the ref is URL-only (the
    caller should have routed through the keyframe fallback to obtain bytes).
    """
    if transport is ImageTransport.URL:
        if ref.url is not None:
            return ref.url
        return ref.as_data_uri() if ref.has_bytes else None
    if transport is ImageTransport.DATA_URI:
        return ref.as_data_uri() if ref.has_bytes else None
    # BASE64
    return ref.as_base64() if ref.has_bytes else None


# --------------------------------------------------------------------------- #
# The conditioner
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ConditionerConfig:
    """Tunables for :class:`IdentityConditioner` (deterministic; no env reads)."""

    selection: SelectionPolicy | None = None
    #: Inject the locked appearance phrase into the prompt on every path.
    reinforce_prompt: bool = True
    #: Inject anti-drift negatives when the backend honours a negative prompt.
    reinforce_negatives: bool = True
    #: Default anti-drift negatives merged in when the bundle has none of its own.
    default_negatives: tuple[str, ...] = (
        "warped face",
        "extra fingers",
        "inconsistent appearance",
    )


class IdentityConditioner:
    """Choose + emit the best identity conditioning for a backend, per shot.

    Stateless apart from the injected fallback + config; safe to share. The single
    public entry point is :meth:`plan`, which is async only because the keyframe
    fallback may bake an image — every non-fallback path is effectively pure.
    """

    def __init__(
        self,
        *,
        fallback: KeyframeFallback | None = None,
        config: ConditionerConfig | None = None,
    ) -> None:
        self._config = config or ConditionerConfig()
        self._fallback = fallback or KeyframeFallback()

    async def plan(
        self,
        bundle: IdentityBundle,
        profile: CapabilityProfile,
        *,
        requested_mode: WanMode = WanMode.REFERENCE_TO_VIDEO,
        desired_pose: Pose = Pose.FRONT,
        shot_prompt: str = "",
        seed: int | None = None,
    ) -> ConditioningPlan:
        """Build the :class:`ConditioningPlan` for ``bundle`` on ``profile`` (async).

        Resolves the highest-fidelity conditioning kind the backend supports and is
        feasible given the bundle's assets (e.g. INLINE_IMAGE needs ref bytes),
        falling through the ladder. When nothing direct is feasible, runs the
        keyframe fallback and re-routes the mode to image-to-video.
        """
        suffix, neg = self._reinforcement(bundle, profile)

        # 1. REFERENCE_SET — strongest. Needs refs *and* the requested mode is r2v
        #    (or any mode, since r2v is the canonical multi-ref path).
        if profile.supports(ConditioningKind.REFERENCE_SET) and bundle.has_references:
            plan = self._reference_set_plan(
                bundle, profile, desired_pose, suffix, neg, seed
            )
            if plan is not None:
                return plan

        # 2. CHARACTER_ID — a registered subject token.
        if profile.supports(ConditioningKind.CHARACTER_ID) and bundle.character_id:
            return ConditioningPlan(
                entity_key=bundle.entity_key,
                kind=ConditioningKind.CHARACTER_ID,
                mode=_single_frame_mode(requested_mode),
                character_id=bundle.character_id,
                prompt_suffix=suffix,
                negative_suffix=neg if profile.accepts_negative_prompt else "",
                voice_ref_url=self._voice(bundle, profile),
                seed=seed if profile.accepts_seed else None,
            )

        # 3. INLINE_IMAGE — best locked ref's bytes inline.
        if profile.supports(ConditioningKind.INLINE_IMAGE):
            plan = self._inline_plan(bundle, profile, desired_pose, suffix, neg, seed)
            if plan is not None:
                return plan

        # 4. FIRST_FRAME / FIRST_LAST_FRAME — best locked ref as the start frame.
        if profile.supports(ConditioningKind.FIRST_FRAME) or profile.supports(
            ConditioningKind.FIRST_LAST_FRAME
        ):
            plan = self._first_frame_plan(
                bundle, profile, requested_mode, desired_pose, suffix, neg, seed
            )
            if plan is not None:
                return plan

        # 5. Keyframe fallback — bake/reuse a keyframe and re-route to i2v.
        return await self._fallback_plan(
            bundle, profile, desired_pose, shot_prompt, suffix, neg, seed
        )

    # -- per-kind builders ------------------------------------------------- #

    def _reference_set_plan(
        self,
        bundle: IdentityBundle,
        profile: CapabilityProfile,
        desired_pose: Pose,
        suffix: str,
        neg: str,
        seed: int | None,
    ) -> ConditioningPlan | None:
        cap = max(profile.max_reference_images, 1)
        # For URL transport, URL-only refs are fine; for inline transports the refs
        # must carry bytes.
        require_bytes = profile.image_transport is not ImageTransport.URL
        refs = select_top(
            bundle,
            k=cap,
            desired_pose=desired_pose,
            policy=self._config.selection,
            require_bytes=require_bytes,
        )
        values = [v for v in (_materialise(r, profile.image_transport) for r in refs) if v]
        if not values:
            return None
        used = [r for r in refs if _materialise(r, profile.image_transport)]
        logger.info(
            "identity.conditioner.reference_set",
            entity=bundle.entity_key,
            refs=len(values),
            backend=profile.name,
        )
        return ConditioningPlan(
            entity_key=bundle.entity_key,
            kind=ConditioningKind.REFERENCE_SET,
            mode=WanMode.REFERENCE_TO_VIDEO,
            reference_values=tuple(values),
            prompt_suffix=suffix,
            negative_suffix=neg if profile.accepts_negative_prompt else "",
            voice_ref_url=self._voice(bundle, profile),
            seed=seed if profile.accepts_seed else None,
            selected_ref_ids=tuple(r.ref_id for r in used),
        )

    def _inline_plan(
        self,
        bundle: IdentityBundle,
        profile: CapabilityProfile,
        desired_pose: Pose,
        suffix: str,
        neg: str,
        seed: int | None,
    ) -> ConditioningPlan | None:
        ref = select_best(
            bundle,
            desired_pose=desired_pose,
            policy=self._config.selection,
            require_bytes=True,
        )
        if ref is None:
            return None
        transport = (
            profile.image_transport
            if profile.image_transport is not ImageTransport.URL
            else ImageTransport.DATA_URI
        )
        value = _materialise(ref, transport)
        if value is None:
            return None
        logger.info(
            "identity.conditioner.inline_image",
            entity=bundle.entity_key,
            ref_id=ref.ref_id,
            backend=profile.name,
        )
        return ConditioningPlan(
            entity_key=bundle.entity_key,
            kind=ConditioningKind.INLINE_IMAGE,
            mode=WanMode.IMAGE_TO_VIDEO,
            image_value=value,
            prompt_suffix=suffix,
            negative_suffix=neg if profile.accepts_negative_prompt else "",
            seed=seed if profile.accepts_seed else None,
            selected_ref_ids=(ref.ref_id,),
        )

    def _first_frame_plan(
        self,
        bundle: IdentityBundle,
        profile: CapabilityProfile,
        requested_mode: WanMode,
        desired_pose: Pose,
        suffix: str,
        neg: str,
        seed: int | None,
    ) -> ConditioningPlan | None:
        require_bytes = profile.image_transport is not ImageTransport.URL
        ref = select_best(
            bundle,
            desired_pose=desired_pose,
            policy=self._config.selection,
            require_bytes=require_bytes,
        )
        if ref is None:
            return None
        value = _materialise(ref, profile.image_transport)
        if value is None:
            return None
        # Honour a requested first-last-frame mode only when the backend supports it.
        flf = (
            requested_mode is WanMode.FIRST_LAST_FRAME
            and profile.supports(ConditioningKind.FIRST_LAST_FRAME)
        )
        kind = ConditioningKind.FIRST_LAST_FRAME if flf else ConditioningKind.FIRST_FRAME
        mode = WanMode.FIRST_LAST_FRAME if flf else WanMode.IMAGE_TO_VIDEO
        logger.info(
            "identity.conditioner.first_frame",
            entity=bundle.entity_key,
            ref_id=ref.ref_id,
            backend=profile.name,
            flf=flf,
        )
        return ConditioningPlan(
            entity_key=bundle.entity_key,
            kind=kind,
            mode=mode,
            image_value=value,
            prompt_suffix=suffix,
            negative_suffix=neg if profile.accepts_negative_prompt else "",
            seed=seed if profile.accepts_seed else None,
            selected_ref_ids=(ref.ref_id,),
        )

    async def _fallback_plan(
        self,
        bundle: IdentityBundle,
        profile: CapabilityProfile,
        desired_pose: Pose,
        shot_prompt: str,
        suffix: str,
        neg: str,
        seed: int | None,
    ) -> ConditioningPlan:
        keyframe = await self._fallback.keyframe_for(
            bundle, shot_prompt=shot_prompt, desired_pose=desired_pose, seed=seed
        )
        if keyframe is None:
            # No bytes, no baker, no usable ref: the only honest option is a
            # text-only render with the appearance phrase carrying identity.
            logger.info(
                "identity.conditioner.text_only",
                entity=bundle.entity_key,
                backend=profile.name,
            )
            return ConditioningPlan(
                entity_key=bundle.entity_key,
                kind=ConditioningKind.NONE,
                mode=WanMode.TEXT_TO_VIDEO,
                prompt_suffix=suffix,
                negative_suffix=neg if profile.accepts_negative_prompt else "",
                seed=seed if profile.accepts_seed else None,
                used_fallback=True,
            )
        transport = (
            profile.image_transport
            if profile.image_transport is not ImageTransport.URL
            else ImageTransport.DATA_URI
        )
        value = _materialise(keyframe.as_locked_reference(), transport)
        logger.info(
            "identity.conditioner.keyframe_fallback",
            entity=bundle.entity_key,
            backend=profile.name,
            source=keyframe.source.value,
        )
        return ConditioningPlan(
            entity_key=bundle.entity_key,
            kind=ConditioningKind.FIRST_FRAME,
            mode=WanMode.IMAGE_TO_VIDEO,
            image_value=value,
            prompt_suffix=suffix,
            negative_suffix=neg if profile.accepts_negative_prompt else "",
            seed=keyframe.seed if keyframe.seed is not None else seed,
            baked_keyframe=keyframe,
            selected_ref_ids=keyframe.source_ref_ids,
            used_fallback=True,
        )

    # -- helpers ----------------------------------------------------------- #

    def _reinforcement(
        self, bundle: IdentityBundle, profile: CapabilityProfile
    ) -> tuple[str, str]:
        suffix = bundle.appearance_prompt.strip() if self._config.reinforce_prompt else ""
        if not self._config.reinforce_negatives:
            return suffix, ""
        negs = bundle.negative_tokens or self._config.default_negatives
        return suffix, ", ".join(negs)

    @staticmethod
    def _voice(bundle: IdentityBundle, profile: CapabilityProfile) -> str | None:
        if profile.accepts_reference_voice and bundle.voice_ref_url:
            return bundle.voice_ref_url
        return None


def _single_frame_mode(requested: WanMode) -> WanMode:
    """The mode to use for a single-frame / token path given the requested mode."""
    if requested in (WanMode.FIRST_LAST_FRAME, WanMode.IMAGE_TO_VIDEO):
        return requested
    return WanMode.IMAGE_TO_VIDEO


__all__ = [
    "ConditionerConfig",
    "ConditioningPlan",
    "IdentityConditioner",
]
