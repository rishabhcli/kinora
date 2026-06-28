"""``@cached`` — memoize an async function through a :class:`Cache`.

Wrap an ``async def`` and its results are cached (read-through, with all the
facade's stampede protection and negative caching). The key is derived from the
call arguments via :func:`~app.cache.keys.derive_key`, so identical calls share a
result and different arguments get distinct entries.

Examples::

    cache = Cache(MemoryCache(), namespace="canon-embed")

    @cached(cache, ttl=600, tags=lambda entity_id, **_: [f"entity:{entity_id}"])
    async def embed_entity(entity_id: str, version: int) -> list[float]:
        ...

    # Later, when an entity changes, drop just its embeddings:
    await cache.invalidate_tag(f"entity:{e}")

The wrapped function gains helpers:

* ``.cache_key(*args, **kwargs)`` -> the derived key (for manual invalidation),
* ``.invalidate(*args, **kwargs)`` -> drop the entry for those arguments, and
* ``.cache`` -> the backing :class:`Cache`.

``key`` can be a custom ``(args, kwargs) -> str`` builder; ``tags`` can be a
static list or a ``(*args, **kwargs) -> Iterable[str]`` callable resolved per
call, so a tag can depend on the arguments (the §8.7 cheap-edit pattern).
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Protocol, TypeVar, cast

from app.cache.cache import ABSENT, Cache
from app.cache.keys import derive_key

R = TypeVar("R")
R_co = TypeVar("R_co", covariant=True)

#: A user key builder: receives the positional + keyword args, returns a key.
KeyBuilder = Callable[..., str]
#: A user tag builder: receives the call args, returns the tags for this entry.
TagBuilder = Callable[..., Iterable[str]]


class CachedFunction(Protocol[R_co]):
    """The callable returned by :func:`cached`, with attached cache helpers."""

    cache: Cache[Any]

    def __call__(self, *args: Any, **kwargs: Any) -> Awaitable[R_co]: ...

    def cache_key(self, *args: Any, **kwargs: Any) -> str: ...

    def invalidate(self, *args: Any, **kwargs: Any) -> Awaitable[bool]: ...


def cached(
    cache: Cache[Any],
    *,
    ttl: float | None | object = ABSENT,
    tags: Iterable[str] | TagBuilder | None = None,
    key: KeyBuilder | None = None,
    key_prefix: str | None = None,
    include: Iterable[str] | None = None,
    exclude: Iterable[str] | None = None,
    cache_negatives: bool | None = None,
) -> Callable[[Callable[..., Awaitable[R]]], CachedFunction[R]]:
    """Decorator factory: memoize an async function through ``cache``.

    Args:
        cache: The :class:`Cache` instance to store results in.
        ttl: TTL override (defaults to the cache's ``default_ttl`` when ``...``).
        tags: Static tags, or a callable resolving tags from the call arguments.
        key: A custom key builder ``(*args, **kwargs) -> str``.
        key_prefix: Prefix for derived keys (defaults to the function's qualname).
        include / exclude: Restrict which kwargs feed the derived key (e.g. drop a
            ``self`` or a DB session that must not influence the key).
        cache_negatives: Per-decorator override of negative caching.
    """

    def _resolve_tags(args: tuple[Any, ...], kwargs: dict[str, Any]) -> list[str] | None:
        if tags is None:
            return None
        if callable(tags):
            return list(tags(*args, **kwargs))
        return list(tags)

    def decorator(func: Callable[..., Awaitable[R]]) -> CachedFunction[R]:
        prefix = key_prefix or f"{func.__module__}.{func.__qualname__}"

        def _key_for(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
            if key is not None:
                return key(*args, **kwargs)
            return derive_key(prefix, args, kwargs, include=include, exclude=exclude)

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> R:
            cache_key = _key_for(args, kwargs)
            resolved_tags = _resolve_tags(args, kwargs)

            async def _loader() -> R:
                return await func(*args, **kwargs)

            return cast(
                "R",
                await cache.get_or_load(
                    cache_key,
                    _loader,
                    ttl=ttl,
                    tags=resolved_tags,
                    cache_negatives=cache_negatives,
                ),
            )

        def cache_key_fn(*args: Any, **kwargs: Any) -> str:
            return _key_for(args, kwargs)

        async def invalidate_fn(*args: Any, **kwargs: Any) -> bool:
            return await cache.delete(_key_for(args, kwargs))

        wrapper.cache = cache  # type: ignore[attr-defined]
        wrapper.cache_key = cache_key_fn  # type: ignore[attr-defined]
        wrapper.invalidate = invalidate_fn  # type: ignore[attr-defined]
        return cast("CachedFunction[R]", wrapper)

    return decorator


__all__ = ["CachedFunction", "KeyBuilder", "TagBuilder", "cached"]
