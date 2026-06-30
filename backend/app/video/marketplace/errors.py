"""Typed errors for the video-provider marketplace.

Every failure mode the subsystem can produce is a subclass of
:class:`MarketplaceError`, each carrying a stable ``code`` string so the API
layer (``app.video.marketplace.api``) can map it to an HTTP status without
string-sniffing the message. The hierarchy mirrors the three failure surfaces:

* registry / lookup (:class:`ListingNotFoundError`, :class:`DuplicateListingError`),
* validation (:class:`ListingValidationError`),
* lifecycle / wizard (:class:`OnboardingError`, :class:`GateFailedError`,
  :class:`InvalidTransitionError`, :class:`LifecycleError`).

None of these import any other Kinora subsystem — the marketplace owns its own
error vocabulary so it stays self-contained.
"""

from __future__ import annotations


class MarketplaceError(Exception):
    """Base class for every marketplace failure; carries a stable ``code``."""

    code = "marketplace_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ListingNotFoundError(MarketplaceError):
    """A listing key (or key@version) was not found in the catalog."""

    code = "listing_not_found"


class DuplicateListingError(MarketplaceError):
    """A listing with the same ``key`` is already registered."""

    code = "duplicate_listing"


class ListingValidationError(MarketplaceError):
    """A proposed listing / manifest failed structural validation."""

    code = "listing_validation"


class OnboardingError(MarketplaceError):
    """Base for onboarding-wizard failures."""

    code = "onboarding_error"


class InvalidTransitionError(OnboardingError):
    """An onboarding step was requested that is illegal from the current stage."""

    code = "invalid_transition"


class GateFailedError(OnboardingError):
    """A wizard gate refused to advance; the reasons explain *why* and *how to fix*."""

    code = "gate_failed"

    def __init__(self, message: str, *, reasons: list[str] | None = None) -> None:
        super().__init__(message)
        self.reasons = list(reasons or [])


class LifecycleError(MarketplaceError):
    """An illegal deprecation / sunset / retire transition was requested."""

    code = "lifecycle_error"


__all__ = [
    "DuplicateListingError",
    "GateFailedError",
    "InvalidTransitionError",
    "LifecycleError",
    "ListingNotFoundError",
    "ListingValidationError",
    "MarketplaceError",
    "OnboardingError",
]
