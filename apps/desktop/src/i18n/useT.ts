// Typed translation hooks over the IntlProvider.
//
// `useT()` returns a `t(key, args)` bound to the active locale; `key` is the
// compile-time `MessageKey` union derived from the en catalog, so a typo is a
// build error. `useLocale()` / `useDirection()` expose the active locale + RTL,
// and `useIntl()` returns the full context (translator + formatters + switcher).

import { useContext } from "react";
import { IntlContext, type IntlContextValue } from "./IntlProvider.tsx";
import type { IntlArgs, Direction } from "../lib/intl/types.ts";
import type { Part } from "../lib/intl/icu/index.ts";
import type { MessageKey, LocaleCode } from "./messages.ts";

function useCtx(): IntlContextValue {
  const ctx = useContext(IntlContext);
  if (!ctx) {
    throw new Error("useT/useIntl must be used within an <IntlProvider>");
  }
  return ctx;
}

/** The full intl context (translator, locale, direction, switcher, formatters). */
export function useIntl(): IntlContextValue {
  return useCtx();
}

/** A type-checked `t(key, args)` bound to the active locale. */
export type TFunction = (key: MessageKey, args?: IntlArgs) => string;

/** Translate a key to a string. Re-renders when the locale or pseudo flag changes. */
export function useT(): TFunction {
  const { translator } = useCtx();
  return (key: MessageKey, args?: IntlArgs) => translator.t(key, args);
}

/** Translate a key to rich-text parts (tags preserved) for `<T>` / custom render. */
export function useTParts(): (key: MessageKey, args?: IntlArgs) => Part[] {
  const { translator } = useCtx();
  return (key: MessageKey, args?: IntlArgs) => translator.tParts(key, args);
}

/** The active locale code. */
export function useLocale(): LocaleCode {
  return useCtx().locale;
}

/** The active text direction ("ltr" | "rtl"). */
export function useDirection(): Direction {
  return useCtx().direction;
}

/** The locale switcher + loading flag (for a language selector control). */
export function useLocaleSwitch(): {
  locale: LocaleCode;
  setLocale: (code: LocaleCode) => Promise<void>;
  loading: boolean;
} {
  const { locale, setLocale, loading } = useCtx();
  return { locale, setLocale, loading };
}

/** Locale-bound formatters (number/currency/date/relative/list). */
export function useFormatters() {
  const { translator } = useCtx();
  return {
    number: translator.number.bind(translator),
    currency: translator.currency.bind(translator),
    date: translator.date.bind(translator),
    time: translator.time.bind(translator),
    relative: translator.relative.bind(translator),
    list: translator.list.bind(translator),
  };
}
