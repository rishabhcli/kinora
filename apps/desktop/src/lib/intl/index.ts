// Public barrel for the Kinora intl engine.
//
// Framework-agnostic, side-effect-free. The React integration lives in
// `apps/desktop/src/i18n/` (IntlProvider, useT). Import the engine from here to
// use it standalone (tests, tooling, non-React surfaces).

// Types
export {
  SEED_LOCALES,
  PLURAL_CATEGORIES,
  isMessageTree,
  isSeedLocale,
  normalizeTag,
  primarySubtag,
  truncationChain,
} from "./types.ts";
export type {
  SeedLocale,
  LocaleCode,
  Direction,
  PluralCategory,
  IntlValue,
  IntlArgs,
  MessageTree,
  FlatCatalog,
  LocaleMeta,
} from "./types.ts";

// Detection / negotiation
export {
  negotiateLocale,
  resolveCatalogBase,
  matchCatalogBase,
  pickInitialLocale,
} from "./detect.ts";
export type { NegotiateOptions } from "./detect.ts";

// Plural / format
export { pluralCategory, ordinalCategory, selectPluralArm } from "./plural.ts";
export {
  formatNumber,
  formatInteger,
  formatDecimal,
  formatPercent,
  formatCompact,
  formatCurrency,
  formatUnit,
  formatDate,
  formatTime,
  formatDateTime,
  formatRelative,
  formatRelativeAuto,
  formatList,
  languageDisplayName,
  regionDisplayName,
  currencyDisplayName,
} from "./format.ts";
export type { DateStyle } from "./format.ts";

// ICU
export {
  parse,
  tryParse,
  compile,
  evaluate,
  evaluateParts,
  formatMessage,
  formatMessageToParts,
  ICUParseError,
} from "./icu/index.ts";
export type { Message, MessageNode, Part, EvalContext } from "./icu/index.ts";

// Bidi
export {
  isRtl,
  directionOf,
  isolate,
  isolateDir,
  stripBidiControls,
  firstStrongDirection,
  physicalEdge,
  FSI,
  PDI,
  LRI,
  RLI,
  LRM,
  RLM,
} from "./bidi.ts";

// Pseudo
export { pseudoLocalize, pseudoLocalizeCatalog } from "./pseudo.ts";
export type { PseudoOptions } from "./pseudo.ts";

// Catalog ops
export {
  flatten,
  unflatten,
  deepMerge,
  diffCatalogs,
  getMessage,
  coverage,
  keysOf,
} from "./catalog.ts";
export type { CatalogDiff } from "./catalog.ts";

// Lint / extract
export { lintCatalog, collectArguments, formatLintReport } from "./lint.ts";
export type { LintIssue, LintResult, LintOptions, Severity } from "./lint.ts";
export { extractKeys, extractKeySet, crossReference } from "./extract.ts";
export type { ExtractedKey, ExtractResult, CoverageReport } from "./extract.ts";

// Engine
export { Translator, createTranslator } from "./engine.ts";
export type { TranslatorOptions } from "./engine.ts";
