"""``RenderFingerprint`` — the full provenance manifest for one clip.

> "We also need to know exactly what produced any clip, and re-rendering a shot
> must be reproducible where the model allows it."

The fingerprint is that record. It captures **every input that could change the
output** of a render — the resolved request, the provider/model/version, the
seed, digests of the prompt-dialect and reference-identity, the planner
concessions (degradations the planner accepted to stay in budget / under the
retry cap), and the post-ops applied after generation. From those fields it
derives a single stable ``fingerprint_id`` (collision-resistant), and it carries
the determinism classification so the manifest *itself* states whether the clip
is byte-reproducible or only plan-reproducible.

Design choices that matter for reproducibility:

* **Canonical-request digest is separate from the full id.** ``request_digest``
  hashes only the bits that the *provider* sees (prompt, mode, seed, reference
  urls→identity, model, params). Two fingerprints with the same
  ``request_digest`` would re-issue the *identical* provider call — that is what
  :mod:`app.video.repro.replay` keys on. The full ``fingerprint_id`` additionally
  folds in canon version, concessions, and post-ops, so it is the provenance
  identity (two clips can share a request_digest yet differ in canon-version
  context).
* **Identity by digest, not by URL.** Reference images are addressed in the canon
  as ``id@version`` (e.g. ``char_elsa_001@v3``); their *signed* OSS URLs rotate
  and must never enter a stable hash. The fingerprint hashes the **reference
  identity ids**, and records the URLs only as non-keyed provenance.
* **Aligned with §8.7.** The fingerprint can emit the exact six-component
  ``shot_hash`` the cache uses, so a fingerprint and a cache key never disagree.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.providers.types import WanMode, WanSpec

from .classifier import (
    DEFAULT_CLASSIFIER,
    DeterminismClassification,
    DeterminismClassifier,
)
from .hashing import DIGEST_ALGORITHM, digest, digest_fields, short

#: Schema version of the manifest itself — lets a persisted fingerprint be read
#: back even after the model grows fields. Bump on any change to *which* fields
#: feed ``fingerprint_id`` (a change there invalidates old ids by design).
FINGERPRINT_SCHEMA_VERSION = 1


class ResolvedProvider(BaseModel):
    """The provider/model/version actually used (or to be used) for a render.

    ``version`` is the immutable model-version pin when the provider exposes one
    (DashScope model ids already encode the family+version, e.g.
    ``wan2.1-i2v-turbo``); ``protocol`` is the request-shape profile
    (:class:`app.providers.video.VideoProtocol`). Both feed the digest because a
    protocol or version change can change the output even for identical inputs.
    """

    model_config = ConfigDict(frozen=True)

    provider: str
    model: str
    version: str | None = None
    protocol: str | None = None


class Concession(BaseModel):
    """A planner/render concession recorded on the clip's provenance.

    A "concession" is any place the planner *gave something up* to ship the clip:
    a budget-driven downgrade to the Ken-Burns ladder, a retry-cap-exhausted
    degrade, a resolution/duration clamp, a fallback from r2v→flf on a style
    failure (§9.5). Recording them is what lets a DIFF say "this clip differs
    because the earlier one was degraded for budget, the new one rendered live."
    """

    model_config = ConfigDict(frozen=True)

    kind: str
    detail: str = ""


class PostOp(BaseModel):
    """A deterministic post-generation transform applied to the clip.

    Color-match, audio mux, caption burn-in, Ken-Burns synthesis — anything that
    changes the *bytes* after the provider returns. Ordered (a post-op pipeline is
    sequence-sensitive), so post-ops feed the digest as an ordered list.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    #: Stable parameter map for the op (sorted-key digested). Keep it small and
    #: value-only — never embed signed URLs or bytes here.
    params: dict[str, Any] = Field(default_factory=dict)


class RenderFingerprint(BaseModel):
    """Everything that determined a clip, plus the derived provenance ids.

    Build one with :meth:`from_spec` from a resolved :class:`WanSpec` at render
    time, or construct directly when reconstructing from a persisted manifest.
    The model is immutable; derive a changed copy with :meth:`evolve` so the ids
    recompute consistently.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: int = FINGERPRINT_SCHEMA_VERSION

    # -- identity / lineage context --------------------------------------- #
    book_id: str
    scene_id: str | None = None
    beat_id: str | None = None
    shot_id: str | None = None
    #: The canon version this shot was rendered against (§8.2 / §8.7). Part of the
    #: provenance id but *not* the request digest (the provider never sees it).
    canon_version_at_render: int = 0

    # -- the resolved request --------------------------------------------- #
    provider: ResolvedProvider
    mode: WanMode
    seed: int
    duration_s: int = 5
    resolution: str = "720P"
    #: Non-prompt generation params that affect output (watermark, prompt_extend,
    #: negative-prompt presence is folded into ``prompt_digest``).
    params: dict[str, Any] = Field(default_factory=dict)

    # -- content digests (never raw content in a hash key) ---------------- #
    #: Digest of the prompt + negative-prompt + dialect tag. "Dialect" is the
    #: prompt-construction style (e.g. a provider-specific prompt format); a
    #: dialect change can change output even at identical seed, so it is folded in.
    prompt_digest: str
    #: Digest of the *reference identity* — the ``id@version`` reference set, NOT
    #: the rotating signed URLs (§8.1 locked references).
    reference_identity_digest: str

    # -- planner / post provenance ---------------------------------------- #
    concessions: tuple[Concession, ...] = ()
    post_ops: tuple[PostOp, ...] = ()

    # -- non-keyed provenance (recorded, never hashed) -------------------- #
    #: The reference identity ids (``char_elsa_001@v3`` …) for human inspection.
    reference_image_ids: tuple[str, ...] = ()
    #: The (rotating) signed URLs at render time — provenance only, not keyed.
    reference_image_urls: tuple[str, ...] = ()
    #: The provider's task id for the actual render, when known.
    provider_task_id: str | None = None
    #: The raw prompt/negative-prompt, kept for replay reconstruction. They are
    #: *recorded*, but only their digest is *keyed*.
    prompt: str = ""
    negative_prompt: str | None = None
    dialect: str = "default"

    # -- determinism label ------------------------------------------------- #
    determinism: DeterminismClassification

    digest_algorithm: str = DIGEST_ALGORITHM

    # ------------------------------------------------------------------ #
    # Derived ids
    # ------------------------------------------------------------------ #

    @property
    def request_digest(self) -> str:
        """Digest of *only* the inputs the provider sees.

        Two fingerprints with the same ``request_digest`` re-issue the identical
        provider call. Deliberately excludes canon version, concessions, and
        post-ops (those change provenance, not the provider request).
        """
        return digest_fields(
            provider=self.provider.provider,
            model=self.provider.model,
            version=self.provider.version,
            protocol=self.provider.protocol,
            mode=str(self.mode.value),
            seed=self.seed,
            duration_s=self.duration_s,
            resolution=self.resolution,
            params=self.params,
            prompt_digest=self.prompt_digest,
            reference_identity_digest=self.reference_identity_digest,
        )

    @property
    def fingerprint_id(self) -> str:
        """The full provenance identity of the clip (stable, collision-resistant).

        Folds the request digest together with the lineage/canon context and the
        planner concessions + post-ops, so two clips that came from the identical
        provider call but under a different canon version, concession set, or
        post-op pipeline get *distinct* fingerprint ids.
        """
        return digest_fields(
            schema_version=self.schema_version,
            request_digest=self.request_digest,
            book_id=self.book_id,
            scene_id=self.scene_id,
            beat_id=self.beat_id,
            shot_id=self.shot_id,
            canon_version_at_render=self.canon_version_at_render,
            concessions=[c.model_dump() for c in self.concessions],
            post_ops=[p.model_dump() for p in self.post_ops],
        )

    @property
    def short_id(self) -> str:
        """A short human-facing id (logs / UI). Never use as a key."""
        return short(self.fingerprint_id)

    def shot_hash(self) -> str:
        """Emit the §8.7 six-component cache key from this fingerprint.

        Uses the same components and separator as :func:`app.db.hashing`'s
        ``compute_shot_hash`` so a fingerprint and the live cache key agree by
        construction. Requires ``beat_id``; raises if it is absent (a cache key is
        meaningless without the beat).
        """
        if self.beat_id is None:
            raise ValueError("shot_hash requires a beat_id")
        # Import locally to avoid a hard import cycle at module load and to use
        # the *authoritative* cache-key function rather than re-implementing it.
        from app.db.hashing import compute_shot_hash

        return compute_shot_hash(
            book_id=self.book_id,
            beat_id=self.beat_id,
            canon_version_at_render=self.canon_version_at_render,
            render_mode=str(self.mode.value),
            seed=self.seed,
            reference_set_hash=self.reference_identity_digest,
        )

    def evolve(self, **changes: Any) -> RenderFingerprint:
        """Return an immutable copy with *changes* applied (ids recompute lazily)."""
        return self.model_copy(update=changes)

    def as_manifest(self) -> dict[str, Any]:
        """A JSON-safe provenance manifest with the derived ids materialised.

        This is the persistable form — every field plus the derived
        ``fingerprint_id`` / ``request_digest`` so a stored manifest is
        self-describing and a reader need not re-import this code to see the ids.
        """
        data = self.model_dump(mode="json")
        data["fingerprint_id"] = self.fingerprint_id
        data["request_digest"] = self.request_digest
        return data

    # ------------------------------------------------------------------ #
    # Construction from a resolved WanSpec
    # ------------------------------------------------------------------ #

    @classmethod
    def from_spec(
        cls,
        spec: WanSpec,
        *,
        provider: ResolvedProvider,
        book_id: str,
        scene_id: str | None = None,
        beat_id: str | None = None,
        canon_version_at_render: int = 0,
        reference_image_ids: list[str] | None = None,
        dialect: str = "default",
        concessions: list[Concession] | None = None,
        post_ops: list[PostOp] | None = None,
        provider_task_id: str | None = None,
        classifier: DeterminismClassifier | None = None,
    ) -> RenderFingerprint:
        """Build a fingerprint from a fully-resolved :class:`WanSpec`.

        ``reference_image_ids`` are the *stable* canon identities
        (``id@version``); when omitted, the spec's reference URLs are used to form
        the identity digest as a fallback (less stable, but better than nothing).
        The seed must be resolved (not ``None``) by render time; an unset seed is
        treated as ``0`` and recorded as such so the manifest is never lossy.
        """
        clf = classifier or DEFAULT_CLASSIFIER
        ids = tuple(reference_image_ids or [])
        urls = tuple(spec.reference_image_urls)
        # Identity digest keys on stable ids when we have them, else on URLs.
        identity_source: Any = list(ids) if ids else list(urls)
        reference_identity_digest = digest(
            {"refs": identity_source, "voice": spec.reference_voice_url}
        )
        prompt_digest = digest(
            {
                "prompt": spec.prompt,
                "negative_prompt": spec.negative_prompt,
                "dialect": dialect,
            }
        )
        params = {
            "watermark": spec.watermark,
            "prompt_extend": spec.prompt_extend,
        }
        determinism = clf.classify(provider=provider.provider, model=provider.model)
        return cls(
            book_id=book_id,
            scene_id=scene_id,
            beat_id=beat_id,
            shot_id=spec.shot_id,
            canon_version_at_render=canon_version_at_render,
            provider=provider,
            mode=spec.mode,
            seed=int(spec.seed or 0),
            duration_s=spec.duration_s,
            resolution=spec.resolution,
            params=params,
            prompt_digest=prompt_digest,
            reference_identity_digest=reference_identity_digest,
            concessions=tuple(concessions or ()),
            post_ops=tuple(post_ops or ()),
            reference_image_ids=ids,
            reference_image_urls=urls,
            provider_task_id=provider_task_id,
            prompt=spec.prompt,
            negative_prompt=spec.negative_prompt,
            dialect=dialect,
            determinism=determinism,
        )


__all__ = [
    "FINGERPRINT_SCHEMA_VERSION",
    "Concession",
    "PostOp",
    "RenderFingerprint",
    "ResolvedProvider",
]
