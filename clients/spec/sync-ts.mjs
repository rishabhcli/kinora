#!/usr/bin/env node
/**
 * Generate `clients/typescript/src/spec.ts` from the source-of-truth catalog.
 *
 * This keeps the TS SDK self-contained (it imports `./spec.js`, never reaching
 * outside its own `src/`) while the catalog stays the ONE place the surface is
 * declared. Run with `node clients/spec/sync-ts.mjs` (or `--check` to verify it
 * is in sync, used by the drift test).
 *
 * Pure Node, zero dependencies.
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
  MODELS,
} from "./catalog.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = join(HERE, "..", "typescript", "src", "spec.ts");

function lit(value) {
  return JSON.stringify(value, null, 2);
}

function render() {
  return `/**
 * GENERATED FILE — do not edit by hand.
 *
 * Emitted from clients/spec/catalog.mjs by \`node clients/spec/sync-ts.mjs\`.
 * The catalog is the single source of truth for the Kinora API surface; this
 * typed view keeps the TS SDK self-contained.
 */

export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

export interface EndpointSpec {
  readonly id: string;
  readonly method: HttpMethod;
  readonly path: string;
  readonly tag: string;
  readonly auth: boolean;
  readonly summary: string;
  readonly requestModel: string | null;
  readonly responseModel: string | null;
  readonly status: number;
  readonly query?: Readonly<Record<string, string>>;
}

export interface EventSpec {
  readonly name: string;
  readonly summary: string;
  readonly fields: readonly string[];
  readonly channels: readonly ("session" | "book" | "library")[];
}

export interface ErrorTypeSpec {
  readonly type: string;
  readonly status: number;
  readonly summary: string;
}

export const API_VERSION = ${lit(API_VERSION)};
export const API_PREFIX = ${lit(API_PREFIX)};
export const DEFAULT_BASE_URL = ${lit(DEFAULT_BASE_URL)};

export const ENDPOINTS: readonly EndpointSpec[] = ${lit(ENDPOINTS)} as const;

export const EVENTS: readonly EventSpec[] = ${lit(EVENTS)} as const;

export const ERROR_TYPES: readonly ErrorTypeSpec[] = ${lit(ERROR_TYPES)} as const;

export const CONFLICT_OPTIONS: readonly string[] = ${lit(CONFLICT_OPTIONS)} as const;

export const WEBSOCKET = ${lit(WEBSOCKET)} as const;

export const MODELS: Readonly<Record<string, Readonly<Record<string, string>>>> = ${lit(MODELS)};

/** Build the full path for an endpoint (prepends API_PREFIX). */
export function fullPath(e: EndpointSpec): string {
  return \`\${API_PREFIX}\${e.path}\`;
}

/** Endpoints grouped by tag, preserving declaration order. */
export function endpointsByTag(): Map<string, EndpointSpec[]> {
  const out = new Map<string, EndpointSpec[]>();
  for (const e of ENDPOINTS) {
    const list = out.get(e.tag) ?? [];
    list.push(e);
    out.set(e.tag, list);
  }
  return out;
}
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
      console.error("clients/typescript/src/spec.ts is stale — run `node clients/spec/sync-ts.mjs`");
      process.exit(1);
    }
    console.log("spec.ts is up to date.");
    return;
  }
  writeFileSync(OUT, text);
  console.log(`Wrote ${OUT}`);
}

main();
