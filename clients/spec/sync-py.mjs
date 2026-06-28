#!/usr/bin/env node
/**
 * Generate `clients/python/src/kinora/spec.py` from the source-of-truth catalog.
 *
 * Keeps the Python SDK self-contained (it imports `kinora.spec`) while the
 * catalog stays the ONE place the surface is declared. Run with
 * `node clients/spec/sync-py.mjs` (or `--check`). Pure Node, zero deps.
 */
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { readFileSync, writeFileSync } from "node:fs";
import {
  API_VERSION,
  API_PREFIX,
  DEFAULT_BASE_URL,
  ENDPOINTS,
  EVENTS,
  ERROR_TYPES,
  CONFLICT_OPTIONS,
  WEBSOCKET,
} from "./catalog.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = join(HERE, "..", "python", "src", "kinora", "spec.py");

/** Emit a Python literal for a JSON-compatible value. */
function py(value, indent = 0) {
  const pad = "    ".repeat(indent);
  const padInner = "    ".repeat(indent + 1);
  if (value === null) return "None";
  if (typeof value === "boolean") return value ? "True" : "False";
  if (typeof value === "number") return String(value);
  if (typeof value === "string") return JSON.stringify(value);
  if (Array.isArray(value)) {
    if (value.length === 0) return "[]";
    const items = value.map((v) => `${padInner}${py(v, indent + 1)}`).join(",\n");
    return `[\n${items},\n${pad}]`;
  }
  // object
  const entries = Object.entries(value);
  if (entries.length === 0) return "{}";
  const lines = entries
    .map(([k, v]) => `${padInner}${JSON.stringify(k)}: ${py(v, indent + 1)}`)
    .join(",\n");
  return `{\n${lines},\n${pad}}`;
}

function render() {
  return `"""GENERATED FILE — do not edit by hand.

Emitted from clients/spec/catalog.mjs by \`node clients/spec/sync-py.mjs\`. The
catalog is the single source of truth for the Kinora API surface; this module
gives the Python SDK + the contract-drift test a typed view of it.
"""

from __future__ import annotations

from typing import Any, TypedDict


class EndpointSpec(TypedDict, total=False):
    id: str
    method: str
    path: str
    tag: str
    auth: bool
    summary: str
    requestModel: str | None
    responseModel: str | None
    status: int
    query: dict[str, str]


class EventSpec(TypedDict):
    name: str
    summary: str
    fields: list[str]
    channels: list[str]


class ErrorTypeSpec(TypedDict):
    type: str
    status: int
    summary: str


API_VERSION: str = ${py(API_VERSION)}
API_PREFIX: str = ${py(API_PREFIX)}
DEFAULT_BASE_URL: str = ${py(DEFAULT_BASE_URL)}

ENDPOINTS: list[EndpointSpec] = ${py(ENDPOINTS)}

EVENTS: list[EventSpec] = ${py(EVENTS)}

ERROR_TYPES: list[ErrorTypeSpec] = ${py(ERROR_TYPES)}

CONFLICT_OPTIONS: list[str] = ${py(CONFLICT_OPTIONS)}

WEBSOCKET: dict[str, Any] = ${py(WEBSOCKET)}


def full_path(endpoint: EndpointSpec) -> str:
    """Build the full path for an endpoint (prepends API_PREFIX)."""
    return f"{API_PREFIX}{endpoint['path']}"


def endpoints_by_tag() -> dict[str, list[EndpointSpec]]:
    """Endpoints grouped by tag, preserving declaration order."""
    out: dict[str, list[EndpointSpec]] = {}
    for endpoint in ENDPOINTS:
        out.setdefault(endpoint["tag"], []).append(endpoint)
    return out


__all__ = [
    "API_VERSION",
    "API_PREFIX",
    "DEFAULT_BASE_URL",
    "ENDPOINTS",
    "EVENTS",
    "ERROR_TYPES",
    "CONFLICT_OPTIONS",
    "WEBSOCKET",
    "EndpointSpec",
    "EventSpec",
    "ErrorTypeSpec",
    "full_path",
    "endpoints_by_tag",
]
`;
}

function main() {
  const text = render();
  if (process.argv.includes("--check")) {
    let existing = "";
    try {
      existing = readFileSync(OUT, "utf8");
    } catch {
      /* not generated yet */
    }
    if (existing !== text) {
      console.error("clients/python/src/kinora/spec.py is stale — run `node clients/spec/sync-py.mjs`");
      process.exit(1);
    }
    console.log("spec.py is up to date.");
    return;
  }
  writeFileSync(OUT, text);
  console.log(`Wrote ${OUT}`);
}

main();
