"""Raw-body JSON decoding for the ingress (strict, no surprises).

Split out from the parsers so the "is this even JSON?" failure mode is one place
and maps cleanly to a 422. We never use ``eval``-y or lenient decoders; a body
that is not valid JSON, or decodes to something other than an object, is a
:class:`MalformedPayloadError`.
"""

from __future__ import annotations

import json
from typing import Any

from app.video.webhooks.errors import MalformedPayloadError


def decode_json(body: bytes) -> dict[str, Any]:
    """Decode a raw callback body into a JSON object or raise (422).

    The signature is verified over the *raw bytes* before this runs, so decoding
    here cannot be used to smuggle past authentication.
    """
    if not body:
        raise MalformedPayloadError("empty callback body")
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MalformedPayloadError(f"callback body is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise MalformedPayloadError("callback body must be a JSON object")
    return parsed


__all__ = ["decode_json"]
