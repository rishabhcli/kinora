"""Exception hierarchy for the ML-platform serving facet.

Every error this facet raises descends from :class:`MLPlatformError` so a caller
(the service façade, a future API layer) can catch the whole family with one
``except`` and map it to a problem response. Subclasses narrow the cause:

* registry errors — unknown model, duplicate version, illegal promotion, bad
  rollback target, a failed eval gate;
* distillation errors — a malformed distillation spec or an empty teacher corpus;
* serving-simulator errors — invalid serving configuration, capacity exhaustion,
  or a violated scheduler invariant detected at runtime.

Nothing here imports the rest of the app — the facet's foundations stay
dependency-free so the pure logic (the discrete-event simulator, the registry
state machine) is unit-testable in isolation.
"""

from __future__ import annotations


class MLPlatformError(Exception):
    """Base class for every error raised by :mod:`app.mlplatform`."""


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
class RegistryError(MLPlatformError):
    """Base class for model-registry errors."""


class ModelNotFoundError(RegistryError):
    """A model name (optionally a specific version) is not registered."""

    def __init__(self, name: str, version: str | None = None) -> None:
        self.name = name
        self.version = version
        if version is None:
            super().__init__(f"model {name!r} is not registered")
        else:
            super().__init__(f"model {name!r} has no version {version!r}")


class DuplicateModelVersionError(RegistryError):
    """A (name, version) pair already exists (registration is append-only)."""

    def __init__(self, name: str, version: str) -> None:
        self.name = name
        self.version = version
        super().__init__(f"model {name!r} already has version {version!r}")


class PromotionError(RegistryError):
    """A staged promotion is illegal (skips a stage, or the gate has not passed)."""


class RollbackError(RegistryError):
    """A rollback target is invalid (e.g. no prior version in that stage)."""


class EvalGateError(RegistryError):
    """A model version failed (or has not run) the eval gate required to promote."""


class LineageError(RegistryError):
    """A declared parent/teacher lineage points at a model version that is absent."""


# --------------------------------------------------------------------------- #
# Distillation
# --------------------------------------------------------------------------- #
class DistillationError(MLPlatformError):
    """Base class for knowledge-distillation pipeline errors."""


class DistillationSpecError(DistillationError):
    """A distillation spec is malformed (bad ratios, missing teacher, ...)."""


class EmptyCorpusError(DistillationError):
    """Dataset generation produced no usable teacher→student examples."""


# --------------------------------------------------------------------------- #
# Serving simulator
# --------------------------------------------------------------------------- #
class ServingError(MLPlatformError):
    """Base class for serving-layer simulation errors."""


class ServingConfigError(ServingError):
    """A serving configuration is invalid (non-positive limits, bad ratios, ...)."""


class CapacityError(ServingError):
    """The KV-cache / batch has no room and the request cannot be admitted yet."""


class InvariantViolationError(ServingError):
    """A scheduler invariant was violated at runtime — a simulator bug.

    These are raised by the simulator's own internal assertions (e.g. a batch
    exceeding its token budget). They must never fire in a correct run; the
    property tests exist to make sure they don't.
    """


__all__ = [
    "CapacityError",
    "DistillationError",
    "DistillationSpecError",
    "DuplicateModelVersionError",
    "EmptyCorpusError",
    "EvalGateError",
    "InvariantViolationError",
    "LineageError",
    "MLPlatformError",
    "ModelNotFoundError",
    "PromotionError",
    "RegistryError",
    "RollbackError",
    "ServingConfigError",
    "ServingError",
]
