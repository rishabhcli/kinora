"""Typed domain errors for the zero-trust defense subsystem.

These are *domain* exceptions (no FastAPI import) so the package stays a pure
library. A router boundary owned elsewhere may translate them onto the shared
``app.api.errors`` envelope; this package never imports the gateway.

Every error carries a stable machine-readable ``code`` and an HTTP ``status``
hint, mirroring the convention in :mod:`app.compliance.errors`.
"""

from __future__ import annotations


class DefenseError(Exception):
    """Base class for every defense-domain error."""

    code: str = "defense_error"
    status: int = 400

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status


class ConfigError(DefenseError):
    """An invalid detector / policy configuration (programming error, 400)."""

    code = "defense_config_error"
    status = 400


class RuleCompileError(DefenseError):
    """A WAF rule or signature failed to compile (bad regex, bad shape)."""

    code = "defense_rule_compile_error"
    status = 400


class LockfileParseError(DefenseError):
    """A supply-chain lockfile could not be parsed into components."""

    code = "defense_lockfile_parse_error"
    status = 422


class ProvenanceError(DefenseError):
    """An artifact's signature or provenance failed verification."""

    code = "defense_provenance_error"
    status = 422


class NotFoundError(DefenseError):
    """A referenced defense entity (alert, case) does not exist (404)."""

    code = "defense_not_found"
    status = 404
