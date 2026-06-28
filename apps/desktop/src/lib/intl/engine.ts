// The Translator — the runtime object that resolves a key + args to a string in a
// given locale. It owns the registered catalogs, the active locale, the fallback
// chain, optional pseudo-localization, and direction. Framework-agnostic: the
// React layer wraps an instance, but it works standalone (and is node-testable).

import {
  type FlatCatalog,
  type IntlArgs,
  type LocaleCode,
  type MessageTree,
  type Direction,
} from "./types.ts";
import { flatten } from "./catalog.ts";
import { directionOf } from "./bidi.ts";
import { formatMessage, formatMessageToParts, type Part } from "./icu/index.ts";
import { pseudoLocalize } from "./pseudo.ts";
import { resolveCatalogBase } from "./detect.ts";
import {
  formatNumber,
  formatCurrency,
  formatDate,
  formatTime,
  formatRelativeAuto,
  formatList,
} from "./format.ts";

export interface TranslatorOptions {
  /** Initial active locale (must be a registered catalog or it resolves to fallback). */
  locale: LocaleCode;
  /** The source/fallback locale (always last in the chain). */
  fallback?: LocaleCode;
  /** Render pseudo-localized text for QA (default false). */
  pseudo?: boolean;
  /** What to render for a key that resolves nowhere: "key" (default) or "empty". */
  onMissing?: "key" | "empty";
  /**
   * Called when a key is missing from *every* locale in the chain. Useful to log
   * or accumulate missing keys in dev. Receives the key + active locale.
   */
  onMissingKey?: (key: string, locale: LocaleCode) => void;
}

interface RegisteredCatalog {
  flat: FlatCatalog;
}

export class Translator {
  private catalogs = new Map<LocaleCode, RegisteredCatalog>();
  private locale: LocaleCode;
  private readonly fallback: LocaleCode;
  private pseudo: boolean;
  private readonly onMissing: "key" | "empty";
  private readonly onMissingKey?: (key: string, locale: LocaleCode) => void;
  private listeners = new Set<(locale: LocaleCode) => void>();

  constructor(options: TranslatorOptions) {
    this.fallback = options.fallback ?? "en";
    this.locale = options.locale;
    this.pseudo = options.pseudo ?? false;
    this.onMissing = options.onMissing ?? "key";
    this.onMissingKey = options.onMissingKey;
  }

  /** Register (or replace) a locale's catalog from a nested message tree. */
  register(locale: LocaleCode, tree: MessageTree): this {
    this.catalogs.set(locale, { flat: flatten(tree) });
    return this;
  }

  /** Register many catalogs at once. */
  registerAll(map: Record<LocaleCode, MessageTree>): this {
    for (const [locale, tree] of Object.entries(map)) this.register(locale, tree);
    return this;
  }

  /** True iff a catalog is registered for `locale`. */
  has(locale: LocaleCode): boolean {
    return this.catalogs.has(locale);
  }

  /** The locales with registered catalogs. */
  registeredLocales(): LocaleCode[] {
    return [...this.catalogs.keys()];
  }

  /** The active locale. */
  getLocale(): LocaleCode {
    return this.locale;
  }

  /** Set the active locale (resolving regional → base if needed) + notify listeners. */
  setLocale(locale: LocaleCode): void {
    const resolved = resolveCatalogBase(locale, this.registeredLocales(), this.fallback);
    this.locale = resolved;
    for (const fn of this.listeners) fn(resolved);
  }

  /** Toggle pseudo-localization (QA). */
  setPseudo(on: boolean): void {
    this.pseudo = on;
    for (const fn of this.listeners) fn(this.locale);
  }

  isPseudo(): boolean {
    return this.pseudo;
  }

  /** Subscribe to locale/pseudo changes; returns an unsubscribe fn. */
  subscribe(fn: (locale: LocaleCode) => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  /** The text direction of the active locale. */
  direction(): Direction {
    return directionOf(this.locale);
  }

  /**
   * The locale fallback chain: active → fallback. (Both deduped.) The engine walks
   * this to resolve a key, so a partially-translated locale still renders.
   */
  private chain(locale: LocaleCode): LocaleCode[] {
    const chain = [locale];
    if (!chain.includes(this.fallback)) chain.push(this.fallback);
    return chain;
  }

  /** Resolve the raw ICU source for a key, walking the fallback chain. */
  private resolveSource(key: string, locale: LocaleCode): string | undefined {
    for (const lng of this.chain(locale)) {
      const cat = this.catalogs.get(lng);
      const src = cat?.flat[key];
      if (src !== undefined) return src;
    }
    return undefined;
  }

  /**
   * Translate `key` with `args` in the active (or overridden) locale. Resolves the
   * ICU source via the fallback chain, optionally pseudo-localizes, then formats.
   */
  t(key: string, args: IntlArgs = {}, localeOverride?: LocaleCode): string {
    const locale = localeOverride ?? this.locale;
    let src = this.resolveSource(key, locale);
    if (src === undefined) {
      this.onMissingKey?.(key, locale);
      return this.onMissing === "empty" ? "" : key;
    }
    if (this.pseudo) src = pseudoLocalize(src);
    return formatMessage(src, locale, args, this.onMissing);
  }

  /**
   * Translate to rich-text parts (tags preserved) for the React layer to render
   * `<b>` etc. as real elements.
   */
  tParts(key: string, args: IntlArgs = {}, localeOverride?: LocaleCode): Part[] {
    const locale = localeOverride ?? this.locale;
    let src = this.resolveSource(key, locale);
    if (src === undefined) {
      this.onMissingKey?.(key, locale);
      return [{ type: "text", value: this.onMissing === "empty" ? "" : key }];
    }
    if (this.pseudo) src = pseudoLocalize(src);
    return formatMessageToParts(src, locale, args, this.onMissing);
  }

  /** True iff `key` resolves anywhere in the active chain. */
  exists(key: string, localeOverride?: LocaleCode): boolean {
    return this.resolveSource(key, localeOverride ?? this.locale) !== undefined;
  }

  // ---- Convenience formatters bound to the active locale ----

  number(value: number, options?: Intl.NumberFormatOptions): string {
    return formatNumber(value, this.locale, options);
  }

  currency(value: number, currency: string, options?: Intl.NumberFormatOptions): string {
    return formatCurrency(value, this.locale, currency, options);
  }

  date(value: Date | number | string, options?: Intl.DateTimeFormatOptions): string {
    return formatDate(value, this.locale, options);
  }

  time(value: Date | number | string, options?: Intl.DateTimeFormatOptions): string {
    return formatTime(value, this.locale, options);
  }

  relative(date: Date | number, now?: Date | number): string {
    return formatRelativeAuto(date, this.locale, now);
  }

  list(items: readonly string[], options?: Intl.ListFormatOptions): string {
    return formatList(items, this.locale, options);
  }
}

/** Construct a Translator and register all catalogs in one call. */
export function createTranslator(
  options: TranslatorOptions,
  catalogs: Record<LocaleCode, MessageTree>,
): Translator {
  return new Translator(options).registerAll(catalogs);
}
