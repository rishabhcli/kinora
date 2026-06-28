// Kinora desktop i18n foundation.
//
// Single i18next singleton: react-i18next reads this instance via the React
// context that `initReactI18next` registers, so an <I18nextProvider> is optional
// — importing this module for its side effect (in main.tsx, before render) is
// enough to wire the whole app. See `setLanguage` for the persist + <html lang>
// contract the Settings language selector relies on.
import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import LanguageDetector from "i18next-browser-languagedetector";

import en from "./locales/en.json";
import es from "./locales/es.json";
import fr from "./locales/fr.json";
import de from "./locales/de.json";
import zh from "./locales/zh.json";
import hi from "./locales/hi.json";

/** Persisted localStorage key for the reader's chosen UI language. */
export const LANG_STORAGE_KEY = "kinora.lang";

/** The languages Kinora ships, in display order, by their native name. */
export const SUPPORTED_LANGUAGES = [
  { code: "en", name: "English" },
  { code: "es", name: "Español" },
  { code: "fr", name: "Français" },
  { code: "de", name: "Deutsch" },
  { code: "zh", name: "中文" },
  { code: "hi", name: "हिन्दी" },
] as const;

export type LanguageCode = (typeof SUPPORTED_LANGUAGES)[number]["code"];

export const SUPPORTED_LANGUAGE_CODES: LanguageCode[] = SUPPORTED_LANGUAGES.map(
  (l) => l.code,
);

// `en` is the source of truth + the fallback; the others mirror its shape.
const resources = {
  en: { translation: en },
  es: { translation: es },
  fr: { translation: fr },
  de: { translation: de },
  zh: { translation: zh },
  hi: { translation: hi },
} as const;

// Augment react-i18next so `t("nav.home")` is type-checked against the en catalog.
declare module "i18next" {
  interface CustomTypeOptions {
    defaultNS: "translation";
    resources: { translation: typeof en };
  }
}

void i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources,
    fallbackLng: "en",
    supportedLngs: SUPPORTED_LANGUAGE_CODES,
    // Map regional variants (e.g. "en-US", "zh-CN") onto the base catalog.
    load: "languageOnly",
    nonExplicitSupportedLngs: true,
    defaultNS: "translation",
    detection: {
      order: ["localStorage", "navigator"],
      lookupLocalStorage: LANG_STORAGE_KEY,
      caches: ["localStorage"],
    },
    interpolation: {
      // React already escapes; double-escaping would mangle the UI.
      escapeValue: false,
    },
    returnNull: false,
  });

// Keep <html lang> in sync from the very first detected language.
if (typeof document !== "undefined") {
  document.documentElement.lang = i18n.resolvedLanguage ?? i18n.language ?? "en";
}

/**
 * Switch the active UI language, persist it, and update `<html lang>`.
 * react-i18next re-renders every `useTranslation()` consumer live.
 */
export function setLanguage(lng: LanguageCode): void {
  void i18n.changeLanguage(lng);
  try {
    localStorage.setItem(LANG_STORAGE_KEY, lng);
  } catch {
    /* storage blocked — in-memory language change still applies */
  }
  if (typeof document !== "undefined") {
    document.documentElement.lang = lng;
  }
}

/** The currently active language code, normalised to a supported base code. */
export function currentLanguage(): LanguageCode {
  const resolved = (i18n.resolvedLanguage ?? i18n.language ?? "en").split("-")[0];
  return (SUPPORTED_LANGUAGE_CODES as string[]).includes(resolved)
    ? (resolved as LanguageCode)
    : "en";
}

export { i18n };
export default i18n;
