"""TypeScript **typed-client generator** from the enriched OpenAPI (pure strings).

The renderer's single API surface is the hand-written
``apps/desktop/src/lib/api.ts`` (a Bearer-token ``req`` helper + a flat ``api``
object of method stubs). This module emits a *generated* typed client that
mirrors that shape — interface declarations for the component schemas plus one
method per operation — so the renderer and backend can be checked for drift and,
optionally, the renderer can adopt generated types instead of hand-maintaining
them.

It is **pure string generation** (no `openapi-typescript`, no codegen jar, no
network): it walks the enriched spec and concatenates TypeScript. That keeps the
output deterministic and snapshot-testable, and means it runs anywhere the
backend test suite runs.

The generated module deliberately re-uses the renderer's existing transport
primitives (it imports ``{ http }`` and ``ApiError`` from the hand-written
client) rather than re-implementing fetch/auth, so the two never disagree about
how a request is sent — only about *what* the typed surface is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_HTTP_METHODS = ("get", "put", "post", "delete", "patch")
#: TypeScript reserved-ish identifiers we must not emit as a bare method name.
_TS_KEYWORDS = frozenset({"delete", "new", "default", "return", "function", "class"})


@dataclass(frozen=True)
class GeneratedClient:
    """The output of the generator: TS source + the operation surface it covers."""

    source: str
    operation_ids: tuple[str, ...]
    method_names: tuple[str, ...]
    paths: tuple[str, ...]


# --------------------------------------------------------------------------- #
# Schema → TypeScript type mapping
# --------------------------------------------------------------------------- #


def _ref_name(ref: str) -> str | None:
    if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
        return ref.rsplit("/", 1)[-1]
    return None


def ts_type(schema: Any) -> str:
    """Map a (possibly composite) OpenAPI schema to a TypeScript type expression.

    Conservative + total: anything it cannot map collapses to ``unknown`` rather
    than producing invalid TS, so the output always type-checks in shape.
    """
    if not isinstance(schema, dict):
        return "unknown"
    ref = schema.get("$ref")
    if ref:
        name = _ref_name(ref)
        return name or "unknown"
    for key in ("anyOf", "oneOf"):
        if key in schema:
            parts = [ts_type(s) for s in schema[key]]
            uniq: list[str] = []
            for p in parts:
                if p not in uniq:
                    uniq.append(p)
            return " | ".join(uniq) if uniq else "unknown"
    if "allOf" in schema:
        parts = [ts_type(s) for s in schema["allOf"]]
        return " & ".join(p for p in parts if p != "unknown") or "unknown"
    t = schema.get("type")
    if t == "array":
        inner = ts_type(schema.get("items", {})) or "unknown"
        return f"{inner}[]"
    if t == "object" or ("properties" in schema):
        props = schema.get("properties")
        if not props:
            return "Record<string, unknown>"
        fields = []
        required = set(schema.get("required", []) or [])
        for name, sub in props.items():
            opt = "" if name in required else "?"
            fields.append(f"{_safe_prop(name)}{opt}: {ts_type(sub)}")
        return "{ " + "; ".join(fields) + " }"
    scalar = {
        "string": "string",
        "integer": "number",
        "number": "number",
        "boolean": "boolean",
        "null": "null",
    }
    if isinstance(t, list):
        return " | ".join(scalar.get(x, "unknown") for x in t)
    return scalar.get(t, "unknown") if isinstance(t, str) else "unknown"


def _safe_prop(name: str) -> str:
    """Quote a property key that isn't a bare TS identifier."""
    if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", name):
        return name
    return json_quote(name)


def json_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# --------------------------------------------------------------------------- #
# Interfaces from components/schemas
# --------------------------------------------------------------------------- #


def _emit_interfaces(spec: dict[str, Any]) -> list[str]:
    schemas: dict[str, Any] = (spec.get("components") or {}).get("schemas", {})
    out: list[str] = []
    for name in sorted(schemas):
        schema = schemas[name]
        if not isinstance(schema, dict):
            continue
        # Enums become a string-literal union alias.
        if "enum" in schema and schema.get("type") in {"string", None}:
            members = " | ".join(json_quote(str(v)) for v in schema["enum"])
            out.append(f"export type {name} = {members or 'string'};")
            continue
        props = schema.get("properties")
        if props is None:
            out.append(f"export type {name} = {ts_type(schema)};")
            continue
        required = set(schema.get("required", []) or [])
        lines = [f"export interface {name} {{"]
        for prop, sub in props.items():
            opt = "" if prop in required else "?"
            desc = sub.get("description") if isinstance(sub, dict) else None
            if desc:
                lines.append(f"  /** {_one_line(desc)} */")
            lines.append(f"  {_safe_prop(prop)}{opt}: {ts_type(sub)};")
        lines.append("}")
        out.append("\n".join(lines))
    return out


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().replace("*/", "* /")


# --------------------------------------------------------------------------- #
# Method stubs from paths
# --------------------------------------------------------------------------- #


def _path_params(path: str) -> list[str]:
    return re.findall(r"\{(.+?)\}", path)


def _param_ident(name: str) -> str:
    """A valid TS identifier for a path-param name (underscores preserved).

    ``book_id`` → ``book_id``; a leading digit or stray punctuation is sanitised
    so the result is always a usable variable name and matches between the
    function signature and the path template literal.
    """
    ident = re.sub(r"[^A-Za-z0-9_$]", "", name)
    if not ident or ident[0].isdigit():
        ident = "p_" + ident
    return ident


def _method_name(operation_id: str) -> str:
    name = operation_id or "operation"
    name = re.sub(r"[^A-Za-z0-9]", "", name)
    if not name:
        name = "operation"
    name = name[:1].lower() + name[1:]
    if name in _TS_KEYWORDS:
        name = name + "_"
    return name


def _success_schema(op: dict[str, Any]) -> Any:
    responses = op.get("responses") or {}
    for code in sorted(responses):
        if str(code).startswith("2"):
            content = (responses[code] or {}).get("content") or {}
            media = content.get("application/json") or {}
            if media.get("schema") is not None:
                return media["schema"]
            return None
    return None


def _request_schema(op: dict[str, Any]) -> Any:
    body = op.get("requestBody")
    if not isinstance(body, dict):
        return None
    content = body.get("content") or {}
    media = content.get("application/json") or {}
    return media.get("schema")


def _emit_method(path: str, method: str, op: dict[str, Any]) -> tuple[str, str]:
    """Return (method-name, TS source) for one operation stub."""
    op_id = op.get("operationId") or f"{method}{path}"
    fn = _method_name(op_id)
    params = _path_params(path)
    args: list[str] = [f"{_param_ident(p)}: string" for p in params]
    body_schema = _request_schema(op)
    has_body = body_schema is not None
    if has_body:
        args.append(f"body: {ts_type(body_schema)}")
    ret = _success_schema(op)
    ret_ts = ts_type(ret) if ret is not None else "void"
    # Build the JS template literal for the path.
    ts_path = "/" + path.lstrip("/")
    ts_path = re.sub(r"\{(.+?)\}", lambda m: "${" + _param_ident(m.group(1)) + "}", ts_path)
    summary = _one_line(op.get("summary") or "")
    init_parts = [f'method: "{method.upper()}"']
    if has_body:
        init_parts.append("body: JSON.stringify(body)")
    init = "{ " + ", ".join(init_parts) + " }"
    doc = f"  /** {summary} */\n" if summary else ""
    src = (
        f"{doc}  {fn}({', '.join(args)}): Promise<{ret_ts}> {{\n"
        f"    return http<{ret_ts}>(`{ts_path}`, {init});\n"
        f"  }},"
    )
    return fn, src


def generate_client(spec: dict[str, Any], *, module_name: str = "kinora") -> GeneratedClient:
    """Generate the TypeScript typed client module from an enriched spec.

    Pure string generation. The result imports the renderer's transport
    primitives (``http``/``ApiError``) so it never re-implements fetch or auth.
    """
    header = (
        "// AUTO-GENERATED from the Kinora OpenAPI by app.apispec.tsclient.\n"
        "// Do not edit by hand. Regenerate with the backend spec tooling.\n"
        "// Re-uses the hand-written transport in ../api so request/auth/error\n"
        "// behaviour is shared with the existing renderer client.\n"
        "/* eslint-disable */\n"
        'import { http, ApiError } from "../api";\n'
        "export { ApiError };\n"
    )
    interfaces = _emit_interfaces(spec)

    methods: list[str] = []
    method_names: list[str] = []
    op_ids: list[str] = []
    paths: list[str] = []
    for path in sorted((spec.get("paths") or {}).keys()):
        item = spec["paths"][path]
        if not isinstance(item, dict):
            continue
        for method in _HTTP_METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            fn, src = _emit_method(path, method, op)
            methods.append(src)
            method_names.append(fn)
            op_ids.append(op.get("operationId") or fn)
            paths.append(f"{method.upper()} {path}")

    client_block = f"export const {module_name}Client = {{\n" + "\n".join(methods) + "\n};\n"
    types_block = "\n\n".join(interfaces)
    source = "\n".join([header, "", types_block, "", client_block])
    return GeneratedClient(
        source=source,
        operation_ids=tuple(op_ids),
        method_names=tuple(method_names),
        paths=tuple(paths),
    )


#: The concrete REST routes the hand-written renderer client
#: (``apps/desktop/src/lib/api.ts``) calls. The generated surface MUST cover
#: every one of these or the renderer would have no typed counterpart — the
#: coverage check (:func:`renderer_coverage`) enforces that.
RENDERER_ROUTES: tuple[tuple[str, str], ...] = (
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/register"),
    ("GET", "/api/books"),
    ("POST", "/api/books"),
    ("GET", "/api/books/{book_id}"),
    ("GET", "/api/books/{book_id}/shots"),
    ("GET", "/api/books/{book_id}/pages/{page_number}"),
    ("POST", "/api/sessions"),
    ("POST", "/api/sessions/{session_id}/intent"),
    ("POST", "/api/sessions/{session_id}/seek"),
)


def renderer_coverage(generated: GeneratedClient) -> list[tuple[str, str]]:
    """Return the :data:`RENDERER_ROUTES` *missing* from the generated surface.

    An empty list means the generated client covers every route the renderer
    actually uses — the drift guarantee. Routes are matched on method + path,
    independent of the path-parameter *names*, since the generator and the spec
    may differ on a ``{id}`` vs ``{book_id}`` label.
    """
    covered = {
        (_norm(m), _norm_path(p.split(" ", 1)[1]))
        for p in generated.paths
        if (m := p.split(" ", 1)[0])
    }
    missing: list[tuple[str, str]] = []
    for method, path in RENDERER_ROUTES:
        if (_norm(method), _norm_path(path)) not in covered:
            missing.append((method, path))
    return missing


def _norm(method: str) -> str:
    return method.strip().upper()


def _norm_path(path: str) -> str:
    """Normalise a path so param *names* don't matter (``{x}`` → ``{}``)."""
    return re.sub(r"\{.+?\}", "{}", path.strip())


__all__ = [
    "GeneratedClient",
    "RENDERER_ROUTES",
    "generate_client",
    "renderer_coverage",
    "ts_type",
]
