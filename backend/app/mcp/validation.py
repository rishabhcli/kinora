"""JSON-Schema request / response validation for the MCP tool surface (§8.3).

``MemoryTools.dispatch`` already validates arguments by constructing the tool's
pydantic input model — a malformed call raises a pydantic ``ValidationError``
there. This module makes that contract *explicit at the protocol boundary* and
adds the symmetric **response** check the SDK does not do for us:

* :meth:`SchemaValidator.validate_request` runs the raw arguments against the
  tool's input model and re-raises any failure as a typed
  :class:`~app.mcp.errors.InvalidParamsError` carrying the offending field
  paths (so a client sees *which* field was wrong, not a wall of text).
* :meth:`SchemaValidator.validate_response` checks the handler's already-typed
  result against the tool's declared output JSON Schema. Because the result is a
  pydantic model the field-level shape is already guaranteed; this is a
  belt-and-suspenders gate that catches a *contract* drift (a handler whose
  return type silently diverges from what the catalog advertises) and is the
  hook the conformance suite asserts on. It is gated by a setting so the hot
  path can skip it in production once the contract is proven.

Validation never executes a tool and never spends anything — it is a pure
shape check around the single execution path.
"""

from __future__ import annotations

from typing import Any

import jsonschema
from jsonschema.exceptions import ValidationError as JSONSchemaValidationError
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from app.mcp.errors import InvalidParamsError, InvalidResponseError
from app.mcp.registry import ToolCatalog, ToolMeta


def _pydantic_error_data(exc: PydanticValidationError) -> dict[str, Any]:
    """Reduce a pydantic ``ValidationError`` to a compact, JSON-safe error list."""
    issues: list[dict[str, Any]] = []
    for err in exc.errors():
        issues.append(
            {
                "path": list(err.get("loc", ())),
                "type": err.get("type", "value_error"),
                "msg": err.get("msg", "invalid value"),
            }
        )
    return {"issues": issues, "error_count": len(issues)}


class SchemaValidator:
    """Validate tool requests and responses against the catalog's JSON Schemas.

    Holds a cache of compiled JSON-Schema validators per tool output (built
    lazily) so repeated response checks do not recompile the schema. Request
    validation rides on pydantic (the tool's input model), which is both the
    advertised schema *and* the dispatch contract — so there is no chance the
    validation and the execution disagree.
    """

    def __init__(self, catalog: ToolCatalog) -> None:
        self._catalog = catalog
        self._output_validators: dict[str, jsonschema.protocols.Validator] = {}

    # --- request --------------------------------------------------------------

    def validate_request(self, name: str, arguments: dict[str, Any]) -> BaseModel:
        """Validate ``arguments`` for tool ``name`` and return the parsed input model.

        Raises:
            InvalidParamsError: arguments do not satisfy the tool's input schema.
        """
        meta = self._catalog.get(name)
        if meta is None:
            # Method existence is the server's concern; here we only fail params
            # if we *can* find the model. An unknown tool is reported upstream.
            raise InvalidParamsError(
                f"cannot validate arguments for unknown tool {name!r}",
                data={"tool": name},
            )
        return self._validate_against_model(meta, arguments)

    @staticmethod
    def _validate_against_model(meta: ToolMeta, arguments: dict[str, Any]) -> BaseModel:
        try:
            return meta.input_model.model_validate(arguments)
        except PydanticValidationError as exc:
            raise InvalidParamsError(
                f"invalid arguments for {meta.name!r}",
                data={"tool": meta.name, **_pydantic_error_data(exc)},
            ) from exc

    # --- response -------------------------------------------------------------

    def _validator_for(self, meta: ToolMeta) -> jsonschema.protocols.Validator | None:
        schema = meta.output_schema()
        if schema is None:
            return None
        cached = self._output_validators.get(meta.name)
        if cached is None:
            cls = jsonschema.validators.validator_for(schema)
            cls.check_schema(schema)
            cached = cls(schema)
            self._output_validators[meta.name] = cached
        return cached

    def validate_response(self, name: str, payload: dict[str, Any]) -> None:
        """Validate a tool's already-serialized result against its output schema.

        ``payload`` is the ``model_dump(mode="json")`` of the handler's result.
        A tool with no declared output model is skipped. A schema mismatch is a
        *server* contract bug, surfaced as :class:`InvalidResponseError`.

        Raises:
            InvalidResponseError: the payload does not satisfy the output schema.
        """
        meta = self._catalog.get(name)
        if meta is None:
            return
        validator = self._validator_for(meta)
        if validator is None:
            return
        errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
        if errors:
            first = errors[0]
            raise InvalidResponseError(
                f"response for {name!r} violated its output schema",
                data={
                    "tool": name,
                    "path": list(first.path),
                    "msg": first.message,
                    "error_count": len(errors),
                },
            )

    def is_valid_response(self, name: str, payload: dict[str, Any]) -> bool:
        """Non-raising response check (used by the conformance suite)."""
        try:
            self.validate_response(name, payload)
        except (InvalidResponseError, JSONSchemaValidationError):
            return False
        return True


__all__ = ["SchemaValidator"]
