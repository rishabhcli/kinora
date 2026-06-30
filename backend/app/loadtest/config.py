"""Additive, self-contained settings for the load-testing harness.

The harness is a *tool*, not part of the request-serving app, so rather than add
fields to the central :class:`app.core.config.Settings` (which every entrypoint
loads) its knobs live here under their own ``KINORA_LOADTEST_`` env prefix. This
keeps the change strictly additive — importing the app never sees these — and a
real run reads them from the environment while tests construct plans directly.

These are *defaults / safety rails*, not the plan itself: the target base URL for
a real HTTP run, default per-request timeout, and the hard guards that keep a
load run from ever spending money or hammering production by accident.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class LoadtestSettings(BaseSettings):
    """Knobs + safety rails for a real (wall-clock) load run."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="kinora_loadtest_",
        case_sensitive=False,
        extra="ignore",
    )

    #: Base URL of the API a real run targets. Defaults to local; a run must set
    #: this explicitly to point anywhere else.
    target_base_url: str = "http://localhost:8000"
    #: Default per-request deadline (seconds) when a plan does not set one.
    default_timeout_s: float = 10.0
    #: Default open-loop in-flight cap (backpressure). ``0`` = unbounded.
    default_max_inflight: int = 0
    #: Hard refusal: never run against a host containing any of these substrings
    #: unless ``allow_production`` is explicitly set. Prevents an accidental load
    #: test against prod from a misconfigured env.
    blocked_host_substrings: tuple[str, ...] = ("prod", "production")
    allow_production: bool = False

    def guard_target(self, base_url: str) -> None:
        """Raise if ``base_url`` looks like production and that is not allowed."""
        if self.allow_production:
            return
        lowered = base_url.lower()
        for needle in self.blocked_host_substrings:
            if needle and needle in lowered:
                raise RuntimeError(
                    f"refusing to load-test {base_url!r}: looks like production "
                    f"(matched {needle!r}). Set KINORA_LOADTEST_ALLOW_PRODUCTION=1 "
                    "to override."
                )


@lru_cache
def get_loadtest_settings() -> LoadtestSettings:
    """Return a process-wide cached :class:`LoadtestSettings`."""
    return LoadtestSettings()
