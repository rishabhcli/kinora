"""Event versioning + upcasters.

When a stored event's shape needs to change — a field renamed, a default added,
a value reshaped — we **never rewrite history**. Instead we bump the event's
:attr:`~app.eventsourcing.domain.events.DomainEvent.schema_version` and register
an *upcaster*: a pure function ``data(vN) -> data(vN+1)`` that migrates one
stored ``data`` mapping a single version forward.

On load, :func:`app.eventsourcing.domain.events.deserialise` walks the chain of
single-step upcasters from the stored version up to the class's current version,
so the rest of the system only ever sees the latest shape. Upcasters are pure
and step-wise (each migrates exactly one version) so they compose and are trivial
to test in isolation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

#: A single-version upcaster: ``data`` at version N -> ``data`` at version N+1.
Upcaster = Callable[[Mapping[str, object]], dict[str, object]]


@dataclass(frozen=True, slots=True)
class _Step:
    event_type: str
    from_version: int  # migrates from_version -> from_version + 1
    fn: Upcaster


class UpcasterRegistry:
    """A registry of single-step upcasters keyed by ``(event_type, from_version)``.

    Register one step per version bump; :meth:`upcast` chains them. There must be
    a contiguous run of steps from the stored version to the target, or
    :meth:`upcast` raises :class:`MissingUpcasterError`.
    """

    def __init__(self) -> None:
        self._steps: dict[tuple[str, int], _Step] = {}

    def register(self, event_type: str, from_version: int, fn: Upcaster) -> None:
        """Register the upcaster that migrates ``from_version -> from_version + 1``."""
        if from_version < 1:
            raise ValueError("from_version must be >= 1")
        key = (event_type, from_version)
        if key in self._steps:
            raise ValueError(
                f"upcaster for {event_type!r} v{from_version}->v{from_version + 1} "
                "already registered"
            )
        self._steps[key] = _Step(event_type, from_version, fn)

    def step(self, event_type: str, from_version: int) -> Callable[[Upcaster], Upcaster]:
        """Decorator form of :meth:`register`."""

        def deco(fn: Upcaster) -> Upcaster:
            self.register(event_type, from_version, fn)
            return fn

        return deco

    def upcast(
        self,
        event_type: str,
        stored_version: int,
        target_version: int,
        data: Mapping[str, object],
    ) -> dict[str, object]:
        """Migrate ``data`` from ``stored_version`` up to ``target_version``.

        Returns the data unchanged when already at/above target.

        Raises:
            MissingUpcasterError: a step in the chain is not registered.
        """
        if stored_version >= target_version:
            return dict(data)
        current: dict[str, object] = dict(data)
        version = stored_version
        while version < target_version:
            step = self._steps.get((event_type, version))
            if step is None:
                raise MissingUpcasterError(event_type, version)
            current = step.fn(current)
            version += 1
        return current

    def has_chain(self, event_type: str, stored_version: int, target_version: int) -> bool:
        """Whether a complete step chain exists for the given migration."""
        return all((event_type, v) in self._steps for v in range(stored_version, target_version))


class MissingUpcasterError(KeyError):
    """Raised when a required ``(event_type, version)`` upcaster is not registered."""

    def __init__(self, event_type: str, from_version: int) -> None:
        self.event_type = event_type
        self.from_version = from_version
        super().__init__(f"no upcaster for {event_type!r} v{from_version}->v{from_version + 1}")


#: The process-wide upcaster registry domain modules register migrations into.
upcasters = UpcasterRegistry()


__all__ = [
    "MissingUpcasterError",
    "Upcaster",
    "UpcasterRegistry",
    "upcasters",
]
