"""Unit tests for the MCP request/response JSON-Schema validation (§8.3).

Validation is the shape gate around the single execution path: a malformed call
fails locally with a typed :class:`InvalidParamsError` carrying field paths, and
a handler whose result drifts from its declared output schema fails with a typed
:class:`InvalidResponseError`. No infrastructure required.
"""

from __future__ import annotations

from app.mcp.errors import ErrorCategory, InvalidParamsError, InvalidResponseError
from app.mcp.registry import default_catalog
from app.mcp.validation import SchemaValidator


def _validator() -> SchemaValidator:
    return SchemaValidator(default_catalog())


# --- request validation ------------------------------------------------------


def test_valid_request_returns_parsed_model() -> None:
    v = _validator()
    model = v.validate_request("canon.query", {"book_id": "b1", "beat_id": "beat_1"})
    assert model.book_id == "b1"  # type: ignore[attr-defined]


def test_invalid_request_reports_field_path() -> None:
    v = _validator()
    try:
        v.validate_request("canon.query", {"beat_id": "beat_1"})  # missing book_id
    except InvalidParamsError as exc:
        assert exc.category is ErrorCategory.INVALID_PARAMS
        assert exc.data is not None
        paths = [i["path"] for i in exc.data["issues"]]
        assert ["book_id"] in paths
    else:  # pragma: no cover
        raise AssertionError("expected InvalidParamsError")


def test_unknown_tool_request_is_invalid_params() -> None:
    v = _validator()
    try:
        v.validate_request("nope.nope", {})
    except InvalidParamsError as exc:
        assert exc.data is not None
        assert exc.data["tool"] == "nope.nope"
    else:  # pragma: no cover
        raise AssertionError("expected InvalidParamsError")


def test_wrong_type_request_is_rejected() -> None:
    v = _validator()
    try:
        # episodic_k must be an int.
        v.validate_request(
            "canon.query", {"book_id": "b", "beat_id": "x", "episodic_k": "lots"}
        )
    except InvalidParamsError as exc:
        assert exc.data is not None
        assert exc.data["error_count"] >= 1
    else:  # pragma: no cover
        raise AssertionError("expected InvalidParamsError")


# --- response validation -----------------------------------------------------


def test_valid_response_passes() -> None:
    v = _validator()
    ok = {
        "status": "enqueued",
        "cached": False,
        "shot_hash": "h",
        "reference_set_hash": "r",
        "video_seconds": 5.0,
    }
    v.validate_response("shot.render", ok)  # no raise
    assert v.is_valid_response("shot.render", ok)


def test_invalid_response_is_rejected_with_path() -> None:
    v = _validator()
    try:
        v.validate_response("shot.render", {"cached": False})  # missing required fields
    except InvalidResponseError as exc:
        assert exc.category is ErrorCategory.INVALID_RESPONSE
        assert exc.data is not None
        assert exc.data["tool"] == "shot.render"
        assert exc.data["error_count"] >= 1
    else:  # pragma: no cover
        raise AssertionError("expected InvalidResponseError")


def test_response_validation_handles_nested_defs_schema() -> None:
    v = _validator()
    # canon.facts_as_of output references BitemporalFact via $defs.
    assert v.is_valid_response("canon.facts_as_of", {"facts": []})


def test_unknown_tool_response_is_skipped() -> None:
    v = _validator()
    # No schema to validate against -> treated as valid (the server reports the
    # method-not-found upstream).
    assert v.is_valid_response("nope.nope", {"whatever": 1})
