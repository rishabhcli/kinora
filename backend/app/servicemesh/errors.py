"""Error taxonomy for the service-mesh message/RPC contract layer.

Every failure a producer or consumer can hit on the contract path maps to one of
these. They are deliberately *pure* (no logging, no I/O) so callers decide how to
surface them; the consumer dispatcher (:mod:`app.servicemesh.consumer`) turns the
recoverable ones into dead-letter entries rather than crashing a worker loop.
"""

from __future__ import annotations

__all__ = [
    "ServiceMeshError",
    "SchemaError",
    "SchemaNotFoundError",
    "SchemaAlreadyRegisteredError",
    "SchemaHashMismatchError",
    "EnvelopeError",
    "EnvelopeDecodeError",
    "CompatibilityError",
    "BreakingChangeError",
    "ConversionError",
    "NoConversionPathError",
    "DispatchError",
    "UnknownSchemaError",
    "UnhandledVersionError",
    "NegotiationError",
    "VersionRangeError",
]


class ServiceMeshError(Exception):
    """Base class for every service-mesh contract failure."""


# --------------------------------------------------------------------------- #
# Schema registry / hashing.
# --------------------------------------------------------------------------- #
class SchemaError(ServiceMeshError):
    """A problem with a schema descriptor or its registration."""


class SchemaNotFoundError(SchemaError):
    """A schema id (optionally at a version) is not present in the registry."""


class SchemaAlreadyRegisteredError(SchemaError):
    """A (schema id, version) pair was registered twice with differing content."""


class SchemaHashMismatchError(SchemaError):
    """A schema's recomputed content hash disagrees with its recorded hash."""


# --------------------------------------------------------------------------- #
# Envelope.
# --------------------------------------------------------------------------- #
class EnvelopeError(ServiceMeshError):
    """A problem constructing or interpreting a message envelope."""


class EnvelopeDecodeError(EnvelopeError):
    """A raw byte string / mapping could not be parsed into an envelope."""


# --------------------------------------------------------------------------- #
# Compatibility (the CI gate).
# --------------------------------------------------------------------------- #
class CompatibilityError(ServiceMeshError):
    """A problem evaluating compatibility between two schema versions."""


class BreakingChangeError(CompatibilityError):
    """A schema change would break a channel held to a stability contract.

    Raised by the CI gate (:func:`app.servicemesh.compatibility.assert_evolution_allowed`)
    when the classified change is incompatible with the channel's declared
    compatibility mode.
    """


# --------------------------------------------------------------------------- #
# Converters.
# --------------------------------------------------------------------------- #
class ConversionError(ServiceMeshError):
    """A registered converter failed while transforming a payload."""


class NoConversionPathError(ConversionError):
    """No chain of migrators connects the source version to the target version."""


# --------------------------------------------------------------------------- #
# Consumer dispatch.
# --------------------------------------------------------------------------- #
class DispatchError(ServiceMeshError):
    """A problem routing an envelope to a handler."""


class UnknownSchemaError(DispatchError):
    """The envelope names a schema id the consumer has never been told about."""


class UnhandledVersionError(DispatchError):
    """The envelope's version has no handler and cannot be converted to one."""


# --------------------------------------------------------------------------- #
# Negotiation.
# --------------------------------------------------------------------------- #
class NegotiationError(ServiceMeshError):
    """Two roles could not agree on a common schema version."""


class VersionRangeError(ServiceMeshError):
    """A version range/spec string was malformed or internally inconsistent."""
