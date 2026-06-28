// Ambient declarations for the `Intl` APIs the engine uses that are NOT present
// in the project's `ES2020` TypeScript lib (Intl.ListFormat / Intl.DisplayNames
// landed in the ES2021/ES2022 libs). The *runtime* (Node 18+, Chromium/Electron)
// has shipped these for years; this only teaches the type checker about them so
// we don't have to bump the shared `apps/desktop/tsconfig.json` `lib` target.
//
// Scoped to `lib/intl/**` — additive, no behavioural effect.

declare namespace Intl {
  type ListFormatType = "conjunction" | "disjunction" | "unit";
  type ListFormatStyle = "long" | "short" | "narrow";

  interface ListFormatOptions {
    localeMatcher?: "lookup" | "best fit";
    type?: ListFormatType;
    style?: ListFormatStyle;
  }

  interface ListFormat {
    format(list: Iterable<string>): string;
    formatToParts(list: Iterable<string>): Array<{ type: "element" | "literal"; value: string }>;
    resolvedOptions(): { locale: string; type: ListFormatType; style: ListFormatStyle };
  }

  const ListFormat: {
    new (locales?: string | string[], options?: ListFormatOptions): ListFormat;
    prototype: ListFormat;
    supportedLocalesOf(locales: string | string[], options?: ListFormatOptions): string[];
  };

  type DisplayNamesType = "language" | "region" | "script" | "currency" | "calendar" | "dateTimeField";
  type DisplayNamesFallback = "code" | "none";
  type DisplayNamesLanguageDisplay = "dialect" | "standard";

  interface DisplayNamesOptions {
    localeMatcher?: "lookup" | "best fit";
    style?: "narrow" | "short" | "long";
    type: DisplayNamesType;
    fallback?: DisplayNamesFallback;
    languageDisplay?: DisplayNamesLanguageDisplay;
  }

  interface DisplayNames {
    of(code: string): string | undefined;
    resolvedOptions(): {
      locale: string;
      style: "narrow" | "short" | "long";
      type: DisplayNamesType;
      fallback: DisplayNamesFallback;
    };
  }

  const DisplayNames: {
    new (locales: string | string[] | undefined, options: DisplayNamesOptions): DisplayNames;
    prototype: DisplayNames;
    supportedLocalesOf(locales: string | string[], options?: { localeMatcher?: "lookup" | "best fit" }): string[];
  };
}
