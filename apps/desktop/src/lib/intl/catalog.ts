// Catalog operations — flatten/unflatten between the nested JSON shape the locale
// files ship in and the dotted-key flat shape the engine + tooling work with, plus
// deep-merge (for fallback layering) and key-set diffing (for the linter).

import { isMessageTree, type FlatCatalog, type MessageTree } from "./types.ts";

/**
 * Flatten a nested message tree to dotted keys.
 *   { nav: { home: "Home" } } → { "nav.home": "Home" }
 */
export function flatten(tree: MessageTree, prefix = ""): FlatCatalog {
  const out: FlatCatalog = {};
  for (const [key, value] of Object.entries(tree)) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (typeof value === "string") {
      out[path] = value;
    } else if (isMessageTree(value)) {
      Object.assign(out, flatten(value, path));
    }
  }
  return out;
}

/**
 * Unflatten dotted keys back to a nested tree.
 *   { "nav.home": "Home" } → { nav: { home: "Home" } }
 * A later key that conflicts with an existing leaf-vs-branch loses gracefully:
 * branches win (a string at a node that also has children becomes an object).
 */
export function unflatten(flat: FlatCatalog): MessageTree {
  const root: MessageTree = {};
  for (const [path, value] of Object.entries(flat)) {
    const parts = path.split(".");
    let node: MessageTree = root;
    for (let i = 0; i < parts.length - 1; i++) {
      const part = parts[i];
      const existing = node[part];
      if (!isMessageTree(existing)) {
        node[part] = {};
      }
      node = node[part] as MessageTree;
    }
    node[parts[parts.length - 1]] = value;
  }
  return root;
}

/**
 * Deep-merge `override` onto `base`, returning a new tree (neither input is
 * mutated). Used to layer a partial locale onto its fallback so the engine always
 * resolves *some* string. String-vs-object conflicts resolve to the override.
 */
export function deepMerge(base: MessageTree, override: MessageTree): MessageTree {
  const out: MessageTree = { ...base };
  for (const [key, value] of Object.entries(override)) {
    const existing = out[key];
    if (isMessageTree(existing) && isMessageTree(value)) {
      out[key] = deepMerge(existing, value);
    } else {
      out[key] = value;
    }
  }
  return out;
}

/** All dotted keys of a tree, sorted. */
export function keysOf(tree: MessageTree): string[] {
  return Object.keys(flatten(tree)).sort();
}

export interface CatalogDiff {
  /** Keys present in `reference` but absent from `subject`. */
  missing: string[];
  /** Keys present in `subject` but absent from `reference`. */
  extra: string[];
  /** Keys present in both. */
  common: string[];
}

/**
 * Diff a `subject` catalog against a `reference` (usually `en`, the source of
 * truth). Drives the missing-key linter and the translation-coverage report.
 */
export function diffCatalogs(reference: MessageTree, subject: MessageTree): CatalogDiff {
  const refKeys = new Set(Object.keys(flatten(reference)));
  const subKeys = new Set(Object.keys(flatten(subject)));
  const missing: string[] = [];
  const extra: string[] = [];
  const common: string[] = [];
  for (const k of refKeys) {
    if (subKeys.has(k)) common.push(k);
    else missing.push(k);
  }
  for (const k of subKeys) {
    if (!refKeys.has(k)) extra.push(k);
  }
  return {
    missing: missing.sort(),
    extra: extra.sort(),
    common: common.sort(),
  };
}

/** Look up a dotted key in a nested tree; undefined if absent or not a leaf. */
export function getMessage(tree: MessageTree, dottedKey: string): string | undefined {
  const parts = dottedKey.split(".");
  let node: string | MessageTree | undefined = tree;
  for (const part of parts) {
    if (!isMessageTree(node)) return undefined;
    node = node[part];
  }
  return typeof node === "string" ? node : undefined;
}

/** Coverage ratio of `subject` against `reference` in [0, 1]. */
export function coverage(reference: MessageTree, subject: MessageTree): number {
  const refCount = Object.keys(flatten(reference)).length;
  if (refCount === 0) return 1;
  const { common } = diffCatalogs(reference, subject);
  return common.length / refCount;
}
