"""A tiny declarative config schema for plugin configuration.

A plugin needs configuration — an API key, a base URL, a default model id, a
timeout. The manifest declares the *shape* of that config (field name, type,
whether it is required, a default, and crucially whether it is a **secret**), and
this module validates a concrete config payload against that shape before a
plugin is ever instantiated.

Why not just hand the plugin a raw dict? Two reasons:

* **Fail fast, fail typed.** A missing required field or a wrong-typed value is
  caught at load with a :class:`~app.video.plugins.errors.ConfigSchemaError`
  naming the field — not as an ``AttributeError`` deep inside third-party code.
* **Secret hygiene.** Fields flagged ``secret=True`` are redacted by
  :meth:`ConfigSchema.redact` so the host can log a plugin's *resolved* config
  for debugging without leaking credentials. The sandbox hands the plugin only
  the validated config — never the host's environment — so the declared secret
  fields are the *only* credentials a plugin ever sees (see the no-ambient-creds
  guarantee in :mod:`app.video.plugins.sandbox`).

The schema is intentionally minimal (str/int/float/bool); a plugin needing
richer config layers its own validation on top of the validated primitives.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from app.video.plugins.errors import ConfigSchemaError

#: The primitive types a config field may declare.
FieldType = Literal["str", "int", "float", "bool"]

_FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_PY_TYPE: dict[FieldType, type] = {"str": str, "int": int, "float": float, "bool": bool}
#: The placeholder substituted for a secret value when redacting.
REDACTED = "***"


@dataclass(frozen=True, slots=True)
class ConfigField:
    """One declared configuration field."""

    name: str
    type: FieldType
    required: bool = False
    default: Any = None
    secret: bool = False
    description: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConfigField:
        if not isinstance(data, dict):
            raise ConfigSchemaError("config field must be an object")
        name = data.get("name")
        if not isinstance(name, str) or not _FIELD_NAME_RE.match(name):
            raise ConfigSchemaError(f"invalid config field name: {name!r}", field=str(name))
        ftype = data.get("type")
        if ftype not in _PY_TYPE:
            raise ConfigSchemaError(
                f"config field {name!r} has unknown type {ftype!r} "
                f"(expected one of {sorted(_PY_TYPE)})",
                field=name,
            )
        required = bool(data.get("required", False))
        default = data.get("default")
        if required and default is not None:
            raise ConfigSchemaError(
                f"config field {name!r} cannot be both required and have a default",
                field=name,
            )
        if default is not None and not _type_ok(default, ftype):
            raise ConfigSchemaError(
                f"config field {name!r} default {default!r} is not of type {ftype}",
                field=name,
            )
        return cls(
            name=name,
            type=ftype,
            required=required,
            default=default,
            secret=bool(data.get("secret", False)),
            description=str(data.get("description", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "type": self.type, "required": self.required}
        if self.default is not None:
            out["default"] = self.default
        if self.secret:
            out["secret"] = True
        if self.description:
            out["description"] = self.description
        return out


@dataclass(frozen=True, slots=True)
class ConfigSchema:
    """An ordered set of :class:`ConfigField` with validation + redaction."""

    fields: tuple[ConfigField, ...] = ()

    @classmethod
    def from_iterable(cls, raw: Any) -> ConfigSchema:
        """Build a schema from a list of field dicts (rejects duplicate names)."""
        if raw is None:
            return cls(())
        if not isinstance(raw, (list, tuple)):
            raise ConfigSchemaError("config_schema must be a list of field objects")
        fields = tuple(ConfigField.from_dict(f) for f in raw)
        seen: set[str] = set()
        for f in fields:
            if f.name in seen:
                raise ConfigSchemaError(f"duplicate config field: {f.name!r}", field=f.name)
            seen.add(f.name)
        return cls(fields)

    @property
    def by_name(self) -> dict[str, ConfigField]:
        return {f.name: f for f in self.fields}

    def validate(self, payload: dict[str, Any] | None) -> dict[str, Any]:
        """Validate ``payload``, returning a normalized config dict.

        Unknown keys are rejected (a typo'd field name is a config bug, not a
        silently-ignored extra). Missing required fields and type mismatches
        raise :class:`ConfigSchemaError`. Optional fields fall back to their
        declared default (or are omitted when no default is declared).
        """
        data = payload or {}
        if not isinstance(data, dict):
            raise ConfigSchemaError("plugin config must be an object")
        known = self.by_name
        for key in data:
            if key not in known:
                raise ConfigSchemaError(f"unknown config field: {key!r}", field=str(key))
        resolved: dict[str, Any] = {}
        for field in self.fields:
            if field.name in data:
                value = data[field.name]
                if not _type_ok(value, field.type):
                    raise ConfigSchemaError(
                        f"config field {field.name!r} expected {field.type}, "
                        f"got {type(value).__name__}",
                        field=field.name,
                    )
                resolved[field.name] = value
            elif field.required:
                raise ConfigSchemaError(
                    f"missing required config field: {field.name!r}", field=field.name
                )
            elif field.default is not None:
                resolved[field.name] = field.default
        return resolved

    def redact(self, config: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of ``config`` with secret-flagged fields masked.

        Use this whenever a plugin's config is logged — it keeps non-secret
        config visible for debugging while never emitting a credential.
        """
        secrets = {f.name for f in self.fields if f.secret}
        return {k: (REDACTED if k in secrets else v) for k, v in config.items()}

    @property
    def secret_fields(self) -> frozenset[str]:
        return frozenset(f.name for f in self.fields if f.secret)

    def to_list(self) -> list[dict[str, Any]]:
        return [f.to_dict() for f in self.fields]


def _type_ok(value: Any, ftype: FieldType) -> bool:
    """True when ``value`` matches ``ftype`` (bool is *not* an int here)."""
    if ftype == "bool":
        return isinstance(value, bool)
    if ftype == "int":
        # bool is a subclass of int — exclude it so a stray True is a type error.
        return isinstance(value, int) and not isinstance(value, bool)
    if ftype == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, str)


__all__ = ["REDACTED", "ConfigField", "ConfigSchema", "FieldType"]
