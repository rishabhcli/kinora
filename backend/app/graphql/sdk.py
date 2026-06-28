"""Generate a typed TypeScript client SDK from the schema.

A public API is only as usable as its client. :func:`generate_typescript_sdk`
walks the :class:`~app.graphql.schema.Schema` and emits a single ``.ts`` module:

* TypeScript types for every object / input / enum (scalars mapped to TS
  primitives, ``JSON``→``unknown``, lists/non-null to ``T[]`` / ``T``);
* a tiny ``KinoraGraphQLClient`` class wrapping ``fetch`` with the ``X-API-Key``
  header, persisted-query (sha256) support, and a typed ``request`` method.

The emitter is deterministic (types sorted by name) so the generated SDK is
stable to diff, and it is served from ``GET /graphql/sdk`` alongside the SDL
export. It needs no codegen dependency — it is plain string assembly.
"""

from __future__ import annotations

from app.graphql.schema import Schema
from app.graphql.type_system import (
    GraphQLEnum,
    GraphQLInputObject,
    GraphQLInterface,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObject,
    GraphQLScalar,
    GraphQLType,
    GraphQLUnion,
)
from app.graphql.versioning import API_VERSION

_SCALAR_TS = {
    "Int": "number",
    "Float": "number",
    "String": "string",
    "Boolean": "boolean",
    "ID": "string",
    "DateTime": "string",
    "Cursor": "string",
    "JSON": "unknown",
}


def _ts_type(t: GraphQLType, *, nullable: bool = True) -> str:
    if isinstance(t, GraphQLNonNull):
        return _ts_type(t.of_type, nullable=False)
    if isinstance(t, GraphQLList):
        inner = _ts_type(t.of_type, nullable=True)
        base = f"Array<{inner}>"
        return base if not nullable else f"{base} | null"
    name = t.unwrap().name
    base = _SCALAR_TS.get(name, "unknown") if isinstance(t, GraphQLScalar) else name
    return base if not nullable else f"{base} | null"


def _emit_object(t: GraphQLObject | GraphQLInterface) -> str:
    lines = [f"export interface {t.name} {{"]
    for name, fdef in t.fields.items():
        if name.startswith("__"):
            continue
        optional = "" if isinstance(fdef.type, GraphQLNonNull) else "?"
        doc = f"  /** {fdef.description} */\n" if fdef.description else ""
        lines.append(f"{doc}  {name}{optional}: {_ts_type(fdef.type)};")
    lines.append("}")
    return "\n".join(lines)


def _emit_input(t: GraphQLInputObject) -> str:
    lines = [f"export interface {t.name} {{"]
    for name, fdef in t.fields.items():
        optional = "" if isinstance(fdef.type, GraphQLNonNull) else "?"
        lines.append(f"  {name}{optional}: {_ts_type(fdef.type)};")
    lines.append("}")
    return "\n".join(lines)


def _emit_enum(t: GraphQLEnum) -> str:
    members = " | ".join(f'"{v.name}"' for v in t.values)
    return f"export type {t.name} = {members};"


def _emit_union(t: GraphQLUnion) -> str:
    members = " | ".join(m.name for m in t.types)
    return f"export type {t.name} = {members};"


_CLIENT = '''
export interface GraphQLError {
  message: string;
  path?: Array<string | number>;
  extensions?: Record<string, unknown>;
}

export interface GraphQLResponse<T> {
  data?: T;
  errors?: GraphQLError[];
}

export interface KinoraClientOptions {
  endpoint: string;
  apiKey: string;
  fetchImpl?: typeof fetch;
}

/** A minimal, dependency-free client for the Kinora public GraphQL API. */
export class KinoraGraphQLClient {
  private readonly endpoint: string;
  private readonly apiKey: string;
  private readonly fetchImpl: typeof fetch;

  constructor(opts: KinoraClientOptions) {
    this.endpoint = opts.endpoint;
    this.apiKey = opts.apiKey;
    this.fetchImpl = opts.fetchImpl ?? fetch;
  }

  /** Execute an operation. Pass `operationName` when the document has several. */
  async request<TData = unknown, TVars = Record<string, unknown>>(
    query: string,
    variables?: TVars,
    operationName?: string,
  ): Promise<GraphQLResponse<TData>> {
    const res = await this.fetchImpl(this.endpoint, {
      method: "POST",
      headers: { "content-type": "application/json", "x-api-key": this.apiKey },
      body: JSON.stringify({ query, variables, operationName }),
    });
    return (await res.json()) as GraphQLResponse<TData>;
  }

  /** Execute a registered persisted query by its sha256 id. */
  async requestPersisted<TData = unknown, TVars = Record<string, unknown>>(
    sha256Hash: string,
    variables?: TVars,
    operationName?: string,
  ): Promise<GraphQLResponse<TData>> {
    const res = await this.fetchImpl(this.endpoint, {
      method: "POST",
      headers: { "content-type": "application/json", "x-api-key": this.apiKey },
      body: JSON.stringify({ id: sha256Hash, variables, operationName }),
    });
    return (await res.json()) as GraphQLResponse<TData>;
  }
}
'''


def generate_typescript_sdk(schema: Schema) -> str:
    """Emit the full TypeScript SDK module for ``schema``."""
    header = (
        "/* eslint-disable */\n"
        "// AUTO-GENERATED Kinora public GraphQL client SDK.\n"
        f"// API version {API_VERSION}. Do not edit by hand — regenerate from /graphql/sdk.\n"
    )
    blocks: list[str] = [header]
    for t in schema.named_types():
        if t.name.startswith("__") or t.name in _SCALAR_TS:
            continue
        if isinstance(t, GraphQLScalar):
            blocks.append(f"export type {t.name} = unknown;")
        elif isinstance(t, GraphQLEnum):
            blocks.append(_emit_enum(t))
        elif isinstance(t, GraphQLInputObject):
            blocks.append(_emit_input(t))
        elif isinstance(t, GraphQLUnion):
            blocks.append(_emit_union(t))
        elif isinstance(t, (GraphQLObject, GraphQLInterface)):
            blocks.append(_emit_object(t))
    blocks.append(_CLIENT.strip())
    return "\n\n".join(blocks) + "\n"


__all__ = ["generate_typescript_sdk"]
