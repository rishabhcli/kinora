"""Per-provider determinism classification → a reproducibility label.

The honest truth about hosted diffusion video: **most providers do not promise
byte-identical output even for a fixed seed.** Some honour a seed enough that the
*composition* is reproducible (same framing, same character, same motion arc)
while pixels differ; a few ignore the seed entirely; almost none are byte-stable
across a model-version bump. A reproducibility subsystem that pretended otherwise
would lie to the Director ("re-render is identical!") and erode trust.

So this module classifies each ``(provider, model)`` along two axes —

* :class:`SeedHonoring` — does the model *use* the seed at all, and how strongly?
* :class:`ByteStability` — given the same resolved request, do we get the same
  bytes back?

— and folds them into a single :class:`ReproLabel` that the rest of the
subsystem (and the UI) can show: **GUARANTEED** (we can promise the exact same
clip), **BEST_EFFORT** (we can promise the same *plan*; pixels may drift), or
**NONE** (re-render is a fresh roll — only the inputs are reproducible, not the
output).

The capability table is *data*, not code branching, so adding a provider is one
entry. Defaults are conservative: an unknown model is ``BEST_EFFORT`` at most
(we record everything and can re-issue the identical request, but we never claim
byte-stability we have not observed).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class SeedHonoring(StrEnum):
    """How strongly a model's output depends on the supplied seed."""

    #: A fixed seed pins output deterministically (the model's RNG is the only
    #: stochastic source and it is seeded). Composition *and* pixels are stable.
    DETERMINISTIC = "deterministic"
    #: A fixed seed reproduces the *plan* (framing/identity/motion) but not exact
    #: pixels — the common hosted-diffusion case.
    PLAN_STABLE = "plan_stable"
    #: The seed is accepted by the API but does not meaningfully constrain output.
    COSMETIC = "cosmetic"
    #: The model exposes no seed / ignores it entirely.
    IGNORED = "ignored"
    #: We have not characterised this model; assume the weakest safe stance.
    UNKNOWN = "unknown"


class ByteStability(StrEnum):
    """Whether identical inputs yield identical *bytes* from the provider."""

    #: Byte-identical output for identical resolved request + seed.
    BYTE_STABLE = "byte_stable"
    #: Deterministic composition, non-deterministic encoding/pixels.
    PIXEL_DRIFT = "pixel_drift"
    #: Output varies run to run regardless of inputs.
    NON_DETERMINISTIC = "non_deterministic"
    #: Uncharacterised.
    UNKNOWN = "unknown"


class ReproLabel(StrEnum):
    """The user-facing reproducibility promise for a clip."""

    #: We can re-produce the exact same clip (bytes) by replaying the request.
    GUARANTEED = "guaranteed"
    #: We can re-produce the same *plan* (and re-issue the identical request);
    #: pixels may differ. The default honest stance for hosted video.
    BEST_EFFORT = "best_effort"
    #: Re-render is a fresh roll. Only the *inputs* (fingerprint) are reproducible.
    NONE = "none"


class DeterminismProfile(BaseModel):
    """A model's characterised reproducibility behaviour.

    Persisted as data (one row per ``(provider, model_family)``); the version
    field lets a model *family* keep a profile while a specific version override
    sharpens it once observed.
    """

    model_config = ConfigDict(frozen=True)

    provider: str
    #: Match against the resolved model id by prefix (``""`` = catch-all default).
    model_prefix: str = ""
    seed_honoring: SeedHonoring = SeedHonoring.UNKNOWN
    byte_stability: ByteStability = ByteStability.UNKNOWN
    #: Free-text provenance of *why* this profile is what it is (a doc link, an
    #: observed-experiment note). Never affects the label; aids auditing.
    rationale: str = ""
    #: A model-version bump almost always breaks byte-stability; record whether a
    #: version change should *downgrade* a GUARANTEED label to BEST_EFFORT.
    version_sensitive: bool = True

    def label(self) -> ReproLabel:
        """Fold the two axes into a single reproducibility promise."""
        if (
            self.seed_honoring is SeedHonoring.DETERMINISTIC
            and self.byte_stability is ByteStability.BYTE_STABLE
        ):
            return ReproLabel.GUARANTEED
        if self.seed_honoring in (SeedHonoring.IGNORED, SeedHonoring.UNKNOWN) and (
            self.byte_stability
            in (ByteStability.NON_DETERMINISTIC, ByteStability.UNKNOWN)
        ):
            # No seed influence *and* no observed stability → only inputs repro.
            # We still allow BEST_EFFORT if the seed is at least accepted; pure
            # NONE is reserved for "seed ignored AND output non-deterministic".
            if (
                self.seed_honoring is SeedHonoring.IGNORED
                and self.byte_stability is ByteStability.NON_DETERMINISTIC
            ):
                return ReproLabel.NONE
            return ReproLabel.BEST_EFFORT
        return ReproLabel.BEST_EFFORT


class DeterminismClassification(BaseModel):
    """The classifier's verdict for a specific resolved ``(provider, model)``."""

    model_config = ConfigDict(frozen=True)

    provider: str
    model: str
    seed_honoring: SeedHonoring
    byte_stability: ByteStability
    label: ReproLabel
    version_sensitive: bool
    rationale: str = ""
    #: True when no characterised profile matched and conservative defaults were
    #: used — the caller may want to surface "uncharacterised" in the UI.
    is_default: bool = False

    def reproducible_bytes(self) -> bool:
        """Whether re-rendering can be promised to yield the identical clip."""
        return self.label is ReproLabel.GUARANTEED

    def reproducible_plan(self) -> bool:
        """Whether re-rendering can be promised to yield the same *composition*."""
        return self.label in (ReproLabel.GUARANTEED, ReproLabel.BEST_EFFORT)


# --------------------------------------------------------------------------- #
# Built-in capability table
# --------------------------------------------------------------------------- #
#
# Conservative, additive, data-only. Entries reflect this repo's documented
# providers (AGENTS.md): hosted Wan on DashScope, hosted MiniMax (Hailuo). Both
# are diffusion-family hosted models with no byte-stability promise; the seed is
# accepted (DashScope forwards ``parameters.seed``) and pins the plan, so the
# honest label is BEST_EFFORT. The local ffmpeg Ken-Burns degradation lane, by
# contrast, *is* a deterministic transform of a fixed keyframe → GUARANTEED.

_DEFAULT_PROFILES: tuple[DeterminismProfile, ...] = (
    DeterminismProfile(
        provider="dashscope",
        model_prefix="wan",
        seed_honoring=SeedHonoring.PLAN_STABLE,
        byte_stability=ByteStability.PIXEL_DRIFT,
        rationale=(
            "Hosted Wan forwards parameters.seed; a fixed seed reproduces the "
            "composition/identity/motion arc but not exact pixels (no hosted "
            "byte-stability guarantee). Model-version bumps change the plan."
        ),
        version_sensitive=True,
    ),
    DeterminismProfile(
        provider="minimax",
        model_prefix="MiniMax",
        seed_honoring=SeedHonoring.PLAN_STABLE,
        byte_stability=ByteStability.PIXEL_DRIFT,
        rationale=(
            "Hosted MiniMax/Hailuo accepts a seed and is plan-stable; pixels "
            "drift run to run. No byte-stability guarantee."
        ),
        version_sensitive=True,
    ),
    # The local degradation lane: a pure ffmpeg Ken-Burns transform of a fixed
    # keyframe is genuinely deterministic and byte-stable.
    DeterminismProfile(
        provider="local",
        model_prefix="kenburns",
        seed_honoring=SeedHonoring.DETERMINISTIC,
        byte_stability=ByteStability.BYTE_STABLE,
        rationale="Deterministic ffmpeg Ken-Burns over a fixed keyframe.",
        version_sensitive=False,
    ),
)


class DeterminismClassifier:
    """Resolve a :class:`DeterminismClassification` for a ``(provider, model)``.

    Profiles are matched provider-first, then by the *longest* matching model
    prefix (so a sharper override wins over a family default). An unmatched
    provider/model falls back to a deliberately conservative BEST_EFFORT stance:
    we always record enough to replay the request, but we never over-promise
    byte-stability we have not characterised.
    """

    def __init__(self, profiles: tuple[DeterminismProfile, ...] | None = None) -> None:
        self._profiles = profiles if profiles is not None else _DEFAULT_PROFILES

    def classify(self, *, provider: str, model: str) -> DeterminismClassification:
        match = self._best_match(provider=provider, model=model)
        if match is None:
            return DeterminismClassification(
                provider=provider,
                model=model,
                seed_honoring=SeedHonoring.UNKNOWN,
                byte_stability=ByteStability.UNKNOWN,
                label=ReproLabel.BEST_EFFORT,
                version_sensitive=True,
                rationale=(
                    "No characterised determinism profile for this provider/model; "
                    "assuming best-effort (inputs + request are reproducible; "
                    "output is not guaranteed byte-stable)."
                ),
                is_default=True,
            )
        return DeterminismClassification(
            provider=match.provider,
            model=model,
            seed_honoring=match.seed_honoring,
            byte_stability=match.byte_stability,
            label=match.label(),
            version_sensitive=match.version_sensitive,
            rationale=match.rationale,
            is_default=False,
        )

    def _best_match(self, *, provider: str, model: str) -> DeterminismProfile | None:
        best: DeterminismProfile | None = None
        for profile in self._profiles:
            if profile.provider != provider:
                continue
            if profile.model_prefix and not model.startswith(profile.model_prefix):
                continue
            if best is None or len(profile.model_prefix) > len(best.model_prefix):
                best = profile
        return best

    def with_profiles(
        self, extra: tuple[DeterminismProfile, ...]
    ) -> DeterminismClassifier:
        """Return a new classifier with *extra* profiles taking precedence.

        Extra profiles are tried first (longest-prefix-wins still applies within
        each provider), so a deployment can sharpen a label once it has observed
        a model's behaviour, without mutating the shared defaults.
        """
        return DeterminismClassifier(profiles=extra + self._profiles)


#: Module-level default classifier (frozen profiles → safe to share).
DEFAULT_CLASSIFIER = DeterminismClassifier()


__all__ = [
    "DEFAULT_CLASSIFIER",
    "ByteStability",
    "DeterminismClassification",
    "DeterminismClassifier",
    "DeterminismProfile",
    "ReproLabel",
    "SeedHonoring",
]
