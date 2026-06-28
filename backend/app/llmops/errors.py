"""Exception hierarchy for the LLM-ops / prompt-registry platform.

Every error the package raises descends from :class:`LLMOpsError` so callers
(the API layer, the service façade) can catch the whole family with one ``except``
and map it to an HTTP problem. Each subclass narrows the cause so handlers can
react differently (a 404 for an unknown prompt vs. a 409 for a duplicate version
vs. a 422 for a malformed semver).

Nothing here imports the app — the package's foundations stay dependency-free so
the pure logic (semver, diff, rubric math) can be unit-tested in isolation.
"""

from __future__ import annotations


class LLMOpsError(Exception):
    """Base class for every error raised by :mod:`app.llmops`."""


class PromptNotFoundError(LLMOpsError):
    """A prompt key (optionally a specific version) is not in the registry."""

    def __init__(self, key: str, version: str | None = None) -> None:
        self.key = key
        self.version = version
        if version is None:
            super().__init__(f"prompt {key!r} is not registered")
        else:
            super().__init__(f"prompt {key!r} has no version {version!r}")


class DuplicateVersionError(LLMOpsError):
    """A semver already exists for the prompt key (registration is append-only)."""

    def __init__(self, key: str, version: str) -> None:
        self.key = key
        self.version = version
        super().__init__(f"prompt {key!r} already has version {version!r}")


class InvalidVersionError(LLMOpsError):
    """A version string is not a valid semantic version."""


class RollbackError(LLMOpsError):
    """A rollback target is invalid (e.g. rolling forward, or to a missing version)."""


class GuardrailBlockedError(LLMOpsError):
    """A guardrail policy blocked the input or output.

    Carries the structured verdict so the caller can surface the reasons.
    """

    def __init__(self, message: str, *, verdict: object | None = None) -> None:
        self.verdict = verdict
        super().__init__(message)


class DatasetError(LLMOpsError):
    """A golden dataset is malformed or references an unknown rubric."""


class RubricError(LLMOpsError):
    """A rubric is malformed (e.g. weights don't sum, no criteria)."""


class ModelNotRegisteredError(LLMOpsError):
    """A model id is not present in the model registry."""

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id
        super().__init__(f"model {model_id!r} is not in the registry")


class NoCapableModelError(LLMOpsError):
    """No registered model satisfies the requested capability constraints."""


class TraceNotFoundError(LLMOpsError):
    """A run-trace id is not present in the trace store."""

    def __init__(self, trace_id: str) -> None:
        self.trace_id = trace_id
        super().__init__(f"trace {trace_id!r} not found")


__all__ = [
    "DatasetError",
    "DuplicateVersionError",
    "GuardrailBlockedError",
    "InvalidVersionError",
    "LLMOpsError",
    "ModelNotRegisteredError",
    "NoCapableModelError",
    "PromptNotFoundError",
    "RollbackError",
    "RubricError",
    "TraceNotFoundError",
]
