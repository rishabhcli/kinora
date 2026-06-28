"""Typed domain errors for the compliance subsystem.

These are *domain* exceptions (no FastAPI import), translated to the gateway's
``APIError`` envelope at the router boundary (:mod:`app.compliance.api.routes`)
so the shared :mod:`app.api.errors` handler stays untouched (additive-only rule).

Every error carries a stable ``code`` (machine-readable) and an HTTP ``status``
hint the router maps onto the typed JSON envelope.
"""

from __future__ import annotations


class ComplianceError(Exception):
    """Base class for every compliance-domain error.

    Attributes:
        code: stable machine-readable identifier (e.g. ``"policy_not_found"``).
        status: the HTTP status the gateway should surface.
    """

    code: str = "compliance_error"
    status: int = 400

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status


class NotFoundError(ComplianceError):
    """A referenced compliance entity does not exist (404)."""

    code = "compliance_not_found"
    status = 404


class ConflictError(ComplianceError):
    """A request conflicts with current state (409) — e.g. activating a draft twice."""

    code = "compliance_conflict"
    status = 409


class InvalidTransitionError(ComplianceError):
    """An illegal DSAR / policy / hold state transition was requested (409)."""

    code = "compliance_invalid_transition"
    status = 409


class ConsentRequiredError(ComplianceError):
    """An action was blocked because the subject has not granted the needed consent (403)."""

    code = "compliance_consent_required"
    status = 403

    def __init__(self, purpose: str, subject_id: str) -> None:
        super().__init__(
            f"subject {subject_id!r} has not consented to purpose {purpose!r}",
        )
        self.purpose = purpose
        self.subject_id = subject_id


class LegalHoldError(ComplianceError):
    """An erasure / expiry was blocked by an active legal hold (409)."""

    code = "compliance_legal_hold"
    status = 409

    def __init__(self, subject_id: str, hold_id: str) -> None:
        super().__init__(
            f"subject {subject_id!r} is under active legal hold {hold_id!r}; "
            "erasure and expiry are suspended",
        )
        self.subject_id = subject_id
        self.hold_id = hold_id


class LedgerIntegrityError(ComplianceError):
    """The hash-chained compliance ledger failed verification (500 — internal)."""

    code = "compliance_ledger_integrity"
    status = 500


class PolicyEvaluationError(ComplianceError):
    """A policy-as-code rule could not be evaluated (e.g. unknown predicate)."""

    code = "compliance_policy_evaluation"
    status = 400


__all__ = [
    "ComplianceError",
    "ConflictError",
    "ConsentRequiredError",
    "InvalidTransitionError",
    "LedgerIntegrityError",
    "LegalHoldError",
    "NotFoundError",
    "PolicyEvaluationError",
]
