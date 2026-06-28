#!/usr/bin/env node
/**
 * Emit `clients/spec/openapi.json` from the single source-of-truth catalog.
 *
 * Pure Node, zero dependencies. Run with:
 *   node clients/spec/generate.mjs           # writes openapi.json
 *   node clients/spec/generate.mjs --check    # exits 1 if the file is stale
 *
 * The OpenAPI document is deliberately conservative: every documented endpoint
 * becomes a path item, every MODELS entry becomes a `#/components/schemas` entry,
 * the error envelope is shared, and bearer security is applied where required.
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
  WEBSOCKET,
  MODELS,
  fullPath,
} from "./catalog.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = join(HERE, "openapi.json");

/** Convert a MODELS field type string into a JSON-Schema fragment. */
function fieldSchema(type) {
  let t = type;
  let nullable = false;
  if (t.endsWith("?")) {
    nullable = true;
    t = t.slice(0, -1);
  }
  let schema;
  if (t.endsWith("[]")) {
    schema = { type: "array", items: fieldSchema(t.slice(0, -2)) };
  } else if (t === "string") {
    schema = { type: "string" };
  } else if (t === "integer") {
    schema = { type: "integer" };
  } else if (t === "number") {
    schema = { type: "number" };
  } else if (t === "boolean") {
    schema = { type: "boolean" };
  } else if (t === "object") {
    schema = { type: "object", additionalProperties: true };
  } else {
    // A reference to another model.
    schema = { $ref: `#/components/schemas/${t}` };
  }
  if (nullable) {
    // OpenAPI 3.1 uses a type union for nullable.
    if (schema.$ref) return { anyOf: [schema, { type: "null" }] };
    return { ...schema, nullable: true };
  }
  return schema;
}

function buildSchemas() {
  const schemas = {};
  for (const [name, fields] of Object.entries(MODELS)) {
    const properties = {};
    const required = [];
    for (const [field, type] of Object.entries(fields)) {
      properties[field] = fieldSchema(type);
      if (!type.endsWith("?")) required.push(field);
    }
    schemas[name] = {
      type: "object",
      properties,
      ...(required.length ? { required } : {}),
    };
  }
  return schemas;
}

/** Path-template params -> OpenAPI parameter objects. */
function pathParams(path) {
  const out = [];
  for (const m of path.matchAll(/\{([^}]+)\}/g)) {
    out.push({
      name: m[1],
      in: "path",
      required: true,
      schema: { type: m[1].endsWith("_number") ? "integer" : "string" },
    });
  }
  return out;
}

function queryParams(query) {
  if (!query) return [];
  return Object.entries(query).map(([name, description]) => ({
    name,
    in: "query",
    required: false,
    description,
    schema: { type: name === "duration_s" || name === "velocity" ? "number" : "string" },
  }));
}

function responseSchema(responseModel) {
  if (!responseModel) return undefined;
  if (responseModel === "text/event-stream") return undefined;
  if (responseModel.endsWith("[]")) {
    return { type: "array", items: { $ref: `#/components/schemas/${responseModel.slice(0, -2)}` } };
  }
  return { $ref: `#/components/schemas/${responseModel}` };
}

function buildPaths() {
  const paths = {};
  for (const e of ENDPOINTS) {
    const p = fullPath(e);
    paths[p] = paths[p] ?? {};
    const op = {
      operationId: e.id,
      tags: [e.tag],
      summary: e.summary,
      parameters: [...pathParams(e.path), ...queryParams(e.query)],
      responses: {
        [String(e.status)]: e.responseModel
          ? e.responseModel === "text/event-stream"
            ? {
                description: e.summary,
                content: { "text/event-stream": { schema: { type: "string" } } },
              }
            : {
                description: e.summary,
                content: { "application/json": { schema: responseSchema(e.responseModel) } },
              }
          : { description: e.summary },
        default: {
          description: "Typed error envelope.",
          content: { "application/json": { schema: { $ref: "#/components/schemas/ErrorResponse" } } },
        },
      },
    };
    if (e.auth) op.security = [{ bearerAuth: [] }];
    if (e.requestModel === "multipart") {
      op.requestBody = {
        required: true,
        content: {
          "multipart/form-data": {
            schema: {
              type: "object",
              properties: {
                file: { type: "string", format: "binary" },
                title: { type: "string" },
                author: { type: "string" },
                art_direction: { type: "string" },
              },
              required: ["file"],
            },
          },
        },
      };
    } else if (e.requestModel) {
      op.requestBody = {
        required: true,
        content: {
          "application/json": { schema: { $ref: `#/components/schemas/${e.requestModel}` } },
        },
      };
    }
    if (e.parameters?.length === 0) delete op.parameters;
    if (!op.parameters?.length) delete op.parameters;
    paths[p][e.method.toLowerCase()] = op;
  }
  return paths;
}

function buildDoc() {
  return {
    openapi: "3.1.0",
    info: {
      title: "Kinora API",
      version: API_VERSION,
      description:
        "Kinora turns a book/PDF into a page-synced film generated a few seconds " +
        "ahead of the reader. This document is generated from the single " +
        "source-of-truth catalog at clients/spec/catalog.mjs. Do not edit by hand.",
      license: { name: "Apache-2.0" },
    },
    servers: [{ url: DEFAULT_BASE_URL, description: "Local development backend" }],
    tags: [...new Set(ENDPOINTS.map((e) => e.tag))].map((t) => ({ name: t })),
    paths: buildPaths(),
    components: {
      securitySchemes: {
        bearerAuth: { type: "http", scheme: "bearer", bearerFormat: "JWT" },
      },
      schemas: buildSchemas(),
    },
    "x-kinora-prefix": API_PREFIX,
    "x-kinora-events": EVENTS,
    "x-kinora-websocket": WEBSOCKET,
    "x-kinora-error-types": ERROR_TYPES,
  };
}

function main() {
  const doc = buildDoc();
  const json = JSON.stringify(doc, null, 2) + "\n";
  const check = process.argv.includes("--check");
  if (check) {
    let existing = "";
    try {
      existing = readFileSync(OUT, "utf8");
    } catch {
      /* not generated yet */
    }
    if (existing !== json) {
      console.error("openapi.json is stale — run `node clients/spec/generate.mjs`");
      process.exit(1);
    }
    console.log("openapi.json is up to date.");
    return;
  }
  writeFileSync(OUT, json);
  console.log(
    `Wrote ${OUT} — ${ENDPOINTS.length} endpoints, ${Object.keys(MODELS).length} schemas, ` +
      `${EVENTS.length} events.`,
  );
}

main();
