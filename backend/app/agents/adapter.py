"""Adapter / Screenwriter — page → beats → shot list (§4.2, §9.1, §10).

Reads page text (and detected illustrations) into narrative beats, then
decomposes beats into ~5-second shots with their source spans and cost
estimates. It is the :class:`app.memory.interfaces.ShotPlanner` the memory layer
declares as a seam: :meth:`plan_scene` reads a scene's beats and returns the
render-queue shot specs.

``analyze_page`` is the only LLM call here (page → beats, with the §10 "never
invent a character" guardrail). Every beat is then run through the deep
literary-comprehension engine (:mod:`app.agents.comprehension`) — a set of PURE,
network-free passes that add multi-POV / unreliable-narrator tagging,
free-indirect-discourse + interiority detection, dialogue attribution + speaker
diarization, literary-device → visual-intent translation, pacing-aware tempo, and
(across a sequence) non-linear timeline reconstruction (narrative-time vs
story-time). ``plan_shots`` is deterministic AND pacing-aware: shot density and
per-shot duration vary with each beat's tempo, so a dramatised scene gets denser
coverage than a summarised span — and the whole beat→shot decomposition is still
unit-testable without a network. ``plan_scene`` wraps it over the persisted beats
(read via ``BeatRepo``).
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.repositories.beat import BeatRepo
from app.db.session import get_session
from app.memory.interfaces import ShotPlanner
from app.memory.interfaces import ShotSpec as RenderShotSpec
from app.providers import Providers

from .base import BaseAgent
from .comprehension import (
    BeatComprehension,
    analyze_beat,
    build_shot_intent,
    duration_bias,
    enrich_sequence,
    merge_comprehension,
    words_per_shot_for,
)
from .contracts import (
    AnalyzePageRequest,
    AnalyzePageResponse,
    Beat,
    EstCost,
    ShotListItem,
    SourceSpan,
)
from .prompts import ADAPTER, ADAPTER_COMPREHEND

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
BeatsLoader = Callable[[str], Awaitable[Sequence[Any]]]

#: A beat splits into shots of roughly this many narration words each (~5s screen
#: time), matching the §4.1 reading/screen-time asymmetry.
WORDS_PER_SHOT = 60
SHOT_SECONDS = 5.0
#: Per-shot target duration is decided by the planner from each shot's own
#: narration length, so a dense shot runs longer than a sparse one instead of
#: every shot being a fixed constant. The anchor is the §4.2 example — a full
#: ``WORDS_PER_SHOT`` (60-word) shot ≈ ``SHOT_SECONDS`` (5s) of screen-time,
#: i.e. ~12 narration words/sec of compressed screen-time (the §4.1 reading↔
#: screen-time asymmetry, distinct from the §4.3 *reading* velocity). The result
#: is clamped to ``[MIN_SHOT_SECONDS, MAX_SHOT_SECONDS]`` so no single clip is
#: jarringly short or burns excess video-seconds.
SCREEN_WORDS_PER_SECOND = WORDS_PER_SHOT / SHOT_SECONDS  # 12 words/sec → 60 words ≈ 5s
MIN_SHOT_SECONDS = 3.0
MAX_SHOT_SECONDS = 8.0
#: Token-cost estimate per shot: a fixed planning/prompt floor plus per-word.
BASE_SHOT_TOKENS = 1500
TOKENS_PER_WORD = 6


class Adapter(BaseAgent):
    """Turns pages into beats and beats into a shot list (implements ShotPlanner)."""

    def __init__(
        self,
        providers: Providers,
        *,
        settings: Settings | None = None,
        session_factory: SessionFactory = get_session,
        beats_loader: BeatsLoader | None = None,
        skills: object | None = None,
    ) -> None:
        settings = settings or get_settings()
        super().__init__(
            providers,
            name="adapter",
            model=settings.chat_model_adapter,
            prompt=ADAPTER,
            skills=skills,  # type: ignore[arg-type]
        )
        self._sf = session_factory
        self._beats_loader = beats_loader

    # -- page → beats (§9.1) ------------------------------------------------- #

    async def analyze_page(
        self,
        page_text: str,
        *,
        page: int = 1,
        scene_id: str | None = None,
        beat_index_start: int = 0,
        detected_illustrations: list[str] | None = None,
        known_entities: set[str] | None = None,
        max_tokens: int | None = None,
        comprehend: bool = True,
    ) -> list[Beat]:
        """Segment a page into beats; assign ids, resolve entities, comprehend.

        When ``known_entities`` is given, any entity the model named that is not
        in the canon is moved to ``unresolved_entities`` — a deterministic
        enforcement of the §10 "never invent a character" guardrail. ``max_tokens``
        bounds the (potentially large) multi-beat JSON generation.

        Unless ``comprehend`` is False, each beat is then enriched in place by the
        deep literary-comprehension engine (POV/discourse/dialogue/devices/tempo).
        Per-beat comprehension is order-free, so it is safe to run page-by-page;
        the cross-beat *story-time* reconstruction is a separate pass over the
        whole book (:meth:`comprehend_sequence`) the ingest phase calls once.
        """
        request = AnalyzePageRequest(
            page=page,
            page_text=page_text,
            scene_id=scene_id,
            beat_index_start=beat_index_start,
            detected_illustrations=detected_illustrations or [],
        )
        response = await self.run_json(
            request, AnalyzePageResponse, temperature=0.2, max_tokens=max_tokens
        )
        beats: list[Beat] = []
        for offset, raw in enumerate(response.beats):
            index = beat_index_start + offset
            entities, unresolved = self._resolve_entities(raw, known_entities)
            span = raw.source_span.model_copy(update={"page": raw.source_span.page or page})
            beat = raw.model_copy(
                update={
                    "beat_id": f"beat_{index:04d}",
                    "beat_index": index,
                    "scene_id": raw.scene_id or scene_id,
                    "entities": entities,
                    "unresolved_entities": unresolved,
                    "source_span": span,
                }
            )
            if comprehend:
                beat = analyze_beat(beat, canon_names=known_entities)
            beats.append(beat)
        return beats

    def comprehend_sequence(
        self,
        beats: Sequence[Beat],
        *,
        known_entities: set[str] | None = None,
    ) -> list[Beat]:
        """Deeply comprehend a whole book's beat sequence (pure, no network).

        Runs the per-beat passes *and* the book-level non-linear timeline
        reconstruction so each beat carries a ``story_time`` (narrative-order vs
        story-order, flashback/flash-forward position). Called once by the ingest
        phase after all pages are segmented — flashbacks span pages, so story-time
        can only be resolved across the full ordered sequence. Re-running it on
        already-comprehended beats is idempotent for the per-beat fields and only
        refines ``story_time`` with the full neighbour context.
        """
        return enrich_sequence(beats, canon_names=known_entities)

    async def enrich_beat_llm(
        self,
        beat: Beat,
        *,
        known_entities: set[str] | None = None,
        max_tokens: int | None = 600,
    ) -> Beat:
        """Refine ONE beat's comprehension with a bounded LLM pass over the floor.

        The deterministic heuristic runs first (the floor); the model is then
        shown the beat text + the heuristic verdict and asked to correct it only
        where the text plainly disagrees. The reply is merged CONSERVATIVELY and
        canon-guarded (:func:`merge_comprehension`) — a refined POV character or
        dialogue speaker absent from ``known_entities`` is dropped (§10 no-invent).
        On any model/validation failure the heuristic beat is returned unchanged,
        so the LLM pass can only improve, never regress.
        """
        floor = analyze_beat(beat, canon_names=known_entities)
        payload = {
            "text": f"{floor.summary} {floor.described_visuals or ''}".strip(),
            "known_entities": sorted(known_entities) if known_entities else [],
            "heuristic": {
                "pov": floor.pov.value,
                "pov_character": floor.pov_character,
                "unreliable": floor.unreliable,
                "discourse": floor.discourse.value,
                "tempo": floor.tempo.value,
                "dialogue": [d.model_dump() for d in floor.dialogue],
                "devices": [d.model_dump() for d in floor.devices],
            },
        }
        try:
            refined = await self.run_json(
                payload,
                BeatComprehension,
                temperature=0.1,
                max_tokens=max_tokens,
                system=ADAPTER_COMPREHEND.system,
            )
        except (ValidationError, ValueError):
            return floor
        return merge_comprehension(floor, refined, known_entities=known_entities)

    @staticmethod
    def _resolve_entities(
        beat: Beat, known_entities: set[str] | None
    ) -> tuple[list[str], list[str]]:
        if known_entities is None:
            return list(beat.entities), list(beat.unresolved_entities)
        kept = [e for e in beat.entities if e in known_entities]
        unresolved = list(beat.unresolved_entities)
        for entity in beat.entities:
            if entity not in known_entities and entity not in unresolved:
                unresolved.append(entity)
        return kept, unresolved

    # -- beats → shots (deterministic, §4.2) --------------------------------- #

    def plan_shots(self, beats: Sequence[Beat]) -> list[ShotListItem]:
        """Split each beat into shots; pacing-aware density + per-shot duration.

        The split is PURE and deterministic but pacing-aware (§4.2): a beat's
        :class:`SceneTempo` sets how many narration words one shot covers
        (``words_per_shot_for`` — a dramatised SCENE keeps the baseline density,
        a SUMMARY/ELLIPSIS packs a long span into a single clip) and biases each
        shot's screen-time (``duration_bias`` — a held PAUSE lingers). A neutral
        ``SceneTempo.SCENE`` beat reproduces the legacy behaviour exactly.

        ``target_duration_s`` is decided per-shot from that shot's own narration
        length (§4.3 reading pace) times the tempo bias, then clamped to
        ``[MIN_SHOT_SECONDS, MAX_SHOT_SECONDS]``. ``est_cost.video_seconds`` —
        the scarce, hard-capped budget unit — matches the shot's own duration.
        """
        items: list[ShotListItem] = []
        for beat in beats:
            tempo = beat.tempo
            per_shot = words_per_shot_for(tempo, WORDS_PER_SHOT)
            bias = duration_bias(tempo)
            # The beat's comprehension-derived staging brief is shared by all of
            # its shots (one beat → one continuous moment, just split for length).
            intent = build_shot_intent(beat)
            for shot_index, span in enumerate(self._split_span(beat.source_span, per_shot)):
                words = max(span.word_range[1] - span.word_range[0], 0) or per_shot
                duration = self._duration_for_words(words, bias)
                items.append(
                    ShotListItem(
                        shot_id=f"{beat.beat_id or 'beat'}_shot_{shot_index:02d}",
                        beat_id=beat.beat_id,
                        scene_id=beat.scene_id,
                        source_span=span,
                        est_duration_s=duration,
                        est_cost=EstCost(
                            video_seconds=duration,
                            tokens=BASE_SHOT_TOKENS + words * TOKENS_PER_WORD,
                        ),
                        intent=intent,
                    )
                )
        return items

    @staticmethod
    def _duration_for_words(words: int, bias: float = 1.0) -> float:
        """Per-shot target seconds: words / reading-pace × tempo bias, clamped.

        Deterministic (no LLM): a denser shot earns more screen-time, a sparse
        one less; the ``bias`` lets a held PAUSE linger and a SCENE stay brisk.
        Never outside ``[MIN_SHOT_SECONDS, MAX_SHOT_SECONDS]``.
        """
        seconds = (max(words, 0) / SCREEN_WORDS_PER_SECOND) * bias
        clamped = min(max(seconds, MIN_SHOT_SECONDS), MAX_SHOT_SECONDS)
        return round(clamped, 1)

    @staticmethod
    def _split_span(span: SourceSpan, words_per_shot: int = WORDS_PER_SHOT) -> list[SourceSpan]:
        start, end = span.word_range
        words = max(end - start, 0)
        per = max(1, words_per_shot)
        count = max(1, math.ceil(words / per)) if words else 1
        if count == 1:
            return [span]
        step = words / count
        spans: list[SourceSpan] = []
        for i in range(count):
            lo = start + round(i * step)
            hi = end if i == count - 1 else start + round((i + 1) * step)
            spans.append(span.model_copy(update={"word_range": (lo, hi)}))
        return spans

    # -- ShotPlanner protocol (§ interfaces) --------------------------------- #

    async def plan_scene(self, scene_id: str) -> list[RenderShotSpec]:
        """Read a scene's beats and return the render-queue shot specs.

        Persistence of beats/shots is the ingest phase's job; this returns typed
        objects only. The Cinematographer fills render_mode/prompt/refs later.
        """
        rows = await self._load_scene_beats(scene_id)
        beats = [self._beat_from_row(row) for row in rows]
        book_id = str(getattr(rows[0], "book_id", "")) if rows else ""
        return [self._render_spec(item, book_id) for item in self.plan_shots(beats)]

    async def _load_scene_beats(self, scene_id: str) -> Sequence[Any]:
        if self._beats_loader is not None:
            return await self._beats_loader(scene_id)
        async with self._sf() as session:
            return await BeatRepo(session).list_by_scene(scene_id)

    @staticmethod
    def _beat_from_row(row: Any) -> Beat:
        raw_span = getattr(row, "source_span", None) or {}
        span = SourceSpan.model_validate(raw_span) if raw_span else SourceSpan()
        return Beat(
            beat_id=str(getattr(row, "id", "")),
            scene_id=getattr(row, "scene_id", None),
            beat_index=int(getattr(row, "beat_index", 0) or 0),
            summary=str(getattr(row, "summary", "")),
            entities=list(getattr(row, "entities", None) or []),
            described_visuals=getattr(row, "described_visuals", None),
            mood=getattr(row, "mood", None),
            source_span=span,
        )

    @staticmethod
    def _render_spec(item: ShotListItem, book_id: str) -> RenderShotSpec:
        return RenderShotSpec(
            book_id=book_id,
            beat_id=item.beat_id,
            scene_id=item.scene_id,
            shot_id=item.shot_id,
            target_duration_s=item.est_duration_s,
        )


def _planner_conformance(adapter: Adapter) -> ShotPlanner:
    """Static guarantee (checked by ``mypy app``) that Adapter is a ShotPlanner."""
    return adapter


__all__ = [
    "BASE_SHOT_TOKENS",
    "MAX_SHOT_SECONDS",
    "MIN_SHOT_SECONDS",
    "SCREEN_WORDS_PER_SECOND",
    "SHOT_SECONDS",
    "TOKENS_PER_WORD",
    "WORDS_PER_SHOT",
    "Adapter",
]
