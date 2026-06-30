"""Typed domain errors for the privacy / right-to-erasure subsystem.

Pure domain exceptions (no FastAPI import). Each carries a stable machine-readable
``code`` and an HTTP ``status`` hint a router can map onto the shared
:mod:`app.api.errors` envelope — the same contract the sibling
:mod:`app.compliance` errors follow, kept independent so neither imports the other.
"""

from __future__ import annotations


class PrivacyError(Exception):
    """Base class for every privacy-domain error.

    Attributes:
        code: stable machine-readable identifier (e.g. ``"datamap_invalid"``).
        status: the HTTP status a gateway should surface.
    """

    code: str = "privacy_error"
    status: int = 400

    def __init__(
        self, message: str, *, code: str | None = None, status: int | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status


class DataMapError(PrivacyError):
    """The declarative PII data-map is internally inconsistent (programmer error)."""

    code = "privacy_datamap_invalid"
    status = 500


class LegalHoldError(PrivacyError):
    """Right-to-erasure was blocked by an active legal hold (409)."""

    code = "privacy_legal_hold"
    status = 409

    def __init__(self, subject_id: str, hold_id: str) -> None:
        super().__init__(
            f"subject {subject_id!r} is under active legal hold {hold_id!r}; "
            "erasure is suspended until the hold is lifted",
        )
        self.subject_id = subject_id
        self.hold_id = hold_id


class ErasureIncompleteError(PrivacyError):
    """A completion certificate was requested but residual subject data remains."""

    code = "privacy_erasure_incomplete"
    status = 409


class ChainIntegrityError(PrivacyError):
    """A crypto-erasure redaction broke the audit/event hash chain (500 — bug)."""

    code = "privacy_chain_integrity"
    status = 500


class StoreError(PrivacyError):
    """A backing store raised while exporting or erasing (502 — dependency)."""

    code = "privacy_store_error"
    status = 502


__all__ = [
    "ChainIntegrityError",
    "DataMapError",
    "ErasureIncompleteError",
    "LegalHoldError",
    "PrivacyError",
    "StoreError",
]
