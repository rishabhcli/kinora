// IntlProvider — the React adapter for the framework-agnostic Translator engine.
//
// Responsibilities:
//   • own a single Translator, seeded with the source (en) catalog
//   • detect the initial locale (stored pref → navigator → fallback)
//   • lazy-load a locale's catalog on first switch, then activate it
//   • persist the chosen locale (sharing LANG_STORAGE_KEY with the i18next setup
//     so the two layers never disagree) and sync <html lang> + dir
//   • re-render consumers on locale / pseudo change via useSyncExternalStore
//
// It is ADDITIVE: components that still use react-i18next are unaffected; new code
// can adopt `useT()`. Mount it anywhere above the consumers (see App.tsx note).

import {
  createContext,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import { Translator } from "../lib/intl/engine.ts";
import { directionOf } from "../lib/intl/bidi.ts";
import { pickInitialLocale } from "../lib/intl/detect.ts";
import type { Direction } from "../lib/intl/types.ts";
import {
  LOCALE_CODES,
  SOURCE_CATALOG,
  SOURCE_LOCALE,
  loadCatalog,
  peekCatalog,
  localeMeta,
  type LocaleCode,
} from "./messages.ts";

/** Shared with `i18n/index.ts` so the engine + i18next persist the same key. */
export const LANG_STORAGE_KEY = "kinora.lang";
/** Persisted flag enabling pseudo-localization (QA). */
export const PSEUDO_STORAGE_KEY = "kinora.i18n.pseudo";

export interface IntlContextValue {
  translator: Translator;
  locale: LocaleCode;
  direction: Direction;
  pseudo: boolean;
  /** Switch locale (lazy-loads the chunk, persists, updates <html>). */
  setLocale: (code: LocaleCode) => Promise<void>;
  /** Toggle pseudo-localization. */
  setPseudo: (on: boolean) => void;
  /** True while a locale chunk is being fetched. */
  loading: boolean;
}

export const IntlContext = createContext<IntlContextValue | null>(null);

function readStored(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStored(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* storage blocked — in-memory change still applies */
  }
}

function navigatorLanguages(): string[] {
  if (typeof navigator === "undefined") return [];
  if (Array.isArray(navigator.languages) && navigator.languages.length) {
    return [...navigator.languages];
  }
  return navigator.language ? [navigator.language] : [];
}

function applyHtml(locale: string): void {
  if (typeof document === "undefined") return;
  document.documentElement.lang = locale;
  document.documentElement.dir = directionOf(locale);
}

export interface IntlProviderProps {
  children: ReactNode;
  /** Force an initial locale (skips detection) — handy for tests / Storybook. */
  initialLocale?: LocaleCode;
  /** Start in pseudo-localization mode. */
  initialPseudo?: boolean;
}

export function IntlProvider({ children, initialLocale, initialPseudo }: IntlProviderProps) {
  // One Translator for the provider's lifetime, seeded with the en source.
  const translatorRef = useRef<Translator | null>(null);
  if (translatorRef.current === null) {
    const initial =
      initialLocale ??
      (pickInitialLocale({
        stored: readStored(LANG_STORAGE_KEY),
        navigator: navigatorLanguages(),
        supported: LOCALE_CODES,
        fallback: SOURCE_LOCALE,
      }) as LocaleCode);
    const t = new Translator({ locale: SOURCE_LOCALE, fallback: SOURCE_LOCALE });
    t.register(SOURCE_LOCALE, SOURCE_CATALOG);
    const pseudo = initialPseudo ?? readStored(PSEUDO_STORAGE_KEY) === "1";
    if (pseudo) t.setPseudo(true);
    // If the initial locale is already cached (en, or test-primed), activate now.
    const cached = peekCatalog(initial);
    if (cached) {
      t.register(initial, cached);
      t.setLocale(initial);
    }
    translatorRef.current = t;
  }
  const translator = translatorRef.current;

  const [loading, setLoading] = useState(false);

  // Subscribe React to the engine's locale/pseudo changes.
  const subscribe = useCallback(
    (onChange: () => void) => translator.subscribe(onChange),
    [translator],
  );
  const locale = useSyncExternalStore(
    subscribe,
    () => translator.getLocale() as LocaleCode,
    () => translator.getLocale() as LocaleCode,
  );
  const pseudo = useSyncExternalStore(
    subscribe,
    () => translator.isPseudo(),
    () => translator.isPseudo(),
  );

  // Drive the initial (possibly lazy) locale once on mount.
  useEffect(() => {
    const target =
      initialLocale ??
      (pickInitialLocale({
        stored: readStored(LANG_STORAGE_KEY),
        navigator: navigatorLanguages(),
        supported: LOCALE_CODES,
        fallback: SOURCE_LOCALE,
      }) as LocaleCode);
    applyHtml(target);
    if (!translator.has(target)) {
      void doSetLocale(target);
    } else {
      translator.setLocale(target);
      applyHtml(translator.getLocale());
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const doSetLocale = useCallback(
    async (code: LocaleCode) => {
      // Persist the *intent* up-front so the choice survives even if the async
      // chunk load is interrupted (and so it can't race a caller that doesn't
      // await this promise). The active locale is re-confirmed below.
      writeStored(LANG_STORAGE_KEY, code);
      if (!translator.has(code)) {
        setLoading(true);
        try {
          const tree = await loadCatalog(code);
          translator.register(code, tree);
        } catch {
          // Fall back silently to the source locale if the chunk fails to load.
          setLoading(false);
          return;
        }
        setLoading(false);
      }
      translator.setLocale(code);
      const active = translator.getLocale();
      writeStored(LANG_STORAGE_KEY, active);
      applyHtml(active);
    },
    [translator],
  );

  const setPseudo = useCallback(
    (on: boolean) => {
      translator.setPseudo(on);
      writeStored(PSEUDO_STORAGE_KEY, on ? "1" : "0");
    },
    [translator],
  );

  const value = useMemo<IntlContextValue>(
    () => ({
      translator,
      locale,
      direction: directionOf(locale),
      pseudo,
      setLocale: doSetLocale,
      setPseudo,
      loading,
    }),
    [translator, locale, pseudo, doSetLocale, setPseudo, loading],
  );

  return <IntlContext.Provider value={value}>{children}</IntlContext.Provider>;
}

/** The localized native name for a locale code (for a language switcher UI). */
export function localeName(code: LocaleCode): string {
  return localeMeta(code).name;
}
