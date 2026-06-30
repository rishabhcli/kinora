"""Typed errors for the runtime config plane.

These are raised only on the *write* / authoring path (registering a bad spec,
persisting a malformed override, attempting to raise a kill-switch). The *read*
path (:meth:`RuntimeConfigPlane.get` / ``is_enabled``) is a total function that
never raises into a caller — a misconfiguration there degrades to the flag's
base value. Keeping the failure modes typed lets the admin API translate them
into precise 4xx responses.
"""

from __future__ import annotations


class PlaneError(Exception):
    """Base class for every runtime config plane error."""


class UnknownFlagError(PlaneError, KeyError):
    """A flag key was referenced that is not registered in the plane.

    Subclasses :class:`KeyError` so callers that ``except KeyError`` around a
    registry lookup keep working, while the admin API can still match the more
    specific type to return a 404.
    """

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"unknown flag {key!r}")


class FlagTypeError(PlaneError, TypeError):
    """A value did not match the declared :class:`~app.flags.plane.spec.FlagType`.

    Raised when an override / rule / default carries a value the flag's type
    cannot represent (e.g. a string for an ``INT`` flag), so a bad value is
    rejected at write time rather than poisoning a later read.
    """

    def __init__(self, key: str, expected: str, got: object) -> None:
        self.key = key
        self.expected = expected
        self.got = got
        super().__init__(
            f"flag {key!r} expects {expected}, got {type(got).__name__}: {got!r}"
        )


class KillSwitchViolation(PlaneError):  # noqa: N818 - "Violation" reads truer than "Error" for a safety refusal
    """An override / rule tried to raise a guarded kill-switch above its base.

    The canonical case is forcing ``KINORA_LIVE_VIDEO`` *on* when the base
    (Settings) has it off. The plane refuses such a write outright so the live
    spend gate can never be lifted through the runtime surface — it can only be
    forced further *down*.
    """

    def __init__(self, key: str, base: object, attempted: object) -> None:
        self.key = key
        self.base = base
        self.attempted = attempted
        super().__init__(
            f"kill-switch {key!r} cannot be raised above its base value "
            f"{base!r} (attempted {attempted!r})"
        )


__all__ = [
    "FlagTypeError",
    "KillSwitchViolation",
    "PlaneError",
    "UnknownFlagError",
]
