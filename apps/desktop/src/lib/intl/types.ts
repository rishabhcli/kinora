// Core types for the Kinora intl engine.
//
// This module is intentionally framework-agnostic and side-effect-free so it can
// be unit-tested with `node:test` (no jsdom) and reused outside React. Everything
// here is pure type/constant definition or trivial type guards.

/**
 * The locales Kinora seeds. The engine is not *limited* to these — any BCP-47 tag
 * can be passed through to the `Intl` formatters — but these are the catalogs we
 * ship and the codes the language switcher offers.
 */
export const SEED_LOCALES = [
  "en",
  "es",
  "fr",
  "de",
  "zh",
  "hi",
  "ja",
  "ar",
  "pt-BR",
] as const;

export type SeedLocale = (typeof SEED_LOCALES)[number];

/**
 * A locale code is any BCP-47-ish string. We don't constrain it to SeedLocale at
 * the type level because regional variants ("en-GB", "zh-Hant-TW") must flow
 * through formatters unchanged; catalog resolution narrows to a known base.
 */
export type LocaleCode = string;

/** Text direction. RTL locales (ar, he, fa, ur…) render right-to-left. */
export type Direction = "ltr" | "rtl";

/**
 * CLDR plural categories. `Intl.PluralRules` returns a subset of these per locale;
 * an ICU `plural`/`selectordinal` arm must key off one of them (plus exact `=n`).
 */
export type PluralCategory = "zero" | "one" | "two" | "few" | "many" | "other";

export const PLURAL_CATEGORIES: readonly PluralCategory[] = [
  "zero",
  "one",
  "two",
  "few",
  "many",
  "other",
] as const;

/** Values you can interpolate into an ICU message. */
export type IntlValue = string | number | bigint | boolean | Date | null | undefined;

/** The argument bag passed to `t(key, args)`. */
export type IntlArgs = Record<string, IntlValue>;

/**
 * A message catalog is a (possibly nested) tree of string leaves. Leaves are ICU
 * MessageFormat source strings. Nesting maps to dotted keys ("nav.home").
 */
export interface MessageTree {
  [key: string]: string | MessageTree;
}

/** A flattened catalog: dotted-key → ICU source string. */
export type FlatCatalog = Record<string, string>;

/** Metadata the engine tracks per registered locale. */
export interface LocaleMeta {
  /** The base BCP-47 code, e.g. "pt-BR". */
  readonly code: LocaleCode;
  /** Native display name, e.g. "Português (Brasil)". */
  readonly name: string;
  /** English display name, e.g. "Portuguese (Brazil)". */
  readonly englishName: string;
  /** Resolved text direction. */
  readonly dir: Direction;
}

/** Type guard: is the value a plain object (catalog subtree), not an array/null. */
export function isMessageTree(value: unknown): value is MessageTree {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    !(value instanceof Date)
  );
}

/** True iff `code` is one of the seeded locales. */
export function isSeedLocale(code: string): code is SeedLocale {
  return (SEED_LOCALES as readonly string[]).includes(code);
}

/**
 * Normalise a BCP-47 tag to a canonical-ish form: lowercase language, Titlecase
 * script, UPPERCASE region. Pure string work; does not validate against CLDR.
 *
 *   "EN-us"      → "en-US"
 *   "zh-hant-tw" → "zh-Hant-TW"
 *   "PT_br"      → "pt-BR"
 */
export function normalizeTag(tag: string): string {
  const parts = tag.replace(/_/g, "-").split("-").filter(Boolean);
  if (parts.length === 0) return "";
  return parts
    .map((part, i) => {
      if (i === 0) return part.toLowerCase();
      if (part.length === 4) {
        // script subtag → Titlecase
        return part.charAt(0).toUpperCase() + part.slice(1).toLowerCase();
      }
      if (part.length === 2 || part.length === 3) return part.toUpperCase();
      return part.toLowerCase();
    })
    .join("-");
}

/** The primary language subtag of a tag ("en-US" → "en", "zh-Hant" → "zh"). */
export function primarySubtag(tag: string): string {
  return normalizeTag(tag).split("-")[0] ?? tag;
}

/**
 * Build the truncation fallback chain for a tag, most-specific first:
 *   "zh-Hant-TW" → ["zh-Hant-TW", "zh-Hant", "zh"]
 *   "pt-BR"      → ["pt-BR", "pt"]
 *   "en"         → ["en"]
 */
export function truncationChain(tag: string): string[] {
  const norm = normalizeTag(tag);
  const parts = norm.split("-");
  const chain: string[] = [];
  for (let i = parts.length; i >= 1; i--) {
    chain.push(parts.slice(0, i).join("-"));
  }
  return chain;
}
