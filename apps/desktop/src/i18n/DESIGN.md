# Kinora i18n & localization — DESIGN.md (living roadmap)

Owner domain: `apps/desktop/src/i18n/**`, `apps/desktop/src/i18n/locales/**`, and a NEW
`apps/desktop/src/lib/intl/**`. This agent owns ONLY these paths. It must NOT edit
`components/*`, `reading/*`, `styles/*`, or `lib/api.ts` (the user is editing those live).

The single shared file we may *lightly, additively* touch is `App.tsx` (to register a
provider) — see §"Additive shared-file changes".

## Why a two-layer system

The repo already ships an i18next singleton (`i18n/index.ts`) that the existing components
adopt via `useTranslation()`. We keep that working untouched-in-spirit and build a richer,
**framework-agnostic engine** beneath it so:

1. The pure formatting/ICU logic is testable with `node:test` (no jsdom, fast — the repo's
   `run-node-tests.mjs` convention) and reusable outside React.
2. New surfaces can adopt the typed `t()` / `useT()` hook for compile-time key safety, ICU
   plurals/gender/select, and locale-aware number/date/currency/relative-time/list/unit
   formatting — without rewriting the i18next consumers.
3. QA gets pseudo-localization, a missing-key linter, and a key-extraction tool.

```
lib/intl/         ← pure engine (no React, no i18next) — node:test covered
  ├─ types.ts          locale codes, message-catalog typing helpers
  ├─ icu/              ICU MessageFormat: lexer → parser → AST → evaluator
  ├─ plural.ts         CLDR plural-category resolution (Intl.PluralRules wrapper)
  ├─ format.ts         Intl wrappers: number/currency/date/time/relative/list/unit/compact
  ├─ bidi.ts           RTL detection, direction, Unicode bidi isolation helpers
  ├─ pseudo.ts         pseudo-localization transformer for QA
  ├─ detect.ts         locale negotiation (navigator/stored/supported) — pure
  ├─ catalog.ts        flatten/unflatten, deep-merge, key diffing
  ├─ lint.ts           missing/extra-key + ICU-validity + placeholder-parity linter
  ├─ extract.ts        source-scan key extractor (t("…") call sites)
  ├─ engine.ts         Translator: catalog + locale → t(key, args) with ICU + fallback
  └─ index.ts          barrel

i18n/             ← React/i18next integration layer
  ├─ index.ts          (existing) i18next singleton — left as-is in spirit
  ├─ IntlProvider.tsx  React context wrapping the engine + i18next sync
  ├─ useT.ts           typed t() hook + useLocale()/useDirection()
  ├─ messages.ts       typed catalog registry + key-type derivation from en
  ├─ locales/          per-locale JSON catalogs (en/es/fr/de/zh/hi/ja/ar/pt-BR…)
  └─ DESIGN.md         this file
```

## Milestones

- [x] M0  Baseline: read existing i18next setup, kinora.md §5, test conventions. Green.
- [x] M1  `lib/intl/types.ts` + `detect.ts` (pure locale negotiation) + tests.
- [x] M2  `lib/intl/plural.ts` (CLDR categories) + `format.ts` (Intl wrappers) + tests.
- [x] M3  `lib/intl/icu/` full ICU MessageFormat: lexer, parser, evaluator + tests.
- [x] M4  `lib/intl/bidi.ts` RTL/bidi + `pseudo.ts` pseudo-localization + tests.
- [x] M5  `lib/intl/catalog.ts` (flatten/merge/diff) + `lint.ts` + `extract.ts` + tests.
- [x] M6  `lib/intl/engine.ts` Translator (catalog+locale→ICU t) + fallback chain + tests.
- [x] M7  React layer: `IntlProvider.tsx`, `useT.ts`, `messages.ts` (typed) + tests.
- [x] M8  Seed locales: add `ja` + `ar` (RTL); extend en catalog with ICU samples.
- [x] M9  Additive `App.tsx` provider registration (documented, no behavior change).
- [x] M10 CLI runner for lint/extract (`lib/intl/cli.ts`) + node entry.
- [x] M11 Hardening: number/currency edge cases, locale-data fallback, doc polish.

## Shipped surface (current)

Engine (`lib/intl/`, framework-agnostic, node-testable):
- `types.ts` — locale codes, tag normalization, truncation fallback chain.
- `detect.ts` — RFC-4647-lookup-style negotiation + initial-locale pick.
- `plural.ts` — CLDR cardinal/ordinal categories via `Intl.PluralRules`.
- `format.ts` — number/integer/decimal/percent/compact/currency/unit, date/time,
  relative (+ auto-unit), list, and display-name formatters (all cached).
- `icu/` — full ICU MessageFormat: `parse`/`tryParse` (recursive descent, ICU
  apostrophe quoting, plural `offset:`+`#`, selectordinal, select, rich-text
  tags) **plus i18next `{{var}}` double-brace interpolation** so existing
  catalogs work unchanged; `evaluate`/`evaluateParts`; memoised `compile`/
  `formatMessage`/`formatMessageToParts`.
- `bidi.ts` — RTL detection (lang + script subtag), direction, FSI/PDI isolation,
  first-strong detection, logical-edge mirroring.
- `pseudo.ts` — pseudo-localization (accent + expand + bracket) that preserves
  placeholders/tags/quotes; deep catalog variant.
- `catalog.ts` — flatten/unflatten/deepMerge/diff/coverage/getMessage.
- `lint.ts` — missing/extra-key, invalid-ICU, placeholder-drift linter + report.
- `extract.ts` — source key extractor + used/undefined/unused cross-reference.
- `engine.ts` — `Translator`: register catalogs, fallback chain, locale switch +
  subscribe, pseudo toggle, direction, ICU `t`/`tParts`, bound formatters.
- `cli.ts` + `cli-core.ts` — `lint` / `coverage` / `extract` / `pseudo` CLI
  (run via `node --experimental-strip-types src/lib/intl/cli.ts <cmd>`).

React layer (`i18n/`):
- `messages.ts` — typed `MessageKey` union derived from `en`; lazy `loadCatalog`
  (`import.meta.glob`, one chunk per locale); locale metadata.
- `IntlProvider.tsx` — owns a `Translator`, detects+lazy-loads+persists the
  locale (shares `kinora.lang` with i18next), syncs `<html lang/dir>`,
  re-renders via `useSyncExternalStore`. Additive — mounted in `App.tsx`.
- `useT.ts` — `useT`/`useTParts`/`useLocale`/`useDirection`/`useLocaleSwitch`/
  `useFormatters`/`useIntl` (all typed).
- `T.tsx` — `<T k=… args=… components=…>` renders ICU rich-text as elements.
- `locales/` — en/es/fr/de/zh/hi (existing) + **ja, ar (RTL), pt-BR** (new), each
  with an `icu` showcase section (plural/select/date/number/currency/ordinal).

## Remaining roadmap (future)
- Adopt `useT()` in components (currently still react-i18next) — out of domain now.
- Wire a language switcher UI to `useLocaleSwitch()` + a QA pseudo toggle to
  `setPseudo()` (a Settings control — owned by the components agent).
- Number/date **skeleton** coverage could grow (only the common `::` tokens now).
- Optional: a Vite/eslint plugin that runs `cli.ts lint` in CI; a message-id hash
  for over-the-air catalog updates.

## Testing convention (followed)

- Pure-core engine tests use `node:test` + `assert/strict`, import siblings with explicit
  `.ts` extensions, and are auto-discovered by `src/test/run-node-tests.mjs`
  (`--experimental-strip-types`). They must NOT touch the DOM.
- React/provider tests use vitest + Testing Library (jsdom), default `.test.tsx`.
- Gate after every milestone: `pnpm --filter @kinora/desktop run typecheck` and `… run test`.

## Additive shared-file changes

- `App.tsx`: wrap the tree in `<IntlProvider>` additively (optional, no-op if engine
  catalog matches i18next). Documented inline. No existing behavior removed.

## Non-goals / guardrails

- KINORA_LIVE_VIDEO stays OFF. No commits/pushes — working tree only.
- Do not rename or restructure the existing `i18n/index.ts` exports
  (`setLanguage`, `currentLanguage`, `SUPPORTED_LANGUAGES`, …) — other code imports them.
- The new engine is additive; components keep using i18next until they opt in.
