"""A declarative request-template engine for the provider descriptor.

The descriptor (see :mod:`.descriptor`) describes a provider's *request body* as a
JSON object containing ``{{placeholders}}`` that are filled from a canonical
:class:`WanSpec`'s field bag. This module renders that template:

* ``"{{prompt}}"`` as an *entire* string value → substituted with the raw context
  value (preserving its type: a number stays a number, a list stays a list).
* ``"prefix {{seed}} suffix"`` → string interpolation (the value is stringified).
* A key whose rendered value is ``None`` is **omitted** from the output object,
  so optional fields (``negative_prompt``, ``seed``, conditioning URLs) simply
  vanish when absent rather than sending ``null``.
* A literal default may be supplied as ``"{{seed|0}}"`` — used when the context
  value is ``None``.
* Nested objects and lists are rendered recursively; a list element that renders
  to ``None`` is dropped (so ``[{{first_frame_url}}]`` becomes ``[]`` not
  ``[null]`` when there is no frame).

The engine is intentionally tiny and pure — no eval, no arbitrary expressions —
so a descriptor is safe to load from untrusted-ish config and is fully testable.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["build_context", "render_template", "MISSING"]

#: Matches a whole-string placeholder, optionally ``{{name|default}}``.
_WHOLE = re.compile(r"^\{\{\s*([\w.]+)\s*(?:\|([^}]*))?\}\}$")
#: Matches inline placeholders for interpolation within a larger string.
_INLINE = re.compile(r"\{\{\s*([\w.]+)\s*(?:\|([^}]*))?\}\}")

#: Sentinel for "omit this key/element".
MISSING = object()


def _lookup(context: dict[str, Any], name: str) -> Any:
    """Resolve a dotted ``name`` against ``context`` (``None`` when absent)."""
    node: Any = context
    for part in name.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def _render_value(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, str):
        whole = _WHOLE.match(value.strip())
        if whole:
            name, default = whole.group(1), whole.group(2)
            resolved = _lookup(context, name)
            if resolved is None:
                return _coerce_default(default) if default is not None else MISSING
            return resolved

        def _sub(m: re.Match[str]) -> str:
            resolved = _lookup(context, m.group(1))
            if resolved is None:
                resolved = m.group(2) if m.group(2) is not None else ""
            return str(resolved)

        return _INLINE.sub(_sub, value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            rendered = _render_value(v, context)
            if rendered is not MISSING:
                out[k] = rendered
        return out
    if isinstance(value, list):
        items = [_render_value(v, context) for v in value]
        return [it for it in items if it is not MISSING]
    return value


def _coerce_default(default: str) -> Any:
    """Best-effort literal coercion of a ``{{x|default}}`` default token."""
    text = default.strip()
    if text == "":
        return ""
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    try:
        if "." in text:
            return float(text)
        return int(text)
    except ValueError:
        return text


def render_template(template: Any, context: dict[str, Any]) -> Any:
    """Render a descriptor request template against a flat ``context`` bag.

    Top-level keys whose value renders to :data:`MISSING` are dropped, so optional
    fields cleanly disappear when their context value is ``None``.
    """
    rendered = _render_value(template, context)
    if rendered is MISSING:
        return {}
    return rendered


def build_context(spec: Any, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Flatten a :class:`~app.providers.types.WanSpec` into a template context.

    Exposes every field a descriptor might reference under a stable name, plus a
    couple of derived conveniences (``mode`` as its string value,
    ``reference_image_url`` = the first reference). ``extra`` overlays caller
    additions (e.g. a resolved native model id).
    """
    refs = list(getattr(spec, "reference_image_urls", []) or [])
    context: dict[str, Any] = {
        "prompt": getattr(spec, "prompt", "") or "",
        "negative_prompt": getattr(spec, "negative_prompt", None),
        "mode": getattr(getattr(spec, "mode", None), "value", None),
        "duration_s": getattr(spec, "duration_s", None),
        "resolution": getattr(spec, "resolution", None),
        "seed": getattr(spec, "seed", None),
        "watermark": getattr(spec, "watermark", None),
        "prompt_extend": getattr(spec, "prompt_extend", None),
        "image_url": getattr(spec, "image_url", None),
        "first_frame_url": getattr(spec, "first_frame_url", None),
        "last_frame_url": getattr(spec, "last_frame_url", None),
        "source_video_url": getattr(spec, "source_video_url", None),
        "reference_voice_url": getattr(spec, "reference_voice_url", None),
        "reference_image_urls": refs,
        "reference_image_url": refs[0] if refs else None,
        "model": getattr(spec, "model", None),
        "shot_id": getattr(spec, "shot_id", None),
    }
    if extra:
        context.update(extra)
    return context
