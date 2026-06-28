# Content translation subsystem — `backend/app/translation/`

**Domain owner:** content-translation pipeline (a parallel-agent worktree).
**Scope boundary:** this is the *content* translation layer — translating the
material a reader consumes (book page text, canon entity descriptions, narration
scripts). It is **distinct** from any UI-string i18n layer (which translates
button labels). Nothing here touches the live render/video path; it is a
token-only, deterministic-in-tests subsystem keyed to source content hashes.

It cites kinora.md §8 (canon entities + content hashing §8.7) and §9 (the
generation pipeline — narration scripts §9.4 are a translation target).

## Why this exists

A Kinora adaptation can be read in a language other than the source book's. The
canon is authored once (in the source language) but the *reader-facing strings*
— page text shown in the read-along surface, the entity descriptions a localized
canon viewer renders, and the narration scripts CosyVoice speaks — must be
translatable into the reader's language **without corrupting the structured
markup, placeholders, and do-not-translate proper nouns** the rest of the
pipeline depends on. And because re-reading must be free (§8.7), every
translation is cached against a content hash so a re-translation costs nothing.

## Architecture (layered, bottom-up)

```
languages.py        BCP-47 normalization, RTL/script metadata, language registry
detect.py           heuristic + pluggable language detection
markup.py           placeholder/markup masking ("protect → translate → restore")
glossary.py         glossary + do-not-translate (DNT) terms, longest-match apply
memory_store.py     in-memory translation memory (TM) + fuzzy match
provider.py         TranslationProvider ABC + FakeTranslationProvider (tests)
llm_provider.py     LLM-backed provider shaped on app.providers.chat (real path)
segment.py          segment-aware text splitting (sentence/paragraph units)
quality.py          quality estimation + back-translation round-trip checks
cost.py             batching, cost accounting, per-language spend ledger
hashing.py          source-content-hash keys (mirrors db/hashing.py spirit)
service.py          TranslationService — orchestrates the whole pipeline
document.py         page / narration / entity-description translators (stitched)
canon.py            build a DNT glossary from canon entity names (§8.1 bridge)
artifacts.py        persisted translation artifacts (DB models + repo)
review.py           review / post-edit workflow state machine
rtl.py              RTL/bidi handling helpers
errors.py           typed translation errors
types.py            shared dataclasses / pydantic models
```

Status: **all milestones M1–M10 implemented + tested** (see below). 152 tests
(unit + DB-bound + API), `make lint` (ruff + mypy over 401 files) green, full
`make test` green (1171 passed pre-existing + the new translation tests).

DB tables (Alembic migration chained on head `a1b2c3d4e5f6`, unique rev id
`c1d2e3f4a5b6`): `translation_artifacts`, `translation_segments`,
`translation_glossary`, `translation_reviews`. All FK to `books.id` with
`ondelete=CASCADE`, mirroring the entity/shot models' conventions.

## Pipeline (one `translate_segments` call)

```
detect source lang (if unknown)
  → for each segment:
      mask markup + placeholders (markup.py)         # protect {name}, <b>…</b>, %s
      apply DNT + glossary pre-substitution           # lock proper nouns
      TM lookup by (src_hash, target_lang)            # cache hit → 0 cost
      on miss: provider.translate(...)                # batched, cost-accounted
      restore markup + placeholders
      glossary post-verification                      # ensure DNT survived
      quality estimate (+ optional back-translation)  # score 0..1
  → persist artifact keyed to source content hash
  → optionally enqueue low-confidence segments for human post-edit (review.py)
```

## Hard rules honored
- **No live model calls in tests.** Every provider is injectable; the default
  test double is `FakeTranslationProvider` (deterministic, pure). Zero credits.
- **KINORA_LIVE_VIDEO untouched** — this subsystem never renders video.
- DB-bound tests use the isolated `kinora_translation_test` DB on :5433 and
  **skip cleanly** when `KINORA_TRANSLATION_TEST_DATABASE_URL` is unset.

## Additive shared-file changes (documented per the worktree rules)
ALL changes outside `app/translation/` + `tests/test_translation_*.py` are
strictly additive (no existing line repurposed):
- `app/core/config.py`: appended a `translation_*` settings block (6 fields,
  defaults only; nothing required). Inserted after the CORS block.
- `app/db/models/__init__.py`: import + `__all__`-export of the four new models
  (`TranslationArtifact`, `TranslationSegment`, `TranslationGlossaryRow`,
  `TranslationReview`) + the two status enums.
- `app/api/routes/__init__.py`: import `translation` + append `translation.router`
  to `ROUTERS` (last entry).
- `app/composition.py`: added an overridable `translation_provider` seam, a
  `_translation_provider` lazy cache, a `_get_translation_provider()` accessor,
  and `build_translation_service(glossary, memory)`. Two TYPE_CHECKING imports.
  No existing seam touched.
- Alembic: one new migration `c1d2e3f4a5b6` (`down_revision="a1b2c3d4e5f6"`,
  the current head — verified single head after).

A new route file `app/api/routes/translation.py` is owned by this domain (not a
shared file). The canon read in that file uses the existing `Entity` model
read-only.

## Milestones / roadmap

- **M1 — Language + markup core.** `languages.py`, `markup.py`, `detect.py`,
  `rtl.py`, `errors.py`, `types.py`. Pure, no I/O.
- **M2 — Provider abstraction.** `provider.py` (ABC + fake), `segment.py`.
- **M3 — Glossary + DNT + TM.** `glossary.py`, `memory_store.py`.
- **M4 — Quality + back-translation + cost.** `quality.py`, `cost.py`,
  `hashing.py`.
- **M5 — Service orchestration.** `service.py`.
- **M6 — LLM-backed provider (real path, shaped, no live calls).**
  `llm_provider.py`.
- **M7 — Persistence.** `artifacts.py` (models + repo) + Alembic migration.
- **M8 — Review/post-edit workflow.** `review.py`.
- **M9 — API surface.** `app/api/routes/translation.py` — 8 routes (languages,
  translate, list/get artifacts, glossary GET/POST, reviews GET + action POST).
- **M10 — Composition wiring + config.** additive `translation_provider` seam +
  `build_translation_service` + `translation_*` settings.
- **M11 — Canon bridge + document layer.** `canon.py` lifts canon character names
  into a DNT glossary (the §8.1 connection — the translate route auto-locks them);
  `document.py` translates whole pages / narration / entity descriptions and
  stitches the structure back.

### Future depth (roadmap)
- sub-segment alignment to re-use the §9.4 narration word-timestamps across
  languages (translate-then-realign so the karaoke highlight survives a re-voice);
- ICU MessageFormat / pluralization + gender selection in `markup.py`;
- terminology-consistency scoring across a whole book (flag a term translated two
  different ways in different chapters);
- glossary import/export (TBX/CSV) and a translation-quality dashboard fed by the
  `CostLedger` summaries + review-state aggregates;
- a model-backed `Detector` behind the same `Detector` protocol (no pipeline change);
- streaming/partial translation for very long pages (the batcher already bounds
  by token budget; add an async generator surface).
```
