"""The default Kinora model catalog + serving profiles.

A small, opinionated set of model versions modeling Kinora's actual stack (§11):
the reasoning brain, the judge, the learned reward model, and a tiny draft model for
speculative decoding. These are *modeled* serving profiles for the simulator — the
per-token times and KV footprints are first-order estimates scaled by parameter
count, not measured kernels — so the planner has something realistic to plan against
out of the box without anyone hand-building a registry.

Pure data + a builder; no network, no live model.
"""

from __future__ import annotations

from app.mlplatform.serving.model import ModelKind, ModelProfile, ModelVersion
from app.mlplatform.serving.registry import ModelRegistry


def _profile(
    *,
    params_b: float,
    decode_ms: float,
    prefill_ms: float,
    kv_bytes: int,
    cost_1k: float,
    accept_rate: float = 0.0,
    ctx: int = 8192,
) -> ModelProfile:
    return ModelProfile(
        decode_ms_per_token=decode_ms,
        prefill_ms_per_token=prefill_ms,
        kv_bytes_per_token=kv_bytes,
        params_billions=params_b,
        cost_per_1k_tokens=cost_1k,
        accept_rate=accept_rate,
        max_context_tokens=ctx,
    )


#: The seed catalog — one DEV version of each model class.
DEFAULT_CATALOG: tuple[ModelVersion, ...] = (
    ModelVersion(
        name="kinora-brain",
        version="1.0.0",
        kind=ModelKind.REASONING,
        profile=_profile(
            params_b=72.0, decode_ms=22.0, prefill_ms=2.2, kv_bytes=8192, cost_1k=0.02
        ),
        tags=("reasoning", "teacher"),
    ),
    ModelVersion(
        name="kinora-judge",
        version="1.0.0",
        kind=ModelKind.JUDGE,
        profile=_profile(
            params_b=14.0, decode_ms=9.0, prefill_ms=0.9, kv_bytes=4096, cost_1k=0.006
        ),
        tags=("judge",),
    ),
    ModelVersion(
        name="kinora-reward",
        version="1.0.0",
        kind=ModelKind.REWARD,
        profile=_profile(params_b=7.0, decode_ms=5.0, prefill_ms=0.5, kv_bytes=2048, cost_1k=0.002),
        tags=("reward",),
    ),
    ModelVersion(
        name="kinora-draft",
        version="1.0.0",
        kind=ModelKind.DRAFT,
        profile=_profile(
            params_b=1.5,
            decode_ms=1.1,
            prefill_ms=0.12,
            kv_bytes=768,
            cost_1k=0.0004,
            accept_rate=0.78,
        ),
        tags=("draft", "speculative-proposer"),
    ),
)


def build_default_registry() -> ModelRegistry:
    """Return a fresh registry seeded with the default Kinora catalog (all in DEV)."""
    registry = ModelRegistry()
    for version in DEFAULT_CATALOG:
        registry.register(version)
    return registry


__all__ = ["DEFAULT_CATALOG", "build_default_registry"]
