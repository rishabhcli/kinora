"""Secret-backend abstraction: typed resolution, caching, rotation, redaction.

Kinora reads most secrets straight off :class:`~app.core.config.Settings` (which
loads them from the environment / ``backend/.env``). That is fine for the
single-node dev/demo posture, but a production deployment wants secrets to come
from a *secret store* — a mounted file, AWS/GCP secret manager, Vault — fetched
lazily, cached with a TTL, and rotated without a restart. This module is that
seam, deliberately **additive**: it does not change how Settings loads; it sits
beside it so callers that want managed secrets can opt in.

Design:

* :class:`SecretRef` — a typed reference to a secret (a logical name) plus an
  optional ``version``; resolution returns a :class:`SecretValue`.
* :class:`SecretValue` — the resolved material wrapped so it never prints in a
  ``repr``/``str``/log (defends against accidental leakage). ``.reveal()`` is the
  single, explicit way to read the plaintext.
* :class:`SecretBackend` — the protocol every backend satisfies (``fetch``).
* :class:`EnvSecretBackend` / :class:`FileSecretBackend` / :class:`StaticSecretBackend`
  — the built-in backends (env vars, a directory of files à la Docker/K8s
  secrets, and an in-memory map for tests / pluggable composition).
* :class:`SecretResolver` — the façade: a primary backend plus optional
  fallbacks, a TTL cache, rotation hooks, and audited misses. Pluggable Vault
  backends drop in by satisfying :class:`SecretBackend` — no resolver change.

Nothing here performs network I/O on import, and the default backends touch only
the process environment / the local filesystem, so tests stay hermetic.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.core.logging import REDACTED, get_logger

__all__ = [
    "SecretRef",
    "SecretValue",
    "SecretNotFoundError",
    "SecretBackend",
    "EnvSecretBackend",
    "FileSecretBackend",
    "StaticSecretBackend",
    "SecretResolver",
    "RotationHook",
]

_log = get_logger("configmgmt.secrets")


class SecretNotFoundError(KeyError):
    """Raised when a required secret cannot be resolved from any backend."""


@dataclass(frozen=True, slots=True)
class SecretRef:
    """A typed reference to a secret.

    Args:
        name: The logical secret name (e.g. ``"dashscope_api_key"``). Backends
            map this to their native key (an env var, a file name, a Vault path).
        version: Optional version pin; ``None`` means "current". A rotation
            increments the live version, so pinning a stale version still
            resolves the cached prior value until it ages out.
        required: When ``True`` a miss raises; when ``False`` a miss returns
            ``None`` from :meth:`SecretResolver.resolve_optional`.
    """

    name: str
    version: str | None = None
    required: bool = True

    @property
    def cache_key(self) -> str:
        """Stable key for the resolver cache (name + version)."""
        return f"{self.name}@{self.version or 'current'}"


class SecretValue:
    """Resolved secret material that refuses to print itself.

    ``repr``/``str`` render ``[REDACTED]`` (matching :data:`app.core.logging`),
    so a ``SecretValue`` is safe to log, put in an exception, or drop into a
    structlog event. The plaintext is read *only* via :meth:`reveal`.
    """

    __slots__ = ("_value", "name", "version", "source", "fetched_at")

    def __init__(
        self,
        value: str,
        *,
        name: str,
        version: str | None = None,
        source: str = "unknown",
        fetched_at: float | None = None,
    ) -> None:
        self._value = value
        self.name = name
        self.version = version
        self.source = source
        self.fetched_at = time.monotonic() if fetched_at is None else fetched_at

    def reveal(self) -> str:
        """Return the plaintext secret. The only path that exposes it."""
        return self._value

    def __len__(self) -> int:
        return len(self._value)

    def __bool__(self) -> bool:
        return bool(self._value)

    def __eq__(self, other: object) -> bool:
        # Compare by material so rotation no-ops are detectable, but never expose
        # the value in the process. Constant-time is unnecessary here (this is a
        # config-plane equality, not an auth check).
        if isinstance(other, SecretValue):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.name, self.version))

    def __repr__(self) -> str:
        return f"SecretValue(name={self.name!r}, version={self.version!r}, value={REDACTED})"

    def __str__(self) -> str:
        return REDACTED


@runtime_checkable
class SecretBackend(Protocol):
    """A source of secret material. Pluggable: Vault etc. satisfy this."""

    name: str

    def fetch(self, ref: SecretRef) -> str | None:
        """Return the plaintext for ``ref`` or ``None`` if this backend lacks it."""
        ...


@dataclass(slots=True)
class EnvSecretBackend:
    """Resolve secrets from the process environment.

    The logical name is upper-cased to the env-var convention
    (``dashscope_api_key`` -> ``DASHSCOPE_API_KEY``); a ``prefix`` namespaces a
    deployment's vars. Version pins are ignored (env has no versioning).
    """

    prefix: str = ""
    name: str = "env"
    _environ: Mapping[str, str] = field(default_factory=lambda: os.environ)

    def fetch(self, ref: SecretRef) -> str | None:
        key = f"{self.prefix}{ref.name}".upper()
        return self._environ.get(key)


@dataclass(slots=True)
class FileSecretBackend:
    """Resolve secrets from a directory of files (Docker / K8s secret mounts).

    ``<root>/<name>`` holds the plaintext; a trailing newline (the common
    ``echo``/editor artefact) is stripped. ``<root>/<name>.<version>`` is read
    when a version is pinned, falling back to the unversioned file.
    """

    root: Path
    name: str = "file"

    def __init__(self, root: str | Path, *, name: str = "file") -> None:
        self.root = Path(root)
        self.name = name

    def fetch(self, ref: SecretRef) -> str | None:
        candidates = []
        if ref.version:
            candidates.append(self.root / f"{ref.name}.{ref.version}")
        candidates.append(self.root / ref.name)
        for path in candidates:
            try:
                if path.is_file():
                    return path.read_text(encoding="utf-8").rstrip("\n")
            except OSError as exc:  # pragma: no cover - defensive
                _log.warning("secret_file_unreadable", path=str(path), error=str(exc))
        return None


@dataclass(slots=True)
class StaticSecretBackend:
    """In-memory secret map. The pluggable / test backend.

    Keys are ``name`` or ``name@version``; a versioned lookup falls back to the
    unversioned entry. :meth:`put` mutates the map so tests can simulate rotation
    without a real store.
    """

    values: dict[str, str] = field(default_factory=dict)
    name: str = "static"

    def put(self, name: str, value: str, *, version: str | None = None) -> None:
        """Set (or rotate) a secret in the map."""
        self.values[f"{name}@{version}" if version else name] = value

    def fetch(self, ref: SecretRef) -> str | None:
        if ref.version is not None:
            versioned = self.values.get(f"{ref.name}@{ref.version}")
            if versioned is not None:
                return versioned
        return self.values.get(ref.name)


#: A rotation hook is notified when a resolved secret's material changes
#: (old value may be ``None`` on first resolution). Hooks must not raise.
RotationHook = Callable[[str, SecretValue | None, SecretValue], None]


@dataclass(slots=True)
class _CacheEntry:
    value: SecretValue
    expires_at: float  # monotonic deadline; math.inf == never


class SecretResolver:
    """Resolve, cache, and rotate secrets across a chain of backends.

    The resolver tries the primary backend then each fallback in order, caches
    the first hit for ``ttl_s`` seconds, and fires rotation hooks when a
    re-resolution yields different material. ``ttl_s <= 0`` disables caching
    (every resolve re-fetches), which is what tests use to drive rotation.
    """

    def __init__(
        self,
        backend: SecretBackend,
        *,
        fallbacks: tuple[SecretBackend, ...] = (),
        ttl_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._backends: tuple[SecretBackend, ...] = (backend, *fallbacks)
        self._ttl_s = ttl_s
        self._clock = clock
        self._cache: dict[str, _CacheEntry] = {}
        self._hooks: list[RotationHook] = []

    @property
    def backends(self) -> tuple[SecretBackend, ...]:
        """The ordered backend chain (primary first)."""
        return self._backends

    def add_rotation_hook(self, hook: RotationHook) -> None:
        """Register a callback fired when a secret's material changes."""
        self._hooks.append(hook)

    def _fetch_uncached(self, ref: SecretRef) -> SecretValue | None:
        for backend in self._backends:
            raw = backend.fetch(ref)
            if raw is not None:
                return SecretValue(
                    raw,
                    name=ref.name,
                    version=ref.version,
                    source=backend.name,
                    fetched_at=self._clock(),
                )
        return None

    def resolve_optional(self, ref: SecretRef | str) -> SecretValue | None:
        """Resolve ``ref``; return ``None`` on a miss (no raise).

        A cache hit within TTL is returned as-is. A miss past TTL re-fetches; if
        the material changed, rotation hooks fire and the cache updates.
        """
        if isinstance(ref, str):
            ref = SecretRef(ref)
        now = self._clock()
        entry = self._cache.get(ref.cache_key)
        if entry is not None and now < entry.expires_at:
            return entry.value

        fresh = self._fetch_uncached(ref)
        previous = entry.value if entry is not None else None
        if fresh is None:
            # Miss: drop any stale cache entry so a later put() is seen promptly.
            self._cache.pop(ref.cache_key, None)
            _log.debug("secret_miss", name=ref.name, version=ref.version)
            return None

        if previous is None or previous != fresh:
            self._notify_rotation(ref.name, previous, fresh)
        self._cache[ref.cache_key] = _CacheEntry(
            value=fresh,
            expires_at=(now + self._ttl_s) if self._ttl_s > 0 else now - 1.0,
        )
        return fresh

    def resolve(self, ref: SecretRef | str) -> SecretValue:
        """Resolve a required secret; raise :class:`SecretNotFoundError` on miss."""
        if isinstance(ref, str):
            ref = SecretRef(ref)
        value = self.resolve_optional(ref)
        if value is None:
            raise SecretNotFoundError(
                f"secret {ref.name!r} (version={ref.version!r}) not found in "
                f"backends {[b.name for b in self._backends]}"
            )
        return value

    def reveal(self, ref: SecretRef | str) -> str:
        """Convenience: resolve a required secret and return its plaintext."""
        return self.resolve(ref).reveal()

    def invalidate(self, name: str | None = None) -> None:
        """Drop cached secrets so the next resolve re-fetches.

        ``name=None`` clears the whole cache; a name clears every version of it
        (the next resolve picks up a rotated value immediately).
        """
        if name is None:
            self._cache.clear()
            return
        for key in [k for k in self._cache if k == name or k.startswith(f"{name}@")]:
            self._cache.pop(key, None)

    def _notify_rotation(self, name: str, old: SecretValue | None, new: SecretValue) -> None:
        _log.info("secret_rotated", name=name, source=new.source, first=old is None)
        for hook in self._hooks:
            try:
                hook(name, old, new)
            except Exception as exc:  # pragma: no cover - hook isolation
                _log.warning("secret_rotation_hook_failed", name=name, error=str(exc))


def env_resolver(*, prefix: str = "", ttl_s: float = 300.0) -> SecretResolver:
    """Build the default env-backed resolver (the production-friendly default)."""
    return SecretResolver(EnvSecretBackend(prefix=prefix), ttl_s=ttl_s)
