// Typed message registry + lazy locale loading.
//
// `en` is the source of truth and is imported statically: its shape derives the
// compile-time `MessageKey` union so `t("nav.home")` is checked but `t("nav.xyz")`
// is a type error. Every other locale is a lazy chunk (`import()`d on demand) so
// the initial bundle ships only English; switching language fetches the catalog.
//
// This file is part of the i18n domain and is intentionally decoupled from the
// existing i18next singleton (`i18n/index.ts`): both can coexist while components
// migrate to the typed engine at their own pace.

import en from "./locales/en.json";
import type { MessageTree } from "../lib/intl/types.ts";

/** The shipped locales, in display order, with native names + direction. */
export const LOCALES = [
  { code: "en", name: "English", englishName: "English", dir: "ltr" },
  { code: "es", name: "Español", englishName: "Spanish", dir: "ltr" },
  { code: "fr", name: "Français", englishName: "French", dir: "ltr" },
  { code: "de", name: "Deutsch", englishName: "German", dir: "ltr" },
  { code: "zh", name: "中文", englishName: "Chinese", dir: "ltr" },
  { code: "hi", name: "हिन्दी", englishName: "Hindi", dir: "ltr" },
  { code: "ja", name: "日本語", englishName: "Japanese", dir: "ltr" },
  { code: "ar", name: "العربية", englishName: "Arabic", dir: "rtl" },
  { code: "pt-BR", name: "Português (Brasil)", englishName: "Portuguese (Brazil)", dir: "ltr" },
] as const;

export type LocaleCode = (typeof LOCALES)[number]["code"];

export const LOCALE_CODES: LocaleCode[] = LOCALES.map((l) => l.code);

/** Source-of-truth catalog (English). Always available, never lazy. */
export const SOURCE_CATALOG = en as unknown as MessageTree;
export const SOURCE_LOCALE: LocaleCode = "en";

// ---- Compile-time key typing -------------------------------------------

type Primitive = string | number | boolean | null | undefined;

/**
 * Recursively derive the dotted-key union of a message tree. A leaf (`string`)
 * contributes its path; a subtree recurses with the path prefixed.
 *
 *   { nav: { home: "Home" } } → "nav.home"
 */
type DottedKeys<T, Prefix extends string = ""> = {
  [K in keyof T & string]: T[K] extends Primitive
    ? `${Prefix}${K}`
    : DottedKeys<T[K], `${Prefix}${K}.`>;
}[keyof T & string];

/** The compile-time union of every valid message key (derived from `en`). */
export type MessageKey = DottedKeys<typeof en>;

// ---- Lazy locale loading ------------------------------------------------

// Vite turns this glob into a map of dynamic importers (one chunk per JSON), so a
// locale catalog is only fetched when first requested. `eager: false` keeps them
// out of the initial bundle. The key is the relative path under ./locales.
const loaders = import.meta.glob<{ default: MessageTree }>("./locales/*.json");

function loaderFor(code: LocaleCode): (() => Promise<{ default: MessageTree }>) | undefined {
  return loaders[`./locales/${code}.json`];
}

const cache = new Map<LocaleCode, MessageTree>();
cache.set(SOURCE_LOCALE, SOURCE_CATALOG);

/**
 * Load a locale catalog (lazy chunk). The source locale is returned synchronously
 * from cache. Unknown locales reject. Repeated loads are memoised.
 */
export async function loadCatalog(code: LocaleCode): Promise<MessageTree> {
  const hit = cache.get(code);
  if (hit) return hit;
  const loader = loaderFor(code);
  if (!loader) {
    throw new Error(`no catalog chunk for locale "${code}"`);
  }
  const mod = await loader();
  const tree = (mod.default ?? mod) as MessageTree;
  cache.set(code, tree);
  return tree;
}

/** Synchronously read an already-loaded catalog (or undefined). */
export function peekCatalog(code: LocaleCode): MessageTree | undefined {
  return cache.get(code);
}

/** Is a locale's catalog already in memory? */
export function isLoaded(code: LocaleCode): boolean {
  return cache.has(code);
}

/** Test seam: prime the cache without a dynamic import. */
export function _primeCatalog(code: LocaleCode, tree: MessageTree): void {
  cache.set(code, tree);
}

/** Metadata for a locale code (falls back to the en row for unknown codes). */
export function localeMeta(code: string) {
  return LOCALES.find((l) => l.code === code) ?? LOCALES[0];
}
