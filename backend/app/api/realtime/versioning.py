"""API versioning + deprecation signalling (kinora.md §12 — the unglamorous 30%).

The gateway mounts everything under ``/api`` (an implicit ``v1``). As the surface
evolves we need a disciplined way to *retire* an endpoint without breaking a
shipped desktop build: announce a deprecation, give a sunset date, point at the
replacement — all in standard headers a client can read programmatically.

This module supplies:

* the **standard headers** — ``Deprecation`` (RFC 8594), ``Sunset``, and a
  ``Link rel="deprecation"`` / ``rel="successor-version"`` — built by
  :func:`deprecation_headers`;
* a :func:`deprecated` route-decorator that stamps those headers on a response
  *and* registers the route in a process-wide :data:`REGISTRY` so the
  ``/api/versions`` endpoint can enumerate the contract (what's current, what's
  deprecated, when it sunsets, what supersedes it);
* :data:`API_VERSIONS` — the declared version manifest the meta endpoint returns.

It is intentionally framework-light: the decorator works on any async route by
mutating the FastAPI ``Response`` the route already receives, so it composes with
the existing dependency-injection style without new middleware.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Any, TypeVar

from fastapi import Response

#: The current (and only) live API major version. ``/api`` == this.
CURRENT_VERSION = "v1"

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


@dataclass(frozen=True, slots=True)
class VersionInfo:
    """One declared API version in the manifest."""

    version: str
    status: str  # "current" | "deprecated" | "sunset"
    released: str
    prefix: str
    notes: str = ""

    def to_public(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "status": self.status,
            "released": self.released,
            "prefix": self.prefix,
            "notes": self.notes,
        }


#: The version manifest served at ``/api/versions``.
API_VERSIONS: tuple[VersionInfo, ...] = (
    VersionInfo(
        version="v1",
        status="current",
        released="2026-01-01",
        prefix="/api",
        notes="Generation-on-scroll showrunner API. SSE/WS realtime, "
        "cursor pagination, idempotency keys, presence.",
    ),
)


@dataclass(frozen=True, slots=True)
class DeprecatedRoute:
    """A registered deprecation (powers the ``/versions`` deprecations list)."""

    name: str
    since: str
    sunset: str | None
    successor: str | None
    note: str

    def to_public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "since": self.since,
            "sunset": self.sunset,
            "successor": self.successor,
            "note": self.note,
        }


#: Process-wide registry of deprecated routes (populated by the decorator).
REGISTRY: dict[str, DeprecatedRoute] = {}


def _http_date(value: str | None) -> str | None:
    """Render an ISO date as an RFC 1123 HTTP-date (Sunset/Deprecation want that)."""
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return value
    # Dates are timezone-naive; emit the 00:00:00 GMT form clients expect.
    return parsed.strftime("%a, %d %b %Y 00:00:00 GMT")


def deprecation_headers(
    *, since: str, sunset: str | None = None, successor: str | None = None
) -> dict[str, str]:
    """Build the RFC 8594 deprecation header set for a response."""
    headers: dict[str, str] = {}
    since_http = _http_date(since)
    if since_http:
        headers["Deprecation"] = since_http
    sunset_http = _http_date(sunset)
    if sunset_http:
        headers["Sunset"] = sunset_http
    links: list[str] = []
    if successor:
        links.append(f'<{successor}>; rel="successor-version"')
    if links:
        headers["Link"] = ", ".join(links)
    return headers


def stamp(
    response: Response, *, since: str, sunset: str | None = None, successor: str | None = None
) -> None:
    """Apply deprecation headers to an in-flight FastAPI ``Response``."""
    for name, value in deprecation_headers(since=since, sunset=sunset, successor=successor).items():
        response.headers[name] = value


def deprecated(
    *,
    since: str,
    sunset: str | None = None,
    successor: str | None = None,
    note: str = "",
) -> Callable[[F], F]:
    """Decorate an async route to mark it deprecated.

    Registers the route in :data:`REGISTRY` and stamps the deprecation headers on
    the ``Response`` the route declares as a parameter (FastAPI injects it). The
    route keeps working — deprecation is an announcement, not a removal.
    """

    def decorator(func: F) -> F:
        REGISTRY[func.__name__] = DeprecatedRoute(
            name=func.__name__, since=since, sunset=sunset, successor=successor, note=note
        )

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            result = await func(*args, **kwargs)
            response = kwargs.get("response")
            if isinstance(response, Response):
                stamp(response, since=since, sunset=sunset, successor=successor)
            return result

        return wrapper  # type: ignore[return-value]

    return decorator


def version_manifest() -> dict[str, Any]:
    """The full ``/versions`` payload: versions + active deprecations."""
    return {
        "current": CURRENT_VERSION,
        "versions": [v.to_public() for v in API_VERSIONS],
        "deprecations": [d.to_public() for d in REGISTRY.values()],
    }


__all__ = [
    "API_VERSIONS",
    "CURRENT_VERSION",
    "REGISTRY",
    "DeprecatedRoute",
    "VersionInfo",
    "deprecated",
    "deprecation_headers",
    "stamp",
    "version_manifest",
]
