"""Exception hierarchy for the cache layer.

All cache errors derive from :class:`CacheError`, so callers can catch the whole
family. Backend transport failures (a Redis blip) raise :class:`CacheBackendError`;
the high-level :class:`~app.cache.cache.Cache` treats those as a soft miss when
``fail_open`` is set, so a Redis outage degrades to "no cache" instead of taking
the request path down with it (the §12.4 "the film never hard-stops" ethos
applied to the cache itself).
"""

from __future__ import annotations


class CacheError(RuntimeError):
    """Base class for every error raised by the cache layer."""


class CacheBackendError(CacheError):
    """A backend (e.g. Redis) transport / protocol failure.

    Wraps the underlying driver exception so business code never imports the
    ``redis`` exception types. The original is preserved as ``__cause__``.
    """


class SerializationError(CacheError):
    """A codec failed to encode or decode a value."""


class SingleFlightError(CacheError):
    """The single-flight leader raised while computing a value.

    Followers waiting on the same key receive this so the failure is shared
    rather than each follower independently re-running an expensive loader.
    """


__all__ = [
    "CacheBackendError",
    "CacheError",
    "SerializationError",
    "SingleFlightError",
]
