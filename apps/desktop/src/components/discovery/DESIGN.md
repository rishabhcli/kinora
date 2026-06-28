# Discovery surface — DESIGN.md (living roadmap)

**Agent: Frontend Home shell + discovery.** A Netflix-grade discovery surface for
the Kinora home: personalized shelves, semantic + faceted search, a
recommendations engine, continue-reading, a ⌘K command palette, full keyboard
navigation + focus management, rich hover/preview, and skeleton/loading states.

## Domain & boundaries
**OWNED** (this agent, this session):
- `apps/desktop/src/components/HomePage.tsx` — the home shell / app navigation
- `apps/desktop/src/components/BookShelf.tsx`, `BookCard.tsx`, `BookTicker.tsx`
- `apps/desktop/src/components/discovery/**` — NEW discovery surface
- `apps/desktop/src/lib/discovery/**` — NEW pure logic cores (engine)

**FORBIDDEN** (round-1 Director-Studio + Library agents own these — do NOT touch):
- `components/LibraryPage.tsx`, `components/library/**`
- `components/director/**`

**SHARED SEAM (additive-only):** `src/lib/api.ts` — reuse the `http` primitive,
`toBrowserUrl`, `toUiBook`, `BookResponse`. Never edit; compose against it from
`lib/discovery/*`. Any additive change is documented in this file's "Shared-file
changes" section.

## Architecture (mirrors the codebase convention)
Pure, DOM-free logic cores live in `src/lib/discovery/*.ts` with co-located
**vitest** tests (the dominant pattern — see `lib/api/collections.ts`). React
components live in `src/components/discovery/*.tsx` with **RTL** tests. The bulk
of the intelligence is in the pure cores so it is fully testable without a DOM.

All randomness/seeds are injectable; all storage goes through a `KeyValueStore`
seam (same shape `lib/api/collections.ts` uses) so tests are deterministic.

## Phases

### Phase 1 — Engine cores (pure, fully tested) ✅
- [x] `lib/discovery/types.ts` — shared discovery types (`DiscoveryBook`, signals)
- [x] `lib/discovery/tokenize.ts` — text normalization, tokenization, fuzzy core
- [x] `lib/discovery/search.ts` — faceted + ranked + fuzzy search; suggestions
- [x] `lib/discovery/affinity.ts` — taste profile from interaction history
- [x] `lib/discovery/scoring.ts` — per-book recommendation scoring + explanations
- [x] `lib/discovery/rows.ts` — personalized shelf/row generation (Netflix rows)
- [x] `lib/discovery/continueReading.ts` — continue-reading ranking model
- [x] `lib/discovery/history.ts` — interaction-history store (KeyValueStore seam)
- [x] `lib/discovery/palette.ts` — command registry + fuzzy command matcher
- [x] `lib/discovery/preview.ts` — hover-intent preview state machine

### Phase 2 — React discovery surface ✅
- [x] `components/discovery/useDiscovery.ts` — wire cores to React state
- [x] `components/discovery/CommandPalette.tsx` — ⌘K global nav (focus-trapped)
- [x] `components/discovery/DiscoverySearch.tsx` — faceted/semantic search panel
- [x] `components/discovery/BookPreviewCard.tsx` — rich hover/preview card
- [x] `components/discovery/RecommendationRail.tsx` — explained recommendation row
- [x] `components/discovery/ContinueReadingRow.tsx` — continue-reading row
- [x] `components/discovery/RowSkeleton.tsx` — skeleton/loading states
- [x] `components/discovery/DiscoveryHome.tsx` — orchestrator (personalized rows)

### Phase 3 — Integration ✅
- [x] `components/discovery/commands.ts` — ⌘K command registry builder
- [x] `components/discovery/useCommandPalette.ts` — ⌘K / "/" shortcut + state
- [x] `components/discovery/buildCatalog.ts` — merge backend + demo books, popularity
- [x] `components/discovery/index.ts` — package barrel
- [x] Wire `CommandPalette` + `DiscoverySearch` + `DiscoveryHome` into `HomePage.tsx`
- [x] Personalized rows + continue-reading replace static shelves on Home; new
      "Search" page seeded by the palette / "More like this"

### Phase 4 — Depth ✅
- [x] `lib/discovery/facets.ts` — facet derivation + counts from a catalog
- [x] `lib/discovery/semantic.ts` — lightweight semantic similarity (token TF + synonyms)
- [x] `lib/discovery/recents.ts` — recently-viewed ring buffer
- [x] `lib/discovery/roving.ts` — keyboard roving-grid index math (ragged grid)
- [x] `components/discovery/useRovingGrid.ts` — roving-tabindex React hook; wired
      into `DiscoveryHome` so the rails are one Tab stop with full arrow nav
- [x] `components/discovery/styleInjection.ts` — owned keyframes (no shared-CSS edits)
- [x] "Did you mean …" suggestion in `DiscoverySearch` (clickable fix)
- [x] Browser end-to-end verification: HomePage mounts, 2 rails + continue-reading
      render (backend-down graceful degradation), ⌘K opens + fuzzy-filters + Esc closes

## Test counts (final)
- `lib/discovery/*` pure cores: 13 modules, ~128 tests (vitest, DOM-free)
- `components/discovery/*` React + glue: ~16 modules, ~80 tests (RTL + renderHook)
- Full app suite GREEN: 58 vitest files / 423 tests; node-test 24 files / 0 failing;
  `tsc --noEmit` clean; `vite build` succeeds.

## Shared-file changes (additive-only)
_None._ The discovery cores consume `lib/api.ts` exports (`http`, `toBrowserUrl`,
`toUiBook`, `BookResponse`, `api`) without editing the file.

## Test strategy
- Pure cores: `vitest` (DOM-free) co-located `*.test.ts`.
- React: `@testing-library/react` `*.test.tsx`, deterministic via injected stores
  + fake timers for hover-intent/preview.
- Keep `pnpm --filter @kinora/desktop run typecheck` + `run test` green each phase.
