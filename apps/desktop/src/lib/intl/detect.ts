// Locale negotiation — pure functions for resolving the best supported locale.
//
// Split from any browser/DOM access so it is node-testable. The React layer feeds
// it `navigator.languages` and the stored preference; this module just negotiates.

import { normalizeTag, primarySubtag, truncationChain, type LocaleCode } from "./types.ts";

export interface NegotiateOptions {
  /** The user's requested locales, most-preferred first (e.g. navigator.languages). */
  requested: readonly string[];
  /** The locales we actually have catalogs for. */
  supported: readonly string[];
  /** Fallback when nothing matches. */
  fallback: LocaleCode;
}

/**
 * Negotiate the best supported locale for a requested list. The algorithm, in
 * priority order, mirrors RFC 4647 "lookup" with progressive truncation:
 *
 *   1. exact normalised match ("en-US" wants "en-US")
 *   2. requested-truncation match ("en-US" → "en")
 *   3. supported-base match (requested "en" satisfied by supported "en-US")
 *
 * The first requested tag that resolves wins; ties resolve to the supported tag
 * that appears earliest in `supported`.
 */
export function negotiateLocale(opts: NegotiateOptions): LocaleCode {
  const supported = opts.supported.map(normalizeTag);
  const supportedSet = new Set(supported);

  for (const raw of opts.requested) {
    if (!raw) continue;
    const chain = truncationChain(raw);

    // (1)+(2): requested (and its truncations) directly present in supported.
    for (const candidate of chain) {
      if (supportedSet.has(candidate)) {
        return supportedIndex(supported, candidate);
      }
    }

    // (3): a supported tag whose base equals the requested base.
    const base = primarySubtag(raw);
    const baseMatch = supported.find((s) => primarySubtag(s) === base);
    if (baseMatch) return baseMatch;
  }

  return normalizeTag(opts.fallback);
}

/** Return the canonical supported tag (keeps the supported-list casing). */
function supportedIndex(supported: string[], candidate: string): string {
  const idx = supported.indexOf(candidate);
  return idx >= 0 ? supported[idx] : candidate;
}

/**
 * Resolve which supported *catalog base* a (possibly regional) locale maps onto,
 * or `null` when none does. Truncation first ("es-MX" → "es"), then base match
 * ("pt" satisfied by supported "pt-BR"). Does NOT apply the blind fallback —
 * callers decide what to do with a miss.
 */
export function matchCatalogBase(
  locale: string,
  supported: readonly string[],
): LocaleCode | null {
  const supportedNorm = supported.map(normalizeTag);
  const set = new Set(supportedNorm);
  for (const candidate of truncationChain(locale)) {
    if (set.has(candidate)) return candidate;
  }
  const base = primarySubtag(locale);
  return supportedNorm.find((s) => primarySubtag(s) === base) ?? null;
}

/**
 * Resolve which *catalog base* a (possibly regional) locale should load. A
 * regional tag with no own catalog falls back to its base ("es-MX" → "es") if
 * that base is supported, else to `fallback`.
 */
export function resolveCatalogBase(
  locale: string,
  supported: readonly string[],
  fallback: LocaleCode,
): LocaleCode {
  return matchCatalogBase(locale, supported) ?? normalizeTag(fallback);
}

/**
 * Pick the initial locale from a stored preference + the navigator list. A stored
 * value (the explicit user choice) always wins if it is supported; otherwise we
 * negotiate from the navigator-provided list.
 */
export function pickInitialLocale(args: {
  stored: string | null | undefined;
  navigator: readonly string[];
  supported: readonly string[];
  fallback: LocaleCode;
}): LocaleCode {
  if (args.stored) {
    // Honour the explicit stored choice only if it maps onto a *real* catalog
    // (not the blind fallback) — otherwise fall through to navigator negotiation.
    const base = matchCatalogBase(args.stored, args.supported);
    if (base) return base;
  }
  return negotiateLocale({
    requested: args.navigator,
    supported: args.supported,
    fallback: args.fallback,
  });
}
