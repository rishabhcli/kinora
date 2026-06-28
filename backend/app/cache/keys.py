"""Deterministic cache-key derivation.

Two concerns:

* **Qualification.** A logical key like ``"book:42"`` is qualified with its
  namespace (``"lib"``) into a single flat backend key ``"lib:book:42"`` so all
  backends share one keyspace and a namespace can be cleared/listed coherently.
* **Derivation.** The ``@cached`` decorator needs a *stable* key from a function's
  arguments. :func:`derive_key` builds one by canonicalising the bound arguments
  into JSON and hashing it — stable across runs, order-independent for kwargs,
  and collision-resistant. Long derived keys are hashed so a key never blows past
  Redis limits.

The hash is SHA-256 truncated to 32 hex chars (128 bits): far below any
practical collision risk for a cache while keeping keys short.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

#: Separator between namespace and the logical key (mirrors the codebase's
#: ``redis`` key style of ``a:b:c``).
NS_SEP = ":"

#: Derived keys longer than this are replaced by their hash.
_MAX_INLINE_KEY = 96


def qualify(namespace: str, key: str) -> str:
    """Join a namespace and a logical key into one backend key."""
    if not namespace:
        return key
    return f"{namespace}{NS_SEP}{key}"


def _canonical(value: Any) -> Any:
    """Reduce a value to a JSON-canonicalisable, order-stable form."""
    if isinstance(value, Mapping):
        return {str(k): _canonical(value[k]) for k in sorted(value, key=str)}
    if isinstance(value, str | bytes):
        return value.decode("utf-8", "replace") if isinstance(value, bytes) else value
    if isinstance(value, set | frozenset):
        return ["__set__", *sorted((_canonical(v) for v in value), key=repr)]
    if isinstance(value, Iterable) and not isinstance(value, str | bytes):
        return [_canonical(v) for v in value]
    return value


def fingerprint(*parts: Any) -> str:
    """A short stable hash of arbitrary (JSON-able-ish) parts."""
    canon = [_canonical(p) for p in parts]
    blob = json.dumps(canon, separators=(",", ":"), sort_keys=True, default=repr)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def derive_key(
    prefix: str,
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
    *,
    include: Iterable[str] | None = None,
    exclude: Iterable[str] | None = None,
) -> str:
    """Build a deterministic key for a call from ``prefix`` + arguments.

    ``include`` (if given) keeps only those kwarg names; ``exclude`` drops the
    named kwargs (e.g. a ``self`` / connection handle that must not influence the
    key). Positional args always participate.
    """
    kw = dict(kwargs)
    if include is not None:
        keep = set(include)
        kw = {k: v for k, v in kw.items() if k in keep}
    if exclude is not None:
        drop = set(exclude)
        kw = {k: v for k, v in kw.items() if k not in drop}
    fp = fingerprint(list(args), kw)
    candidate = f"{prefix}{NS_SEP}{fp}" if prefix else fp
    if len(candidate) > _MAX_INLINE_KEY:
        return hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:32]
    return candidate


__all__ = ["NS_SEP", "derive_key", "fingerprint", "qualify"]
