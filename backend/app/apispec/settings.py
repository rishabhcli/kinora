"""Additive, self-contained settings for the API-spec subsystem (``app.apispec``).

These knobs live in their *own* pydantic-settings model rather than mutating the
shared :class:`app.core.config.Settings`, so the enricher/diff/generator can be
configured (and unit-tested) without touching the production config surface. All
fields default to safe values: the enricher is opt-in via :func:`install`, the
diff gate is *advisory* by default (it reports breaks, never raises in-process),
and the generator/contract tooling read these only when explicitly invoked.

Environment variables use the ``KINORA_APISPEC_`` prefix (e.g.
``KINORA_APISPEC_PUBLIC_SERVER_URL``). Nothing here reads the network, spends
budget, or depends on infra — it is pure spec metadata.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiSpecSettings(BaseSettings):
    """Configuration for OpenAPI enrichment + client generation."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="KINORA_APISPEC_",
        case_sensitive=False,
        extra="ignore",
    )

    #: When ``True`` the enriched ``custom_openapi`` hook is installed on the app
    #: at startup. Off by default so the bare FastAPI behaviour is the baseline
    #: and the enricher is opt-in (it never changes runtime responses regardless).
    enabled: bool = False

    #: The advertised public base URL placed first in the spec's ``servers`` list.
    #: The desktop renderer talks to ``http://localhost:8000`` in dev; production
    #: deployments override this so generated clients point at the real gateway.
    public_server_url: str = "http://localhost:8000"

    #: A human label for the public server entry.
    public_server_description: str = "Kinora API gateway"

    #: Whether to also advertise the local dev server entry. Harmless extra
    #: metadata; some client generators surface it as a selectable target.
    include_local_server: bool = True

    #: The local dev server URL (only emitted when ``include_local_server``).
    local_server_url: str = "http://localhost:8000"

    #: Contact + license metadata stamped into ``info`` (spec hygiene).
    contact_name: str = "Kinora"
    contact_url: str = "https://github.com/kinora"
    license_name: str = "Proprietary"

    #: When ``True`` a breaking-change diff against the golden spec *raises* (the
    #: hard contract gate). Off by default: the diff is computed and reported but
    #: never blocks process startup; CI / the test suite flips this to fail hard.
    diff_strict: bool = False


@lru_cache
def get_apispec_settings() -> ApiSpecSettings:
    """Return a process-wide cached :class:`ApiSpecSettings`."""
    return ApiSpecSettings()


__all__ = ["ApiSpecSettings", "get_apispec_settings"]
