"""Typed errors for the delivery / ABR-packaging subsystem.

A small, dependency-free exception tree so callers can distinguish a *planning*
fault (a bad ladder, an unknown provider profile, an inconsistent manifest)
from a *packaging* fault (ffmpeg/segmenter failure) without catching bare
:class:`Exception`. The pure plan layer raises only :class:`DeliveryError`
subclasses; ffmpeg execution wraps the underlying tool failure in
:class:`PackagingError`.
"""

from __future__ import annotations


class DeliveryError(Exception):
    """Base class for every error this subsystem raises."""


class LadderError(DeliveryError):
    """A rendition ladder is empty, malformed, or has duplicate rungs."""


class ProfileError(DeliveryError):
    """An unknown provider profile, or a profile that cannot be normalized."""


class ManifestError(DeliveryError):
    """A manifest is inconsistent (bad segment durations, target-duration breach, …)."""


class SegmentationError(DeliveryError):
    """A segmentation plan is impossible (e.g. non-positive segment duration)."""


class PackagingError(DeliveryError):
    """The external packager/transcoder (ffmpeg) failed to produce an artifact."""


class SigningError(DeliveryError):
    """A playback token could not be minted or verified."""
