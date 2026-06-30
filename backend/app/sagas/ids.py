"""Stable id / idempotency-key helpers for the saga engine.

Determinism is the whole game: a re-run with the same history must take the same
path and produce the same idempotency keys, so a step that already wrote its side
effect is *recognised* on resume and not re-executed.

* :func:`new_run_id` — a fresh, opaque run id (the one place randomness lives;
  tests inject a deterministic factory instead).
* :func:`step_idempotency_key` — a content-addressed key from
  ``(run_id, step_name, attempt-invariant inputs)``. It is **attempt-invariant**
  on purpose: a crash-resume of the *same* logical step must reproduce the *same*
  key so the downstream side effect dedupes. (A deliberate redesign that should
  re-run uses a different ``salt``.)
* :func:`fingerprint` — a short, stable digest of an arbitrary
  JSON-serialisable structure (used to detect a changed workflow input / spec).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

#: Prefix marks the key scheme/version so a future change is detectable.
_KEY_PREFIX = "sik1:"
_FP_PREFIX = "fp1:"


def new_run_id() -> str:
    """A fresh opaque run id. The single source of randomness in the engine."""
    return "run_" + uuid.uuid4().hex


def _canonical(value: Any) -> str:
    """A stable string for any JSON-serialisable structure.

    ``sort_keys`` makes dict ordering irrelevant; non-JSON values fall back to a
    sorted ``repr`` so the function never raises on, e.g., a dataclass.
    """
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=repr)
    except (TypeError, ValueError):
        return repr(value)


def fingerprint(value: Any) -> str:
    """A short stable digest of ``value`` (changed-input detection)."""
    digest = hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()
    return _FP_PREFIX + digest[:24]


def step_idempotency_key(run_id: str, step_name: str, *inputs: Any, salt: str = "") -> str:
    """An attempt-invariant idempotency key for one logical step execution.

    The same ``(run_id, step_name, inputs, salt)`` always maps to the same key —
    so a crash-resume reproduces it and the side effect dedupes. Inputs should be
    the *attempt-invariant* values (the workflow input + prior step results),
    never an attempt counter or a fresh seed, unless re-execution is intended.
    """
    raw = "|".join((run_id, step_name, salt, *(_canonical(i) for i in inputs)))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return _KEY_PREFIX + digest[:32]


__all__ = ["fingerprint", "new_run_id", "step_idempotency_key"]
