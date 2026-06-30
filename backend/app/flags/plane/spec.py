"""The typed flag *spec* — a self-describing, validated runtime-config knob.

A :class:`FlagSpec` declares one runtime flag: its key, value :class:`FlagType`,
default (the base value, usually mirrored from a :class:`Settings` field),
human description, owning team, and — crucially — whether it is a *guarded
kill-switch* whose value may only ever be forced *down*.

The spec owns **type discipline**: :meth:`FlagSpec.coerce` is the single place
that turns a loosely-typed inbound value (an override from JSON, a Settings
field) into the canonical Python type, and :meth:`FlagSpec.validate` rejects
anything that cannot. This keeps every other module (registry, resolver,
overrides) free of ``isinstance`` ladders — they trust a coerced value.

Pure: imports nothing but stdlib + the plane's errors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.flags.plane.errors import FlagTypeError

#: The set of JSON-representable scalar/container types a flag value can hold.
FlagValue = bool | int | float | str | dict[str, Any] | list[Any] | None


class FlagType(StrEnum):
    """The value type a flag carries (drives coercion + admin-API validation)."""

    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    STRING = "string"
    JSON = "json"  # an arbitrary JSON object / array

    def coerce(self, key: str, value: Any) -> FlagValue:
        """Coerce ``value`` to this type's canonical Python form, or raise.

        Coercion is deliberately *narrow* — it accepts the obvious equivalences
        (``int`` for a ``FLOAT`` flag, ``"true"``/``1`` for a ``BOOL`` flag from
        a string env source) but rejects nonsense (a ``str`` body for an ``INT``
        flag) so a typo in an override surfaces as a 4xx, not a silent default.
        """
        if value is None:
            return None
        match self:
            case FlagType.BOOL:
                return _coerce_bool(key, value)
            case FlagType.INT:
                return _coerce_int(key, value)
            case FlagType.FLOAT:
                return _coerce_float(key, value)
            case FlagType.STRING:
                if isinstance(value, str):
                    return value
                if isinstance(value, bool | int | float):
                    return str(value)
                raise FlagTypeError(key, "string", value)
            case FlagType.JSON:
                if isinstance(value, dict | list):
                    return value
                raise FlagTypeError(key, "json object/array", value)
        raise FlagTypeError(key, str(self), value)  # pragma: no cover - exhaustive match


def _coerce_bool(key: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
    raise FlagTypeError(key, "bool", value)


def _coerce_int(key: str, value: Any) -> int:
    if isinstance(value, bool):  # bool is an int subclass — reject explicitly
        raise FlagTypeError(key, "int", value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            pass
    raise FlagTypeError(key, "int", value)


def _coerce_float(key: str, value: Any) -> float:
    if isinstance(value, bool):
        raise FlagTypeError(key, "float", value)
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            pass
    raise FlagTypeError(key, "float", value)


@dataclass(frozen=True, slots=True)
class FlagSpec:
    """The immutable declaration of one runtime flag.

    ``default`` is the base value when no override/rule applies — for flags that
    mirror a Settings field, the registry stamps the live Settings value here so
    the plane's base layer *is* Settings.

    ``kill_switch`` marks a flag whose value may only be forced *toward off /
    down*. For a ``BOOL`` kill-switch that means an override may set it to
    ``False`` (or leave it) but never to ``True`` when the base is ``False``;
    the safety layer enforces the precise rule (see
    :mod:`app.flags.plane.safety`). ``KINORA_LIVE_VIDEO`` is the archetype.

    ``setting`` records the Settings field this flag mirrors (informational; used
    by the registry to read the base value and by the admin API for provenance).
    """

    key: str
    type: FlagType
    default: FlagValue
    description: str = ""
    owner: str = "platform"
    kill_switch: bool = False
    setting: str | None = None
    tags: tuple[str, ...] = ()
    #: Optional allow-list of legal string values (for STRING flags that are
    #: really an enum, e.g. ``video_backend``). Empty → any string.
    choices: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.key:
            raise FlagTypeError("<empty>", "non-empty key", self.key)
        # Validate the default eagerly so a registry can never hold a bad spec.
        coerced = self.type.coerce(self.key, self.default)
        object.__setattr__(self, "default", coerced)
        if self.choices and self.type is not FlagType.STRING:
            raise FlagTypeError(self.key, "STRING flag for choices", self.type)
        if self.choices and coerced is not None and coerced not in self.choices:
            raise FlagTypeError(self.key, f"one of {self.choices}", coerced)

    def coerce(self, value: Any) -> FlagValue:
        """Coerce ``value`` to this flag's type (raises :class:`FlagTypeError`)."""
        coerced = self.type.coerce(self.key, value)
        if self.choices and coerced is not None and coerced not in self.choices:
            raise FlagTypeError(self.key, f"one of {self.choices}", coerced)
        return coerced

    def with_default(self, value: FlagValue) -> FlagSpec:
        """Return a copy whose default is ``value`` (used to bind the live base)."""
        return FlagSpec(
            key=self.key,
            type=self.type,
            default=value,
            description=self.description,
            owner=self.owner,
            kill_switch=self.kill_switch,
            setting=self.setting,
            tags=self.tags,
            choices=self.choices,
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe spec projection for the admin API / snapshot."""
        return {
            "key": self.key,
            "type": self.type.value,
            "default": self.default,
            "description": self.description,
            "owner": self.owner,
            "kill_switch": self.kill_switch,
            "setting": self.setting,
            "tags": list(self.tags),
            "choices": list(self.choices),
        }


__all__ = ["FlagSpec", "FlagType", "FlagValue"]
