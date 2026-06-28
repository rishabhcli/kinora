// RTL / bidirectional-text support.
//
// Pure helpers: which locales are RTL, what the document `dir` should be, and
// Unicode bidi *isolation* of interpolated runs so a LTR value (a number, a Latin
// title) embedded in an RTL sentence — or vice-versa — doesn't visually reorder
// the surrounding punctuation. This is the classic "phone number in Arabic UI"
// bug, fixed with FSI…PDI (First-Strong Isolate / Pop Directional Isolate).

import { primarySubtag, type Direction } from "./types.ts";

/**
 * Base language subtags that are written right-to-left. (Region/script variants
 * resolve through `primarySubtag`.) Covers the common modern RTL scripts.
 */
const RTL_LANGUAGES = new Set([
  "ar", // Arabic
  "he", // Hebrew
  "fa", // Persian / Farsi
  "ur", // Urdu
  "ps", // Pashto
  "sd", // Sindhi
  "ug", // Uyghur
  "yi", // Yiddish
  "dv", // Divehi / Maldivian
  "ku", // Kurdish (Sorani)
  "arc", // Aramaic
  "ckb", // Central Kurdish
]);

/** RTL scripts (when a tag carries an explicit script subtag like "az-Arab"). */
const RTL_SCRIPTS = new Set(["arab", "hebr", "thaa", "syrc", "nkoo", "samr", "mand"]);

/** True iff the locale is written right-to-left. */
export function isRtl(locale: string): boolean {
  const lang = primarySubtag(locale);
  if (RTL_LANGUAGES.has(lang)) return true;
  // explicit script subtag, e.g. "az-Arab", "ku-Arab"
  const parts = locale.toLowerCase().split(/[-_]/);
  return parts.some((p) => p.length === 4 && RTL_SCRIPTS.has(p));
}

/** The text direction for a locale. */
export function directionOf(locale: string): Direction {
  return isRtl(locale) ? "rtl" : "ltr";
}

// ---- Unicode bidi control characters ----

/** First Strong Isolate — opens an isolate whose base direction is auto-detected. */
export const FSI = "⁨";
/** Left-to-Right Isolate. */
export const LRI = "⁦";
/** Right-to-Left Isolate. */
export const RLI = "⁧";
/** Pop Directional Isolate — closes the most recent isolate. */
export const PDI = "⁩";
/** Left-to-Right Mark (zero width). */
export const LRM = "‎";
/** Right-to-Left Mark (zero width). */
export const RLM = "‏";

/**
 * Wrap `text` in a first-strong isolate (FSI…PDI). Use this around any value
 * interpolated into a translated string whose direction may differ from the
 * surrounding sentence — it prevents the embedded run from reordering adjacent
 * punctuation/digits. No-op on empty strings.
 */
export function isolate(text: string): string {
  if (!text) return "";
  return `${FSI}${text}${PDI}`;
}

/**
 * Isolate with an explicit base direction (LRI/RLI…PDI). Prefer `isolate()`
 * (auto) unless you specifically need to force a run's base direction.
 */
export function isolateDir(text: string, dir: Direction): string {
  if (!text) return "";
  const open = dir === "rtl" ? RLI : LRI;
  return `${open}${text}${PDI}`;
}

/** Strip any bidi control characters from a string (e.g. before measuring width). */
export function stripBidiControls(text: string): string {
  return text.replace(/[⁦-⁩‎‏‪-‮]/g, "");
}

/**
 * Heuristically detect the strong direction of a string from its first strong
 * character (mirrors the Unicode "first-strong" algorithm, abbreviated). Returns
 * "ltr" when there is no strong character.
 */
export function firstStrongDirection(text: string): Direction {
  for (const ch of text) {
    const code = ch.codePointAt(0)!;
    // Hebrew (0x0590-0x05FF) + Arabic (0x0600-0x06FF, 0x0750-0x077F) + others
    if (
      (code >= 0x0590 && code <= 0x05ff) ||
      (code >= 0x0600 && code <= 0x06ff) ||
      (code >= 0x0700 && code <= 0x074f) ||
      (code >= 0x0750 && code <= 0x077f) ||
      (code >= 0x08a0 && code <= 0x08ff) ||
      (code >= 0xfb1d && code <= 0xfdff) ||
      (code >= 0xfe70 && code <= 0xfeff)
    ) {
      return "rtl";
    }
    // Basic Latin / Latin-1 letters → strong LTR
    if (
      (code >= 0x0041 && code <= 0x005a) ||
      (code >= 0x0061 && code <= 0x007a) ||
      (code >= 0x00c0 && code <= 0x024f)
    ) {
      return "ltr";
    }
  }
  return "ltr";
}

/**
 * Mirror a CSS logical edge token under RTL: "left" ⟷ "right" when `dir` is rtl.
 * Useful for translating physical CSS in a direction-aware way; "start"/"end"
 * are returned unchanged (already logical).
 */
export function physicalEdge(edge: "left" | "right" | "start" | "end", dir: Direction): "left" | "right" | "start" | "end" {
  if (dir === "ltr") return edge;
  if (edge === "left") return "right";
  if (edge === "right") return "left";
  return edge;
}
