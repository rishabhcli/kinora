# Reader Assistant — grounded RAG Q&A over a book + its canon

> Domain package: `backend/app/assistant/`. A spoiler-aware question-answering
> assistant grounded in a book's pages, canon entities, and accepted shots.
> Every claim must cite a retrieved span; the assistant never reveals anything
> past the reader's current position.

This is a *living roadmap*. It tracks the design, the milestones shipped, and
what remains. The package reuses three existing seams behind protocols, all
satisfiable by fakes in tests (zero live calls, zero credits):

* the **Embedder** (`app.memory.interfaces.Embedder` / `providers.embeddings`),
* the **chat provider** (`app.providers.chat.ChatProvider`),
* the retrieval math in `app.memory.retrieval` (MMR, hybrid score, packing).

## Why (kinora.md §8)

§8 is the canon-memory layer: a structured canon graph (§8.1), an episodic /
vector store of every accepted shot (§8.2), and a retrieval policy that recalls
*only what a beat needs* under a limited context window (§8.4). The reader
assistant is the human-facing read side of that same memory: instead of feeding
a slice to the Cinematographer, it feeds a slice to a chat model answering the
reader's question — and it inherits §8.5's forgetting discipline as a **spoiler
horizon** (facts valid only up to the reader's beat are visible; later canon is
invisible, exactly as retired facts drop out of forward generation).

## Layered architecture (each layer pure where possible; seams injected)

```
routes (api/routes/assistant.py)  ── additive registration only
        │
   AssistantService (service.py)  ── orchestrates a turn end-to-end
        ├── Retriever (retrieval.py)        spoiler-aware hybrid recall
        │     ├── candidate sources: pages / canon entities / accepted shots
        │     └── SpoilerHorizon (spoiler.py)  position → ceiling
        ├── ContextAssembler (context.py)   pack under token budget + citations
        ├── PromptBuilder (prompts.py)      grounded system+user prompt, intents
        ├── AnswerSynthesizer (synth.py)    chat provider (faked in tests)
        ├── GroundingGuard (grounding.py)   citations must map to retrieved spans
        ├── ConversationMemory (memory.py)  multi-turn session history
        └── Suggestions (suggest.py)        suggested follow-up questions
   Intents (intents.py)   classify "explain passage / who is X / what happened so far"
   Eval (eval.py)         faithfulness + citation-coverage on synthetic Q&A
```

## Milestones

- [x] **M1 — domain types** (`types.py`): `RetrievedSpan`, `SourceKind`,
      `Citation`, `Answer`, `AssistantTurn`, `ReadingPosition`, intents,
      streaming deltas. Pure, fully typed, Pydantic.
- [x] **M2 — spoiler horizon** (`spoiler.py`): map a reading position to a
      beat/word ceiling; filter candidate spans; pure + tested.
- [x] **M3 — retrieval primitives** (`retrieval.py`): candidate scoring built on
      `memory.retrieval` (hybrid + MMR), source-kind weighting, dedup. Pure core
      + a DB-backed `CanonReadModel` protocol satisfied by a fake.
- [x] **M4 — context assembly** (`context.py`): pack retrieved spans under a
      token budget by value-density, assign stable citation markers `[1]..[n]`.
- [x] **M5 — prompt builder** (`prompts.py`): grounded system prompt + per-intent
      user prompt; citation instruction; refusal contract.
- [x] **M6 — intents** (`intents.py`): rule-based classifier (who-is / explain /
      recap / general) with entity-name extraction; pure + tested.
- [x] **M7 — grounding guard** (`grounding.py`): parse `[n]` markers from a draft
      answer, verify each maps to a retrieved span, strip/flag unsupported claims,
      compute citation coverage.
- [x] **M8 — answer synthesizer** (`synth.py`): non-streaming + streaming answer
      over the chat seam; JSON-mode citation extraction with a deterministic
      fallback; faked chat in tests.
- [x] **M9 — conversation memory** (`memory.py`): in-process + Redis-backed
      multi-turn history with a token-bounded window; pure default.
- [x] **M10 — suggestions** (`suggest.py`): generate follow-up questions from the
      retrieved slice (deterministic, entity-aware; optional LLM refinement seam).
- [x] **M11 — service** (`service.py`): orchestrate a full turn (retrieve →
      assemble → prompt → synth → guard → remember → suggest); streaming variant.
- [x] **M12 — eval harness** (`eval.py`): synthetic Q&A generation + faithfulness
      / citation-coverage / spoiler-safety scoring; pure, no network.
- [x] **M13 — DB read model** (`read_model.py`): the real `CanonReadModel` over
      pages/entities/shots repos; spoiler-aware position resolution.
- [x] **M14 — API routes** (`api/routes/assistant.py`): `POST /books/{id}/ask`,
      streaming SSE `/ask/stream`, `GET .../suggestions`, conversation read.
- [x] **M15 — composition wiring**: additive `Container.build_assistant()`.

## Roadmap (future phases)

- Pluggable rerank backends (cross-encoder seam) beyond MMR.
- Per-reader answer-style preferences via `prefs_service` (§8.6).
- Highlight-anchored "explain this selection" with exact char ranges.
- Caching of (book, position, question) → answer, mirroring §8.7's shot cache.
- Multi-book / series-aware retrieval (cross-volume canon).

## Additive shared-file changes (documented per the parallel-work rules)

- `api/routes/__init__.py` — append `assistant.router` to `ROUTERS` (additive).
- `composition.py` — add `Container.build_assistant()` factory (additive method).
- No new DB tables required (reads existing pages/entities/shots/beats). The
  optional Redis conversation store uses keys, not tables, so **no migration**.
- If a future phase persists conversations to Postgres, it adds an Alembic
  migration on head `a1b2c3d4e5f6` with a UNIQUE revision id.

## Testing

`backend/tests/test_assistant_*.py` — pure-unit for every module (no infra),
plus an API smoke guarded by `requires_infra`. Fakes: `FakeChat`,
`FakeEmbedder` (reuse conftest), `FakeCanonReadModel`. Run:
`backend/.venv/bin/pytest tests/test_assistant_*.py -q`.
